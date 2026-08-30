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
import sys
from datetime import date, datetime
from functools import partial
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .const import (
    COST_ZONE_DISPLAY,
    DOMAIN,
    ENEA_PRICES_DOMAIN,
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
    UNIT_COST,
    VAT_RATE,
)
from .statistics import get_statistic_id, has_data, slot_start_dt

_LOGGER = logging.getLogger(__name__)


def get_cost_statistic_name(direction: str, zone_str: str) -> str:
    """Return the human-readable name for a cost statistic.

    Returns the human-readable label shown for the statistic in the Energy
    Dashboard cost picker (e.g. "Koszt energii pobrana – Dzień").
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
    wanted = tariff_name.casefold()
    for entry in hass.config_entries.async_entries(ENEA_PRICES_DOMAIN):
        configured = entry.data.get("tariff") or ""
        if configured.casefold() != wanted:
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
) -> None:
    """Inject hourly cumulative cost statistics (PLN) per zone.

    For each hour in all_days, determines the active tariff zone using the
    tariff schedule, multiplies the total kWh by the zone's brutto price
    (computed inline as `(energy + AKCYZA + total_distribution) × 1.23`)
    and accumulates the result into per-zone cost series.  Each series is then
    injected as an external statistic ("enea:..._koszt_...") mirroring the
    energy statistics.

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
        return

    _enea_prices_const = sys.modules.get("custom_components.enea_prices.const")
    akcyza: float = getattr(_enea_prices_const, "AKCYZA", 0.0)

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
                pricing = period.zones[zone]
                cost = total_kwh * round(
                    (pricing.energy + akcyza + pricing.total_distribution) * (1 + VAT_RATE), 4
                )
                series_by_zone.setdefault(zone_str, []).append((dt, cost))

        for zone_str, series in series_by_zone.items():
            name = get_cost_statistic_name(direction, zone_str)
            await _inject_cost_series(hass, meter_code, name, series)


async def _sum_before(
    hass: HomeAssistant, statistic_id: str, moment: datetime
) -> float:
    """Return the cumulative sum of the newest statistic that starts before moment.

    Appending days after the newest stored entry is the common case and is
    answered by get_last_statistics alone.  Re-injecting a range that is already
    covered — what the backfill action does — is not: there the newest entry
    lies inside or after the range, so chaining from it would add the range's
    cost on top of itself and double the cumulative series.  The baseline is
    then looked up in the window preceding the range instead.

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


async def _inject_cost_series(
    hass: HomeAssistant,
    meter_code: str,
    name: str,
    series: list[tuple[datetime, float]],
) -> float:
    """Inject cumulative PLN statistics for a single cost zone as an external statistic.

    Chains the running sum from the statistic entry immediately preceding
    series[0] (see _sum_before) so that both fresh injection and re-injection
    produce correct values.  async_add_external_statistics uses INSERT OR
    REPLACE, so re-injecting an unchanged range is idempotent.

    Returns the final running sum after injection (PLN).
    """
    if not series:
        return 0.0

    statistic_id = get_statistic_id(meter_code, name)
    running_sum = await _sum_before(hass, statistic_id, series[0][0])

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


async def async_get_cost_latest_date(
    hass: HomeAssistant,
    meter_code: str,
    tariff: Any,
    fetch_consumption: bool,
    fetch_generation: bool,
) -> date | None:
    """Return the most recent date for which cost statistics exist for this meter.

    Enumerates statistic IDs from the current tariff period and enabled
    directions, asks the recorder for the newest entry of each, and returns the
    latest date found — or None when no cost statistics exist yet.

    The lookup must not be limited to a recent window: reporting None while rows
    actually exist makes the caller restart injection from the meter assembly
    date, over a range that is already covered.
    """
    period = tariff.get_current_period()
    if period is None:
        return None

    stat_ids: list[str] = []
    for direction, enabled in (("pobrana", fetch_consumption), ("oddana", fetch_generation)):
        if not enabled:
            continue
        for zone in period.zones:
            stat_ids.append(
                get_statistic_id(meter_code, get_cost_statistic_name(direction, str(zone)))
            )

    if not stat_ids:
        return None

    all_stats_list = await asyncio.gather(*(
        get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, sid, True, {"sum"}
        )
        for sid in stat_ids
    ))

    latest: date | None = None
    for sid, stats in zip(stat_ids, all_stats_list):
        records = stats.get(sid)
        if not records:
            continue
        ts = records[0].get("start")
        if ts is not None:
            d = (
                dt_util.utc_from_timestamp(ts)
                .astimezone(dt_util.DEFAULT_TIME_ZONE)
                .date()
            )
            if latest is None or d > latest:
                latest = d
    return latest
