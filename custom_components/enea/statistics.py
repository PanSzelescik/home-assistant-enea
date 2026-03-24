"""Statistics injection for the Enea Energy Meter integration."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.helpers.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
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


def time_id_to_dt(stats_date: date, time_id: int) -> datetime:
    """Convert 1-based timeId (60-min resolution) to a timezone-aware datetime.

    timeId=1  → 00:00 local time on stats_date
    timeId=24 → 23:00 local time on stats_date
    """
    midnight = datetime.combine(stats_date, time(0, 0), tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return midnight + timedelta(hours=time_id - 1)


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

    # Process energy (kWh) — requires cumulative sum chaining
    for key, type_label in (
        (STAT_KEY_ENERGY_CONSUMED, "pobrana"),
        (STAT_KEY_ENERGY_RETURNED, "oddana"),
    ):
        series_by_name: dict[str, list[tuple[datetime, float]]] = {}

        for day, data in all_days:
            api = data.get(key)
            if not api or not has_data(api):
                continue
            _collect_series(api, day, type_label, "Energia", series_by_name)

        for name, series in series_by_name.items():
            await _inject_energy_series(hass, meter_code, name, series)

    # Process power (kW) — mean values, no cumulative sum
    for key, type_label in (
        (STAT_KEY_POWER_CONSUMED, "pobrana"),
        (STAT_KEY_POWER_RETURNED, "oddana"),
    ):
        series_by_name = {}

        for day, data in all_days:
            api = data.get(key)
            if not api or not has_data(api):
                continue
            _collect_series(api, day, type_label, "Moc", series_by_name)

        for name, series in series_by_name.items():
            _inject_power_series(hass, meter_code, name, series)


def _collect_series(
    api_response: dict[str, Any],
    stats_date: date,
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
        dt = time_id_to_dt(stats_date, entry["timeId"])
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

    The cumulative running_sum is chained from the stat immediately preceding
    series[0] so that both fresh injection and re-injection (backfill overwrite)
    produce correct values without creating spikes.
    """
    if not series:
        return

    statistic_id = get_statistic_id(meter_code, name)
    first_dt = series[0][0]

    # Look up the sum for the hour that ends exactly at series[0] so we can
    # chain correctly even when overwriting already-injected data.
    base_stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first_dt - timedelta(hours=1),
        first_dt,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    running_sum: float = 0.0
    if base_stats.get(statistic_id):
        running_sum = base_stats[statistic_id][-1].get("sum") or 0.0

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


def _inject_power_series(
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
