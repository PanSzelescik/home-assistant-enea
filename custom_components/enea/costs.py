"""Cost statistics injection for the Enea Energy Meter integration.

Injects hourly cumulative cost statistics (PLN) per tariff zone for consumed
and returned energy.  Requires the enea_prices integration to be configured
with a matching tariff — if it is not present, the function returns early and
no cost sensors are created.

Uses async_add_external_statistics (source=DOMAIN, statistic_id "enea:...")
exactly like the energy statistics.  Because external statistics are not bound
to a recorder entity, Home Assistant never compiles competing long-term rows
for the same statistic_id — which is what previously caused
"UNIQUE constraint failed: statistics.metadata_id, statistics.start_ts".
The Energy Dashboard can select the resulting "enea:..._koszt_..." statistic
under "entity tracking total costs" (it is listed by its PLN unit).
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
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .const import (
    COST_ZONE_DISPLAY,
    DOMAIN,
    ENEA_PRICES_DOMAIN,
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
    UNIT_COST,
)
from .statistics import get_statistic_id, has_data, slot_start_dt

_LOGGER = logging.getLogger(__name__)


def _get_cost_entries(
    hass: HomeAssistant, meter_code: str
) -> list[er.RegistryEntry]:
    """Return entity registry entries for cost sensors of the given meter."""
    registry = er.async_get(hass)
    prefix = f"enea-{meter_code}-koszt_"
    return [
        e
        for e in registry.entities.values()
        if e.platform == DOMAIN and (e.unique_id or "").startswith(prefix)
    ]


def get_cost_unique_id(meter_code: str, direction: str, zone: str) -> str:
    """Return the unique_id for a cost sensor entity."""
    return f"enea-{meter_code}-koszt_{direction}_{zone}"


def get_cost_statistic_name(direction: str, zone_str: str) -> str:
    """Return the human-readable name for a cost statistic.

    Matches the EneaCostSensor friendly name so the external statistic is shown
    under the same label (e.g. "Koszt energii pobrana – Dzień") in the Energy
    Dashboard cost picker.
    """
    zone_display = COST_ZONE_DISPLAY.get(zone_str, zone_str)
    return f"Koszt energii {direction} – {zone_display}"


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
    injected as an external statistic ("enea:..._koszt_...") mirroring the
    energy statistics.

    Returns {unique_id: running_sum} for the corresponding EneaCostSensor
    entities so the coordinator can populate their displayed state.

    Args:
        hass: The Home Assistant instance.
        meter_code: The meter identifier used to build statistic IDs.
        all_days: Chronologically sorted list of (date, data_dict) tuples as
                  returned by the coordinator's fetch helpers.
        tariff: A TariffGroup object from enea_prices (duck-typed, no hard
                import).
        fetch_consumption: Whether to inject costs for consumed energy.
        fetch_generation: Whether to inject costs for returned energy.
    """
    if not all_days:
        return {}

    result: dict[str, float] = {}

    for key, direction in (
        (STAT_KEY_ENERGY_CONSUMED, "pobrana"),
        (STAT_KEY_ENERGY_RETURNED, "oddana"),
    ):
        if key == STAT_KEY_ENERGY_CONSUMED and not fetch_consumption:
            continue
        if key == STAT_KEY_ENERGY_RETURNED and not fetch_generation:
            continue

        # {zone_str: [(dt, cost_pln)]} — each hour belongs to exactly one zone.
        series_by_zone: dict[str, list[tuple[datetime, float]]] = {}

        for day, data in all_days:
            api = data.get(key)
            if not api or not has_data(api):
                continue

            period = tariff.get_period_for_date(day)
            if period is None:
                continue

            for entry in api.get("values", []):
                dt = slot_start_dt(entry)
                zone = period.get_zone_at_hour(dt.hour, day=dt.date())
                if zone not in period.zones:
                    continue
                zone_str = str(zone)
                total_kwh = sum(
                    item.get("value") or 0.0
                    for item in entry.get("items", [])
                )
                cost = total_kwh * period.zones[zone].total_brutto
                series_by_zone.setdefault(zone_str, []).append((dt, cost))

        for zone_str, series in series_by_zone.items():
            name = get_cost_statistic_name(direction, zone_str)
            last_sum = await _inject_cost_series(hass, meter_code, name, series)
            result[get_cost_unique_id(meter_code, direction, zone_str)] = last_sum

    return result


async def _inject_cost_series(
    hass: HomeAssistant,
    meter_code: str,
    name: str,
    series: list[tuple[datetime, float]],
) -> float:
    """Inject cumulative PLN statistics for a single cost zone as an external statistic.

    Chains the running sum from the statistic entry immediately preceding
    series[0] so that both fresh injection and re-injection (backfill overwrite)
    produce correct values.  async_add_external_statistics uses INSERT OR
    REPLACE, making re-injection idempotent.

    Returns the final running sum after injection (PLN).
    """
    if not series:
        return 0.0

    statistic_id = get_statistic_id(meter_code, name)
    first_dt = series[0][0]

    # Look up the sum for the hour ending exactly at series[0] so we can chain
    # correctly even when overwriting an already-injected range.
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
    for dt, cost in series:
        running_sum += cost
        stats_data.append(StatisticData(start=dt, state=running_sum, sum=running_sum))

    metadata = StatisticMetaData(
        has_mean=False,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UNIT_COST,
        unit_class=None,
    )
    async_add_external_statistics(hass, metadata, stats_data)
    _LOGGER.debug(
        "Injected %d cost stats for %s (running sum: %.2f PLN)",
        len(stats_data),
        statistic_id,
        running_sum,
    )
    return running_sum


async def get_cost_stats(
    hass: HomeAssistant, meter_code: str
) -> tuple[date | None, dict[str, float]]:
    """Return current cost statistics summary for all cost sensors of this meter.

    Enumerates cost sensor entities in the registry, builds the corresponding
    external statistic_id ("enea:..._koszt_...") for each, queries the statistics
    DB over a 30-day lookback window, and returns:
      - latest_date: the most recent date for which any entry exists, or None
      - sums: {unique_id: last_sum} cumulative cost per zone (PLN)

    Used to check if cost injection is needed and to pre-populate
    coordinator.cost_sums without triggering a new injection.
    """
    entries = _get_cost_entries(hass, meter_code)
    if not entries:
        return None, {}

    prefix = f"enea-{meter_code}-koszt_"
    uids: list[str] = []
    stat_ids: list[str] = []
    for entry in entries:
        uid = entry.unique_id or ""
        direction, _, zone_str = uid[len(prefix):].partition("_")
        uids.append(uid)
        stat_ids.append(
            get_statistic_id(meter_code, get_cost_statistic_name(direction, zone_str))
        )

    now = dt_util.utcnow()
    all_stats_list = await asyncio.gather(*(
        get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            now - timedelta(days=30),
            now,
            {sid},
            "hour",
            None,
            {"sum"},
        )
        for sid in stat_ids
    ))

    latest: date | None = None
    sums: dict[str, float] = {}
    for uid, sid, stats in zip(uids, stat_ids, all_stats_list):
        records = stats.get(sid)
        if not records:
            continue
        # statistics_during_period returns ascending order, so the last record is
        # the newest and carries the highest cumulative sum.
        ts = records[-1].get("start")
        if ts is not None:
            d = (
                dt_util.utc_from_timestamp(ts)
                .astimezone(dt_util.DEFAULT_TIME_ZONE)
                .date()
            )
            if latest is None or d > latest:
                latest = d
        last_sum = records[-1].get("sum") or 0.0
        if last_sum:
            sums[uid] = last_sum
    return latest, sums
