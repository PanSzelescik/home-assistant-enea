"""Statistics injection for the Enea Energy Meter integration."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.helpers.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    DOMAIN,
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
    STAT_KEY_POWER_CONSUMED,
    STAT_KEY_POWER_RETURNED,
)

_LOGGER = logging.getLogger(__name__)

EPOCH = dt_util.utc_from_timestamp(0)
"""Lower bound for a lookup that must not miss anything, however old."""


def get_statistic_id(meter_code: str, name: str) -> str:
    """Return the external statistic_id for an energy/power statistic."""
    return f"{DOMAIN}:{meter_code}_{slugify(name)}"


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


async def _newest_entry(
    hass: HomeAssistant, statistic_id: str
) -> tuple[datetime | None, float]:
    """Return the start and running total of the newest stored entry.

    Returns (None, 0.0) when the statistic has nothing stored at all.
    """
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    rows = last.get(statistic_id)
    if not rows or rows[0].get("start") is None:
        return None, 0.0
    return dt_util.utc_from_timestamp(rows[0]["start"]), rows[0].get("sum") or 0.0


async def _stored_between(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime | None
) -> list[dict[str, Any]]:
    """Return the stored entries in [start, end), oldest first.

    An end of None asks for everything from start onwards.
    """
    found = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            start,
            end,
            {statistic_id},
            "hour",
            None,
            {"sum", "state"},
        )
    )
    return found.get(statistic_id) or []


async def write_cumulative_series(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    series: list[tuple[datetime, float]],
    state_is_running_total: bool = False,
) -> float:
    """Write hourly values as a cumulative statistic and return the closing total.

    The energy and the cost statistics are both running totals over hourly
    values and differ only in what each hour reports as its own state: energy
    reports the kWh read for that hour, cost reports the total so far.  Keeping
    them in one place keeps the rule that holds the running total together
    across a re-import in one place too.

    The opening total is taken from the entry immediately before the range, not
    from the newest entry of the whole series.  Re-writing a range that is
    already stored is what the backfill action normally does, and there the
    newest entry lies inside or after the range, so counting on from it would
    add the range to itself.  That preceding lookup is deliberately unbounded:
    a zone series only holds the hours in its own zone, and a day the portal
    never published widens the gap further, so any fixed window would sooner or
    later find nothing, silently restart the total at zero and report the
    meter's whole history as one hour's consumption.
    """
    statistic_id = metadata["statistic_id"]
    first, last = series[0][0], series[-1][0]
    newest_start, newest_total = await _newest_entry(hass, statistic_id)

    if newest_start is None:
        opening = closing_before = 0.0
    elif newest_start < first:
        # Appending past everything stored: the newest entry is the one to count
        # on from, and nothing follows the range that could fall out of step.
        opening, closing_before = newest_total, 0.0
    else:
        # The range is already covered.  One window answers both questions: what
        # the total was going into the range, and what it was coming out of it.
        stored = await _stored_between(
            hass, statistic_id, EPOCH, last + timedelta(hours=1)
        )
        before = [row for row in stored if row["start"] < first.timestamp()]
        opening = (before[-1].get("sum") or 0.0) if before else 0.0
        closing_before = (stored[-1].get("sum") or 0.0) if stored else 0.0

    total = opening
    rows = []
    for moment, value in series:
        total += value
        rows.append(
            StatisticData(
                start=moment,
                state=total if state_is_running_total else value,
                sum=total,
            )
        )
    async_add_external_statistics(hass, metadata, rows)

    if newest_start is not None and newest_start > last:
        await _shift_later_totals(
            hass, metadata, last, total - closing_before, state_is_running_total
        )
    return total


async def _shift_later_totals(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    boundary: datetime,
    difference: float,
    state_is_running_total: bool,
) -> None:
    """Add difference to the running total of every entry stored after boundary.

    async_add_external_statistics only rewrites the entries it is handed.  When
    a range is written again with different values — a day the portal had not
    published yet, stored as zeroes, that has since appeared — the entries after
    it keep a total counted on from the old values, so the series steps down
    where the two meet and Home Assistant reads that as a meter reset.

    Every later hour's own value is unaffected by what happened before it, so
    the whole tail moves by one and the same amount, which is what the range's
    closing total gained.  Adding it back is exactly what recomputing the tail
    from scratch would produce, without fetching any of it again.
    """
    if not difference:
        return

    later = await _stored_between(
        hass, metadata["statistic_id"], boundary + timedelta(hours=1), None
    )
    if not later:
        return

    rows = []
    for row in later:
        total = (row.get("sum") or 0.0) + difference
        rows.append(
            StatisticData(
                start=dt_util.utc_from_timestamp(row["start"]),
                state=total if state_is_running_total else row.get("state"),
                sum=total,
            )
        )
    async_add_external_statistics(hass, metadata, rows)
    _LOGGER.info(
        "Re-import changed %s: %d later entries moved by %.3f to keep the "
        "running total continuous",
        metadata["statistic_id"],
        len(rows),
        difference,
    )


async def async_insert_historical_statistics(
    hass: HomeAssistant,
    meter_code: str,
    all_days: list[tuple[date, dict[str, Any]]],
) -> None:
    """Inject hourly historical statistics for one or more days.

    Args:
        hass: The Home Assistant instance.
        meter_code: The meter identifier used to build statistic IDs.
        all_days: List of (date, data_dict) tuples sorted chronologically.
                  data_dict keys: "energy_consumed", "energy_returned",
                                  "power_consumed", "power_returned".
    """
    if not all_days:
        return

    for key, type_label, prefix, inject_fn in (
        (STAT_KEY_ENERGY_CONSUMED, "pobrana", "Energia", _inject_energy_series),
        (STAT_KEY_ENERGY_RETURNED, "oddana", "Energia", _inject_energy_series),
        (STAT_KEY_POWER_CONSUMED, "pobrana", "Moc", _inject_power_series),
        (STAT_KEY_POWER_RETURNED, "oddana", "Moc", _inject_power_series),
    ):
        series_by_name: dict[str, list[tuple[datetime, float]]] = {}
        for _, data in all_days:
            api = data.get(key)
            if not api or not has_data(api):
                continue
            _collect_series(api, type_label, prefix, series_by_name)
        for name, series in series_by_name.items():
            await inject_fn(hass, meter_code, name, series)


def _collect_series(
    api_response: dict[str, Any],
    type_label: str,
    prefix: str,
    series_by_name: dict[str, list[tuple[datetime, float]]],
) -> None:
    """Append one day's time series into series_by_name (mutates in place)."""
    zones: dict[int, str] = {z["id"]: z["name"] for z in api_response.get("zones", [])}
    total_name = f"{prefix} {type_label}"
    zone_names: dict[int, str] = {
        zone_id: f"{prefix} {type_label} \u2013 {zone_name}"
        for zone_id, zone_name in zones.items()
    }

    for entry in api_response.get("values", []):
        dt = slot_start_dt(entry)
        slot_total = 0.0
        for item in entry.get("items", []):
            zone_id = item.get("tarifZoneId")
            value = item.get("value") or 0.0
            slot_total += value
            if zone_id in zone_names:
                series_by_name.setdefault(zone_names[zone_id], []).append((dt, value))
        series_by_name.setdefault(total_name, []).append((dt, slot_total))


async def _inject_energy_series(
    hass: HomeAssistant,
    meter_code: str,
    name: str,
    series: list[tuple[datetime, float]],
) -> None:
    """Inject an energy time series, always overwriting the given range.

    Each hour reports the kWh read for it; the cumulative sum is handled by
    write_cumulative_series.
    """
    if not series:
        return

    statistic_id = get_statistic_id(meter_code, name)
    metadata = StatisticMetaData(
        has_mean=False,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        unit_class="energy",
    )
    await write_cumulative_series(hass, metadata, series)
    _LOGGER.debug("Injected %d energy stats for %s", len(series), statistic_id)


async def _inject_power_series(
    hass: HomeAssistant,
    meter_code: str,
    name: str,
    series: list[tuple[datetime, float]],
) -> None:
    """Inject a power time series as hourly mean values."""
    if not series:
        return

    statistic_id = get_statistic_id(meter_code, name)
    stats_data = [StatisticData(start=dt, mean=value) for dt, value in series]

    metadata = StatisticMetaData(
        has_mean=True,
        mean_type=StatisticMeanType.ARITHMETIC,
        has_sum=False,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfPower.KILO_WATT,
        unit_class="power",
    )
    async_add_external_statistics(hass, metadata, stats_data)
    _LOGGER.debug("Injected %d power stats for %s", len(stats_data), statistic_id)
