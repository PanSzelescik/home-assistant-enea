"""Turning range responses of the Portal Odbiorcy Enea into days to import.

Everything here is a pure function of the responses and of explicit arguments
— no clock, no network, no Home Assistant state — so the rules deciding what
reaches the statistics can be tested on saved responses directly.  The
coordinator only fetches the responses and hands them over.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

DayData = dict[str, Any]
"""One day's responses: statistic key → single-day response with 'values' and 'zones'."""


def has_data(api_response: dict[str, Any]) -> bool:
    """Return True if the response contains at least one non-null value."""
    for entry in api_response.get("values", []):
        for item in entry.get("items", []):
            if item.get("value") is not None:
                return True
    return False


def slot_start_dt(entry: dict[str, Any]) -> datetime:
    """Return the local-aware start datetime of an hourly slot.

    Derived from the slot's ``integrationEnd`` (ms epoch, end of the hour) rather
    than from ``timeId``, so it stays correct across DST transitions — the API
    returns 23 or 25 slots on the spring/autumn switch days, not a fixed 24.
    """
    end = dt_util.utc_from_timestamp(entry["integrationEnd"] / 1000)
    return (end - timedelta(hours=1)).astimezone(dt_util.DEFAULT_TIME_ZONE)


def split_range_response(api_response: dict[str, Any]) -> dict[date, dict[str, Any]]:
    """Split a flat range API response into per-day response dicts.

    Groups the hourly slots by each slot's actual local date (derived from
    ``integrationEnd``) rather than by fixed 24-slot blocks, so DST transition
    days — which carry 23 or 25 slots, not 24 — are split correctly.  Each
    per-day dict has the same {'values', 'zones'} shape as a single-day
    response, so has_data, _collect_series and cost statistics need no changes.

    The hourly slots are in 'valuesToTable'.  For a range spanning several
    months 'values' holds monthly sums instead (one entry per month, with an
    extra zone -1 for the total and an ``integrationEnd`` that is not the end of
    that month); for a short range it repeats 'valuesToTable'.  Whichever holds
    more entries is used, so the monthly sums never stand in for hours.

    Args:
        api_response: Raw API response from the range endpoint.

    Returns:
        Mapping of date → single-day response dict with 'values' and 'zones'.
    """
    values: list[dict[str, Any]] = api_response.get("values") or []
    values_to_table: list[dict[str, Any]] = api_response.get("valuesToTable") or []
    entries = values_to_table if len(values_to_table) > len(values) else values
    zones = api_response.get("zones", [])

    result: dict[date, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("integrationEnd") is None:
            continue
        day = slot_start_dt(entry).date()
        result.setdefault(day, {"values": [], "zones": zones})["values"].append(entry)
    return result


def zero_fill_missing_day(day_data: DayData) -> DayData:
    """Replace null slot values with 0.0 for a day confirmed permanently missing.

    The Enea API still returns hourly slot entries for a missing day, just
    with ``value: null`` for every item — this only rewrites those values,
    it does not fabricate slots. Used once a day has been missing for
    longer than MISSING_DAY_GRACE_DAYS, so it is injected as a legitimate
    zero-consumption day instead of leaving a hole that resets the
    cumulative sum of later statistics to 0 (see _inject_energy_series /
    _inject_cost_series).
    """
    result: DayData = {}
    for key, api_response in day_data.items():
        zeroed_values = [
            {
                **entry,
                "items": [
                    {**item, "value": item.get("value") or 0.0}
                    for item in entry.get("items", [])
                ],
            }
            for entry in api_response.get("values", [])
        ]
        result[key] = {**api_response, "values": zeroed_values}
    return result


def strip_pre_assembly_slots(
    day: date, day_data: DayData, assembly: datetime | None
) -> DayData:
    """Remove hourly slots that predate the meter assembly on the assembly day.

    Keeps slots whose hour starts at or after the assembly hour, so the slot
    containing the assembly moment is included (it carries new-meter readings
    from the assembly minute onwards).  Compares each slot's actual start time
    (slot_start_dt, derived from integrationEnd) rather than timeId, so it
    stays correct even if the meter was assembled on a DST transition day.
    Example: assembly at 12:13 → keep slots starting 12:00 onwards.
    """
    if assembly is None or day != assembly.date():
        return day_data
    cutoff = assembly.replace(minute=0, second=0, microsecond=0)
    return {
        key: {
            **api_response,
            "values": [
                entry for entry in api_response.get("values", [])
                if slot_start_dt(entry) >= cutoff
            ],
        }
        for key, api_response in day_data.items()
    }


def assemble_days(
    responses: dict[str, dict[str, Any]],
    *,
    today: date,
    assembly: datetime | None = None,
    zero_fill_stale: bool = False,
    grace_days: int = 0,
    skip_leading_gaps: bool = False,
) -> tuple[list[tuple[date, DayData]], list[date]]:
    """Merge the range responses of every measurement type into days to import.

    Days with no data (the Enea API returned only null values) are handled
    depending on ``zero_fill_stale``:
    - ``False`` (default): the day is dropped.  The backward historical-boundary
      scan relies on that to detect where the meter's history starts.
    - ``True``: a day still within ``grace_days`` of ``today`` is dropped (it
      may simply not be published yet); an older day is treated as permanently
      missing and kept with zeroed values, so it does not leave a hole that
      corrupts the cumulative sum of later days (see MISSING_DAY_GRACE_DAYS).

    With ``skip_leading_gaps`` the days before the first one with data are
    dropped whatever ``zero_fill_stale`` says: there the meter's history in
    the Portal Odbiorcy Enea has not begun yet (a prosumer's balanced data
    starts long after the meter's assembly), so no day of it is missing.

    Days before the assembly of the active meter belong to the meters before
    it and are dropped; on the assembly day itself the hours before the
    assembly are (strip_pre_assembly_slots).

    Args:
        responses: Statistic key → raw range response, for every measurement
            type whose request succeeded.
        today: The local date the grace period counts from.
        assembly: Assembly moment of the active meter, if known.
        zero_fill_stale: Whether to zero-fill days confirmed missing.
        grace_days: How many days a day may stay missing before it is
            considered permanently absent (only used when zero_fill_stale).
        skip_leading_gaps: Whether the range may start before the history.

    Returns:
        The days to import in chronological order, and the days among them
        that were zero-filled.
    """
    per_key_days = {key: split_range_response(response) for key, response in responses.items()}
    all_dates: set[date] = set()
    for days_map in per_key_days.values():
        all_dates.update(days_map)
    if assembly is not None:
        all_dates = {day for day in all_dates if day >= assembly.date()}

    days: list[tuple[date, DayData]] = []
    zero_filled: list[date] = []
    history_started = not skip_leading_gaps
    for day in sorted(all_dates):
        day_data: DayData = {
            key: days_map[day] for key, days_map in per_key_days.items() if day in days_map
        }
        if any(has_data(response) for response in day_data.values()):
            history_started = True
        else:
            if not history_started or not zero_fill_stale or (today - day).days < grace_days:
                continue
            day_data = zero_fill_missing_day(day_data)
            zero_filled.append(day)
        days.append((day, strip_pre_assembly_slots(day, day_data, assembly)))
    return days, zero_filled
