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


async def sum_before(
    hass: HomeAssistant, statistic_id: str, moment: datetime
) -> float:
    """Return the cumulative sum of the newest statistic that starts before moment.

    Appending days after the newest stored entry is the common case and is
    answered by get_last_statistics alone.  Re-injecting a range that is already
    covered — what the backfill action does — is not: there the newest entry
    lies inside or after the range, so chaining from it would add the range's
    values on top of themselves and double the cumulative series.  The baseline
    is then looked up in the window preceding the range instead.

    That preceding lookup is deliberately unbounded at the start.  A fixed
    window is not safe: a fully missing day (the Enea API had no data at all)
    can push the gap for a zone-specific series — which already skips hours
    outside its zone — past any reasonable threshold, silently resetting the
    sum to 0 and producing a large bogus delta downstream.
    """
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    rows = last.get(statistic_id)
    if not rows:
        return 0.0

    newest_start = rows[0].get("start")
    if newest_start is not None and newest_start < moment.timestamp():
        return rows[0].get("sum") or 0.0

    preceding = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            dt_util.utc_from_timestamp(0),
            moment,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
    )
    before = preceding.get(statistic_id)
    if not before:
        return 0.0
    return before[-1].get("sum") or 0.0


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

    The cumulative running_sum is chained from the statistic immediately
    preceding series[0] (see sum_before), so that both fresh injection and
    re-injection (backfill overwrite) produce correct values without creating
    spikes.
    """
    if not series:
        return

    statistic_id = get_statistic_id(meter_code, name)
    running_sum = await sum_before(hass, statistic_id, series[0][0])

    stats_data = []
    for dt, value in series:
        running_sum += value
        stats_data.append(StatisticData(start=dt, state=value, sum=running_sum))

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
    async_add_external_statistics(hass, metadata, stats_data)
    _LOGGER.debug("Injected %d energy stats for %s", len(stats_data), statistic_id)


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
