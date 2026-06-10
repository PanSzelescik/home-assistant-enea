"""Bill estimation for the Enea Energy Meter integration.

Computes an estimated electricity bill for a date period (start, end] using:
- kWh consumed per tariff zone, read from long-term external statistics
  (cumulative sum difference at the period boundaries).
- Per-zone variable costs (energy + distribution, brutto) from enea_prices.
- Fixed monthly fees (network, subscription, capacity, brutto) for whole months.

Requires the enea_prices integration to be configured with a matching tariff.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .const import (
    COST_ZONE_DISPLAY,
    ENEA_PRICES_DOMAIN,
    VAT_RATE,
)
from .statistics import get_statistic_id

_LOGGER = logging.getLogger(__name__)


@dataclass
class PricesConfig:
    """Runtime configuration from the enea_prices integration (duck-typed)."""

    tariff: Any
    phases: int
    annual_kwh: int
    billing_months: int


@dataclass
class BillEstimate:
    """Estimated electricity bill for the period (start, end]."""

    kwh_by_zone: dict[str, float]
    """Consumed kWh per zone display name (e.g. 'Dzień', 'Noc')."""
    cost_by_zone: dict[str, float]
    """Variable cost brutto per zone display name (PLN)."""
    cost_by_zone_netto: dict[str, float]
    """Variable cost netto per zone display name (PLN)."""
    variable_cost: float
    """Energy + distribution cost, brutto (PLN)."""
    variable_cost_netto: float
    """Energy + distribution cost, netto (PLN)."""
    fixed_network_netto: float
    """Fixed network fee, netto (PLN), for `months` full months."""
    fixed_network: float
    """Fixed network fee, brutto (PLN), for `months` full months."""
    fixed_subscription_netto: float
    """Subscription fee, netto (PLN), for `months` full months."""
    fixed_subscription: float
    """Subscription fee, brutto (PLN), for `months` full months."""
    fixed_capacity_netto: float
    """Capacity fee, netto (PLN), for `months` full months."""
    fixed_capacity: float
    """Capacity fee, brutto (PLN), for `months` full months."""
    fixed_cost: float
    """Total fixed monthly fees, brutto (PLN) — sum of network + subscription + capacity."""
    months: int
    total: float
    total_netto: float
    start: date
    """Period start (exclusive) — day of the previous meter reading."""
    end: date
    """Period end (inclusive) — day of the current meter reading."""


def find_prices_config(hass: HomeAssistant, tariff_name: str | None) -> PricesConfig | None:
    """Return PricesConfig from the matching enea_prices entry, or None.

    Uses duck typing on entry.runtime_data to avoid a hard import dependency
    on the enea_prices package.
    """
    if not tariff_name:
        return None
    for entry in hass.config_entries.async_entries(ENEA_PRICES_DOMAIN):
        if entry.data.get("tariff") != tariff_name:
            continue
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            continue
        tariff = getattr(runtime, "tariff", None)
        if tariff is None:
            continue
        return PricesConfig(
            tariff=tariff,
            phases=getattr(runtime, "phases", 1),
            annual_kwh=getattr(runtime, "annual_kwh", 1200),
            billing_months=getattr(runtime, "billing_months", 1),
        )
    return None


async def async_estimate_bill(
    hass: HomeAssistant,
    meter_code: str,
    cfg: PricesConfig,
    start: date,
    end: date,
) -> BillEstimate | None:
    """Estimate the electricity bill for the period (start, end].

    kWh per zone = difference of cumulative statistics sums at the end of
    start-day and end-day respectively (convention: start exclusive, end
    inclusive — verified against actual meter readings, see data/verify.json).

    Args:
        hass: Home Assistant instance.
        meter_code: Meter identifier used to locate external statistics.
        cfg: Prices configuration from the matching enea_prices entry.
        start: Day of the previous reading (exclusive boundary).
        end: Day of the current reading (inclusive boundary).

    Returns:
        BillEstimate or None if period is empty or statistics are unavailable.
    """
    if end <= start:
        return None

    period = cfg.tariff.get_period_for_date(end)
    if period is None:
        _LOGGER.debug("No tariff period found for %s", end)
        return None

    # Map each tariff zone to its external statistics ID
    zone_stat_ids: dict[str, str] = {}
    for zone in period.zones:
        zone_display = COST_ZONE_DISPLAY.get(str(zone), str(zone))
        stat_name = f"Energia pobrana – {zone_display}"
        zone_stat_ids[zone_display] = get_statistic_id(meter_code, stat_name)

    kwh_by_zone = await _query_zone_kwh(hass, zone_stat_ids, start, end)

    cost_by_zone: dict[str, float] = {}
    cost_by_zone_netto: dict[str, float] = {}
    for zone in period.zones:
        zone_display = COST_ZONE_DISPLAY.get(str(zone), str(zone))
        kwh = kwh_by_zone.get(zone_display, 0.0)
        brutto = round(kwh * period.zones[zone].total_brutto, 2)
        cost_by_zone[zone_display] = brutto
        cost_by_zone_netto[zone_display] = round(brutto / (1 + VAT_RATE), 2)
    variable_cost = sum(cost_by_zone.values())
    variable_cost_netto = sum(cost_by_zone_netto.values())

    days = (end - start).days
    months = max(1, round(days / 30.44)) if days > 0 else 0
    vat = 1 + VAT_RATE
    if months == 0:
        fixed_network_netto = fixed_network = 0.0
        fixed_subscription_netto = fixed_subscription = 0.0
        fixed_capacity_netto = fixed_capacity = 0.0
        fixed_cost = 0.0
    else:
        m = period.monthly
        fixed_network_netto = round(m.get_network_fixed(cfg.phases) * months, 2)
        fixed_network = round(fixed_network_netto * vat, 2)
        fixed_subscription_netto = round(m.get_subscription(cfg.billing_months) * months, 2)
        fixed_subscription = round(fixed_subscription_netto * vat, 2)
        fixed_capacity_netto = round(m.get_capacity(cfg.annual_kwh) * months, 2)
        fixed_capacity = round(fixed_capacity_netto * vat, 2)
        fixed_cost = round(fixed_network + fixed_subscription + fixed_capacity, 2)

    total = round(variable_cost + fixed_cost, 2)

    return BillEstimate(
        kwh_by_zone=kwh_by_zone,
        cost_by_zone=cost_by_zone,
        cost_by_zone_netto=cost_by_zone_netto,
        variable_cost=round(variable_cost, 2),
        variable_cost_netto=round(variable_cost_netto, 2),
        fixed_network_netto=fixed_network_netto,
        fixed_network=fixed_network,
        fixed_subscription_netto=fixed_subscription_netto,
        fixed_subscription=fixed_subscription,
        fixed_capacity_netto=fixed_capacity_netto,
        fixed_capacity=fixed_capacity,
        fixed_cost=fixed_cost,
        months=months,
        total=total,
        total_netto=round(total / vat, 2),
        start=start,
        end=end,
    )


async def _query_zone_kwh(
    hass: HomeAssistant,
    zone_stat_ids: dict[str, str],
    d1: date,
    d2: date,
) -> dict[str, float]:
    """Return kWh consumed per zone in period (d1, d2].

    Computes the difference of cumulative external statistics sums:
      kWh = sum_at_last_slot_of_d2 – sum_at_last_slot_of_d1

    Queries all zone statistics in parallel.  Records are iterated in ascending
    time order; the last record on d1 (resp. d2) gives the cumulative sum at
    the end of that day, regardless of which hour zone slots fall on.
    """
    start_dt = dt_util.start_of_local_day(d1)
    end_dt = dt_util.start_of_local_day(d2 + timedelta(days=1))
    tz = dt_util.DEFAULT_TIME_ZONE

    all_stats = await asyncio.gather(*(
        get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start_dt,
            end_dt,
            {sid},
            "hour",
            None,
            {"sum"},
        )
        for sid in zone_stat_ids.values()
    ))

    result: dict[str, float] = {}
    for zone_display, sid, stats_result in zip(
        zone_stat_ids.keys(), zone_stat_ids.values(), all_stats
    ):
        records = stats_result.get(sid, [])
        if not records:
            _LOGGER.debug("No statistics found for %s in period (%s, %s]", sid, d1, d2)
            result[zone_display] = 0.0
            continue

        sum_at_d1 = 0.0
        sum_at_d2 = 0.0
        for rec in records:
            rec_date = (
                dt_util.utc_from_timestamp(rec["start"])
                .astimezone(tz)
                .date()
            )
            if rec_date <= d1:
                sum_at_d1 = rec.get("sum") or 0.0
            if rec_date <= d2:
                sum_at_d2 = rec.get("sum") or 0.0

        result[zone_display] = max(0.0, round(sum_at_d2 - sum_at_d1, 3))

    return result
