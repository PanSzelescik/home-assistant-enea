"""Bill estimation for the Enea Energy Meter integration.

Computes an estimated electricity bill for a date period (start, end] using the
same calculation method as Enea's invoices:

1. kWh per zone is taken directly from long-term statistics (precise, not rounded).
2. Every line item is multiplied and rounded to 2 decimal places at **netto**
   (pre-VAT) prices.  The bill is split into two sections mirroring the invoice:
   - Sprzedaż energii – energy price including the excise duty (akcyza).
   - Usługa dystrybucji – variable distribution fees per zone (grid, quality,
     OZE, cogeneration) plus fixed monthly fees (network, capacity, subscription).
3. VAT (23%) is applied **once** to the total netto at the very end:
   total = round(total_netto × 1.23, 2).

Requires the enea_prices integration to be configured with a matching tariff.
"""
from __future__ import annotations

import asyncio
import logging
import sys
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
    akcyza: float


@dataclass
class BillEstimate:
    """Estimated electricity bill for the period (start, end].

    All monetary amounts are netto (pre-VAT) except ``total``, which is the
    final brutto amount (total_netto × 1.23) shown on the invoice.
    """

    kwh_by_zone: dict[str, float]
    """Consumed kWh per zone (precise, from long-term statistics)."""

    energy_by_zone_netto: dict[str, float]
    """Energy sale cost netto per zone (energy price + akcyza), PLN."""

    variable_network_by_zone_netto: dict[str, float]
    """Variable network fee netto per zone (składnik zmienny stawki sieciowej), PLN."""

    quality_by_zone_netto: dict[str, float]
    """Quality fee netto per zone (opłata jakościowa), PLN."""

    oze_by_zone_netto: dict[str, float]
    """OZE fee netto per zone (opłata OZE), PLN."""

    cogeneration_by_zone_netto: dict[str, float]
    """Cogeneration fee netto per zone (opłata kogeneracyjna), PLN."""

    energy_netto: float
    """Total energy sale cost netto — section 'Sprzedaż energii', PLN."""

    distribution_netto: float
    """Total distribution service cost netto — section 'Usługa dystrybucji'
    (variable fees across all zones + fixed monthly fees), PLN."""

    fixed_network_netto: float
    """Fixed network fee netto for ``months`` full months, PLN."""

    fixed_capacity_netto: float
    """Capacity fee netto for ``months`` full months, PLN."""

    fixed_subscription_netto: float
    """Subscription fee netto for ``months`` full months, PLN."""

    total_netto: float
    """Grand total netto (energy + distribution), PLN."""

    total: float
    """Grand total brutto = round(total_netto × 1.23, 2), PLN.
    This is the only brutto value — VAT is applied once at the end."""

    months: int
    """Number of full billing months in the period."""

    start: date
    """Period start (exclusive) — day of the previous meter reading."""

    end: date
    """Period end (inclusive) — day of the current meter reading."""


def find_prices_config(hass: HomeAssistant, tariff_name: str | None) -> PricesConfig | None:
    """Return PricesConfig from the matching enea_prices entry, or None.

    Uses duck typing on entry.runtime_data to avoid a hard import dependency
    on the enea_prices package.  AKCYZA is read from the already-loaded
    enea_prices.const module via sys.modules (the module is in memory whenever
    a config entry for the integration exists).
    """
    if not tariff_name:
        return None
    wanted = tariff_name.casefold()
    for entry in hass.config_entries.async_entries(ENEA_PRICES_DOMAIN):
        configured = entry.data.get("tariff") or ""
        if configured.casefold() != wanted:
            continue
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            continue
        tariff = getattr(runtime, "tariff", None)
        if tariff is None:
            continue
        enea_prices_const = sys.modules.get("custom_components.enea_prices.const")
        return PricesConfig(
            tariff=tariff,
            phases=getattr(runtime, "phases", 1),
            annual_kwh=getattr(runtime, "annual_kwh", 1200),
            billing_months=getattr(runtime, "billing_months", 1),
            akcyza=getattr(enea_prices_const, "AKCYZA", 0.0),
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

    Mirrors the calculation method used on Enea invoices:
    - kWh per zone is taken directly from long-term statistics (precise, not rounded).
    - Each line item is rounded to 2 decimal places at netto prices.
    - Variable distribution fees per zone are summed from four components
      rounded individually (variable_network, quality, oze, cogeneration).
    - Fixed monthly fees are rounded individually.
    - VAT (23%) is applied once to the grand total netto at the very end.

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

    energy_by_zone_netto: dict[str, float] = {}
    variable_network_by_zone_netto: dict[str, float] = {}
    quality_by_zone_netto: dict[str, float] = {}
    oze_by_zone_netto: dict[str, float] = {}
    cogeneration_by_zone_netto: dict[str, float] = {}

    for zone in period.zones:
        zone_display = COST_ZONE_DISPLAY.get(str(zone), str(zone))
        kwh = kwh_by_zone.get(zone_display, 0)
        pricing = period.zones[zone]

        # Sprzedaż energii: energia netto = (cena energii + akcyza) × kWh
        energy_by_zone_netto[zone_display] = round(kwh * (pricing.energy + cfg.akcyza), 2)

        # Usługa dystrybucji (zmienne): każdy składnik zaokrąglony osobno jak na fakturze
        variable_network_by_zone_netto[zone_display] = round(kwh * pricing.variable_network, 2)
        quality_by_zone_netto[zone_display] = round(kwh * pricing.quality, 2)
        oze_by_zone_netto[zone_display] = round(kwh * pricing.oze, 2)
        cogeneration_by_zone_netto[zone_display] = round(kwh * pricing.cogeneration, 2)

    energy_netto = round(sum(energy_by_zone_netto.values()), 2)

    days = (end - start).days
    months = max(1, round(days / 30.44)) if days > 0 else 0

    if months == 0:
        fixed_network_netto = 0.0
        fixed_capacity_netto = 0.0
        fixed_subscription_netto = 0.0
    else:
        m = period.monthly
        fixed_network_netto = round(m.get_network_fixed(cfg.phases) * months, 2)
        fixed_capacity_netto = round(m.get_capacity(cfg.annual_kwh) * months, 2)
        fixed_subscription_netto = round(m.get_subscription(cfg.billing_months) * months, 2)

    distribution_netto = round(
        sum(variable_network_by_zone_netto.values())
        + sum(quality_by_zone_netto.values())
        + sum(oze_by_zone_netto.values())
        + sum(cogeneration_by_zone_netto.values())
        + fixed_network_netto
        + fixed_capacity_netto
        + fixed_subscription_netto,
        2,
    )

    total_netto = round(energy_netto + distribution_netto, 2)
    total = round(total_netto * (1 + VAT_RATE), 2)

    return BillEstimate(
        kwh_by_zone=kwh_by_zone,
        energy_by_zone_netto=energy_by_zone_netto,
        variable_network_by_zone_netto=variable_network_by_zone_netto,
        quality_by_zone_netto=quality_by_zone_netto,
        oze_by_zone_netto=oze_by_zone_netto,
        cogeneration_by_zone_netto=cogeneration_by_zone_netto,
        energy_netto=energy_netto,
        distribution_netto=distribution_netto,
        fixed_network_netto=fixed_network_netto,
        fixed_capacity_netto=fixed_capacity_netto,
        fixed_subscription_netto=fixed_subscription_netto,
        total_netto=total_netto,
        total=total,
        months=months,
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
