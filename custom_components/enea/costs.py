"""Cost statistics injection for the Enea Energy Meter integration.

Injects hourly cumulative cost statistics (PLN) per tariff zone for consumed
and returned energy.  Requires the enea_prices integration to be configured
with a matching tariff — if it is not present, the function returns early and
no cost sensors are created.

Uses async_import_statistics (source="recorder") so that the injected
statistics are tied to EneaCostSensor entities and visible in the
Energy Dashboard under "entity tracking total costs".
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    ENEA_PRICES_DOMAIN,
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
    UNIT_COST,
)
from .statistics import _time_id_to_dt, has_data

_LOGGER = logging.getLogger(__name__)


def get_cost_unique_id(meter_code: str, direction: str, zone: str) -> str:
    """Return the unique_id for a cost sensor entity."""
    return f"enea-{meter_code}-koszt_{direction}_{zone}"


def find_tariff_group(hass: HomeAssistant, tariff_name: str | None) -> Any | None:
    """Return the TariffGroup from enea_prices that matches tariff_name, or None.

    Uses duck typing on entry.runtime_data to avoid a hard import dependency
    on the enea_prices package.
    """
    if not tariff_name:
        return None
    for entry in hass.config_entries.async_entries(ENEA_PRICES_DOMAIN):
        if entry.data.get("tariff") != tariff_name:
            continue
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            tariff = getattr(runtime, "tariff", None)
            if tariff is not None:
                return tariff
    return None


async def async_insert_cost_statistics(
    hass: HomeAssistant,
    meter_code: str,
    all_days: list[tuple[date, dict[str, Any]]],
    tariff: Any,
    fetch_consumption: bool = True,
    fetch_generation: bool = True,
) -> dict[str, float]:
    """Inject hourly cumulative cost statistics (PLN) per zone.

    For each hour in all_days, determines the active tariff zone using the
    tariff schedule, multiplies the total kWh by the zone's total_brutto price
    and accumulates the result into per-zone cost series.  Each series is then
    injected as recorder statistics tied to the corresponding EneaCostSensor.

    Args:
        hass: The Home Assistant instance.
        meter_code: The meter identifier used to look up cost sensor entities.
        all_days: Chronologically sorted list of (date, data_dict) tuples as
                  returned by the coordinator's fetch helpers.
        tariff: A TariffGroup object from enea_prices (duck-typed, no hard
                import).
        fetch_consumption: Whether to inject costs for consumed energy.
        fetch_generation: Whether to inject costs for returned energy.
    """
    if not all_days:
        return {}

    registry = er.async_get(hass)
    result: dict[str, float] = {}

    for key, direction in (
        (STAT_KEY_ENERGY_CONSUMED, "pobrana"),
        (STAT_KEY_ENERGY_RETURNED, "oddana"),
    ):
        if key == STAT_KEY_ENERGY_CONSUMED and not fetch_consumption:
            continue
        if key == STAT_KEY_ENERGY_RETURNED and not fetch_generation:
            continue

        # {zone_str: [(dt, cost_pln)]}
        series_by_zone: dict[str, list[tuple[datetime, float]]] = {}

        for day, data in all_days:
            api = data.get(key)
            if not api or not has_data(api):
                continue

            period = tariff.get_period_for_date(day)
            if period is None:
                continue

            allowed_zone_strs = {str(z) for z in period.zones}
            for entry in api.get("values", []):
                dt = _time_id_to_dt(day, entry["timeId"])
                zone = period.get_zone_at_hour(dt.hour, day.weekday(), is_holiday=False)
                zone_str = str(zone)

                if zone_str not in allowed_zone_strs:
                    continue

                total_kwh = sum(
                    item.get("value") or 0.0
                    for item in entry.get("items", [])
                )
                price: float = period.zones[zone].total_brutto
                series_by_zone.setdefault(zone_str, []).append((dt, total_kwh * price))

        for zone_str, series in series_by_zone.items():
            unique_id = get_cost_unique_id(meter_code, direction, zone_str)
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id is None:
                _LOGGER.debug("Cost sensor not in registry: %s", unique_id)
                continue
            last_sum = await _inject_cost_series(hass, entity_id, series)
            result[unique_id] = last_sum

    return result


async def _inject_cost_series(
    hass: HomeAssistant,
    entity_id: str,
    series: list[tuple[datetime, float]],
) -> float:
    """Inject cumulative PLN statistics for a single cost sensor entity.

    Returns the final running sum after injection (PLN).
    """
    if not series:
        return 0.0

    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, entity_id, True, {"sum"}
    )
    running_sum: float = 0.0
    if last_stats.get(entity_id):
        record = last_stats[entity_id][0]
        running_sum = record.get("sum") or 0.0
        last_ts = record.get("start")
        if last_ts is not None:
            last_dt = dt_util.utc_from_timestamp(last_ts)
            series = [(dt, cost) for dt, cost in series if dt > last_dt]

    if not series:
        return running_sum

    stats_data = []
    for dt, cost in series:
        running_sum += cost
        stats_data.append(StatisticData(start=dt, state=cost, sum=running_sum))

    metadata = StatisticMetaData(
        has_mean=False,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=entity_id,
        unit_of_measurement=UNIT_COST,
        unit_class=None,
    )
    async_import_statistics(hass, metadata, stats_data)
    _LOGGER.debug(
        "Injected %d cost stats for %s (running sum: %.2f PLN)",
        len(stats_data),
        entity_id,
        running_sum,
    )
    return running_sum


async def get_cost_stats(
    hass: HomeAssistant, meter_code: str
) -> tuple[date | None, dict[str, float]]:
    """Return current cost statistics summary for all cost sensors of this meter.

    Scans the entity registry for cost sensors matching this meter, queries the
    statistics DB once per entity, and returns:
      - latest_date: the most recent date for which any cost stat exists, or None
      - sums: {unique_id: last_sum} for each sensor that has statistics

    Used to check if cost injection is needed and to pre-populate
    coordinator.cost_sums without triggering a new injection.
    """
    registry = er.async_get(hass)
    prefix = f"enea-{meter_code}-koszt_"
    latest: date | None = None
    sums: dict[str, float] = {}
    for entry in registry.entities.values():
        if entry.platform != DOMAIN:
            continue
        unique_id = entry.unique_id or ""
        if not unique_id.startswith(prefix):
            continue
        last = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, entry.entity_id, True, {"sum"}
        )
        if last.get(entry.entity_id):
            record = last[entry.entity_id][0]
            ts = record.get("start")
            if ts is not None:
                d = (
                    dt_util.utc_from_timestamp(ts)
                    .astimezone(dt_util.DEFAULT_TIME_ZONE)
                    .date()
                )
                if latest is None or d > latest:
                    latest = d
            s = record.get("sum")
            if s is not None:
                sums[unique_id] = s
    return latest, sums
