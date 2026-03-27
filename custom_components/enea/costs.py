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

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
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
from .statistics import time_id_to_dt, has_data

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
                dt = time_id_to_dt(day, entry["timeId"])
                zone = period.get_zone_at_hour(dt.hour, day=day)
                zone_str = str(zone)

                total_kwh = sum(
                    item.get("value") or 0.0
                    for item in entry.get("items", [])
                )
                # Compute cost for the actual zone; other zones get 0.0 for this hour.
                # Every zone receives an entry for every hour of the day so that
                # async_import_statistics overwrites HA auto-recorder entries that
                # would otherwise corrupt the running sum with sum=0 values.
                actual_cost = (
                    total_kwh * period.zones[zone].total_brutto
                    if zone_str in allowed_zone_strs
                    else 0.0
                )
                for z_str in allowed_zone_strs:
                    series_by_zone.setdefault(z_str, []).append(
                        (dt, actual_cost if z_str == zone_str else 0.0)
                    )

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

    Chains the running sum from the last correctly injected entry before
    series[0].  Uses statistics_during_period with a 30-day lookback and
    takes max(sum) so that auto-recorder entries written by HA's recorder
    (sum=0 for a constant sensor state) do not corrupt the chain point —
    our injected entries always have a strictly positive and increasing sum.

    Does not filter the input series: async_import_statistics uses
    INSERT OR REPLACE, making re-injection idempotent, so there is no risk
    of double-counting when the same date range is injected more than once.

    Returns the final running sum after injection (PLN).
    """
    if not series:
        return 0.0

    first_dt = series[0][0]

    # Find the correct chain point: the highest cumulative sum recorded before
    # our first entry.  HA's recorder writes sum=0 when the sensor value is
    # constant (no new injections); our entries always have a growing sum.
    # max() therefore picks our last real injection value over the HA noise.
    base_stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first_dt - timedelta(days=30),
        first_dt,
        {entity_id},
        "hour",
        None,
        {"sum"},
    )
    running_sum: float = 0.0
    if base_stats.get(entity_id):
        running_sum = max(
            (r.get("sum") or 0.0 for r in base_stats[entity_id]),
            default=0.0,
        )

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


async def async_inject_today_cost_bridge(
    hass: HomeAssistant,
    meter_code: str,
    fetch_consumption: bool,
    fetch_generation: bool,
) -> None:
    """Inject zero-cost placeholder entries for all hours of today up to now.

    Called when energy statistics are up to date but today's cost statistics
    contain HA auto-recorder entries with incorrect sum=0 values (written while
    the sensor was in a wrong state after restart or bug recovery).  Zero-cost
    entries keep the running sum constant through the day so the Energy
    Dashboard shows 0 PLN cost until real energy data is injected next day.

    Safe to call on every coordinator refresh — _inject_cost_series uses
    INSERT OR REPLACE so the operation is idempotent.
    """
    registry = er.async_get(hass)
    prefix = f"enea-{meter_code}-koszt_"
    entries = [
        e
        for e in registry.entities.values()
        if e.platform == DOMAIN and (e.unique_id or "").startswith(prefix)
    ]
    if not entries:
        return

    now = dt_util.now()
    today = now.date()
    # timeId N covers (N-1):00–N:00; include the currently-running hour.
    series: list[tuple[datetime, float]] = [
        (time_id_to_dt(today, tid), 0.0)
        for tid in range(1, now.hour + 2)
    ]

    for entry in entries:
        uid = entry.unique_id or ""
        if "pobrana" in uid and not fetch_consumption:
            continue
        if "oddana" in uid and not fetch_generation:
            continue
        await _inject_cost_series(hass, entry.entity_id, series)


async def get_cost_stats(
    hass: HomeAssistant, meter_code: str
) -> tuple[date | None, dict[str, float]]:
    """Return current cost statistics summary for all cost sensors of this meter.

    Scans the entity registry for cost sensors matching this meter, queries the
    statistics DB for each entity over a 30-day lookback window, and returns:
      - latest_date: the most recent date for which any entry exists, or None
      - sums: {unique_id: max_sum} where max_sum is the highest cumulative sum
              found in the lookback window

    Uses statistics_during_period with max(sum) so that auto-recorder entries
    (written by HA for a constant sensor state with sum=0) do not corrupt the
    coordinator.cost_sums used to display the sensor state.  Our injected entries
    always carry a strictly positive and growing sum, so max() reliably picks
    the last correctly injected value.

    Used to check if cost injection is needed and to pre-populate
    coordinator.cost_sums without triggering a new injection.
    """
    registry = er.async_get(hass)
    prefix = f"enea-{meter_code}-koszt_"
    entries = [
        e
        for e in registry.entities.values()
        if e.platform == DOMAIN and (e.unique_id or "").startswith(prefix)
    ]
    if not entries:
        return None, {}

    now = dt_util.utcnow()
    all_stats_list = await asyncio.gather(*(
        get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            now - timedelta(days=30),
            now,
            {e.entity_id},
            "hour",
            None,
            {"sum"},
        )
        for e in entries
    ))

    latest: date | None = None
    sums: dict[str, float] = {}
    for entry, stats in zip(entries, all_stats_list):
        records = stats.get(entry.entity_id)
        if not records:
            continue
        # Latest date from the most recent record (statistics_during_period returns
        # ascending order by start time, so the last element is the newest).
        ts = records[-1].get("start")
        if ts is not None:
            d = (
                dt_util.utc_from_timestamp(ts)
                .astimezone(dt_util.DEFAULT_TIME_ZONE)
                .date()
            )
            if latest is None or d > latest:
                latest = d
        # Correct cumulative sum: max() ignores HA auto-recorder entries (sum=0).
        best_sum = max(
            (r.get("sum") or 0.0 for r in records),
            default=0.0,
        )
        if best_sum > 0:
            sums[entry.unique_id or ""] = best_sum
    return latest, sums
