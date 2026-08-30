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
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
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
from .statistics import get_statistic_id, has_data, slot_start_dt, sum_before

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

    # Days the tariff table does not reach; reported once at the end, because a
    # multi-year backfill would otherwise log a line per day.
    days_without_period: set[date] = set()

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
                days_without_period.add(day)
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

    if days_without_period:
        _LOGGER.warning(
            "No %s tariff period covers %d day(s) between %s and %s; their energy "
            "statistics were stored but no cost was computed for them",
            getattr(tariff, "name", "?"),
            len(days_without_period),
            min(days_without_period),
            max(days_without_period),
        )


async def _inject_cost_series(
    hass: HomeAssistant,
    meter_code: str,
    name: str,
    series: list[tuple[datetime, float]],
) -> float:
    """Inject cumulative PLN statistics for a single cost zone as an external statistic.

    Chains the running sum from the statistic entry immediately preceding
    series[0] (see sum_before) so that both fresh injection and re-injection
    produce correct values.  async_add_external_statistics uses INSERT OR
    REPLACE, so re-injecting an unchanged range is idempotent.

    Returns the final running sum after injection (PLN).
    """
    if not series:
        return 0.0

    statistic_id = get_statistic_id(meter_code, name)
    running_sum = await sum_before(hass, statistic_id, series[0][0])

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

    Enumerates statistic IDs from every zone the tariff has ever had and the
    enabled directions, asks the recorder for the newest entry of each, and
    returns the latest date found — or None when no cost statistics exist yet.

    The lookup must not be limited to a recent window, nor to the period valid
    today: reporting None while rows actually exist makes the caller restart
    injection from the meter assembly date, over a range that is already
    covered.  Zones come from all periods because the bundled tariff table ends
    on a fixed date, and past that date there is no current period at all.
    """
    zones = {zone for period in tariff.periods for zone in period.zones}

    stat_ids: list[str] = []
    for direction, enabled in (("pobrana", fetch_consumption), ("oddana", fetch_generation)):
        if not enabled:
            continue
        for zone in zones:
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
