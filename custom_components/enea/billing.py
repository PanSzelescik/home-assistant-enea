"""Bill estimation for the Enea Energy Meter integration.

Computes an estimated electricity bill for a date period (start, end] using the
same calculation method as Enea's invoices:

1. kWh per zone come from the hourly energy statistics (precise, not rounded),
   each hour put in its zone by the tariff schedule and priced by its own day.
2. Every line item is multiplied and rounded to 2 decimal places at **netto**
   (pre-VAT) prices.  The bill is split into two sections mirroring the invoice:
   - Sprzedaż energii – energy price including the excise duty (akcyza).
   - Usługa dystrybucji – variable distribution fees per zone (grid, quality,
     OZE, cogeneration) plus fixed monthly fees (network, capacity, subscription).
3. VAT (23%) is applied **once** to the total netto at the very end:
   total = round(total_netto × 1.23, 2).
4. A prosumer under net metering (system opustów) pays energy and the variable
   distribution fees only for the consumption left after the returned energy
   times the ratio (0.8/0.7) is settled against it — see settle_net_metering.

Requires the enea_prices integration to be configured with a matching tariff.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .const import (
    COST_ZONE_DISPLAY,
    ENEA_PRICES_CONF_TARIFF,
    ENEA_PRICES_DOMAIN,
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
    STAT_NAME_BY_KEY,
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

    returned_kwh_by_zone: dict[str, float] = field(default_factory=dict)
    """Returned kWh per zone; empty without net metering."""

    billed_kwh_by_zone: dict[str, float] = field(default_factory=dict)
    """Consumed kWh per zone left to pay for after net metering; energy and the
    variable distribution fees are charged on it.  Empty without net metering."""

    net_metering_left_kwh: float | None = None
    """Net-metering credit left over in the period, kWh; None without net metering."""

    unpriced_kwh: float = 0.0
    """Consumed kWh in hours the tariff has no price for — days under a tariff
    group with no enea_prices entry.  Left out of every charge."""


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


def find_prices_entry(hass: HomeAssistant, tariff_name: str | None) -> ConfigEntry | None:
    """Return the enea_prices entry of a tariff group, loaded or not, matched as above.

    Unlike find_prices_config this does not need the entry to be set up: an
    entry that failed to load is still the user's configuration of the group.
    """
    if not tariff_name:
        return None
    wanted = tariff_name.casefold()
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(ENEA_PRICES_DOMAIN)
            if (entry.data.get(ENEA_PRICES_CONF_TARIFF) or "").casefold() == wanted
        ),
        None,
    )


def settle_net_metering(
    consumed: dict[str, float],
    returned: dict[str, float],
    ratio: float,
    order: list[str],
) -> tuple[dict[str, float], float]:
    """Return the consumed kWh per zone left to pay for and the credit left over.

    Follows the regulation on settling prosumers: each zone's returned energy
    times the ratio is first set against the consumption in the same zone, and
    whatever is left goes to the other zones, from the one with the highest
    variable network rate down (order).  Energy carried over from earlier
    periods is not counted.
    """
    billed: dict[str, float] = {}
    surplus = 0.0
    for zone, kwh in consumed.items():
        credit = returned.get(zone, 0.0) * ratio
        used = min(kwh, credit)
        billed[zone] = kwh - used
        surplus += credit - used
    for zone in order:
        taken = min(billed.get(zone, 0.0), surplus)
        billed[zone] = billed.get(zone, 0.0) - taken
        surplus -= taken
    return {zone: round(kwh, 3) for zone, kwh in billed.items()}, round(surplus, 3)


@dataclass
class _ZoneUsage:
    """kWh and unrounded netto charges of one zone, summed over the bill's hours."""

    kwh: float = 0.0
    energy: float = 0.0
    variable_network: float = 0.0
    quality: float = 0.0
    oze: float = 0.0
    cogeneration: float = 0.0
    variable_rate: float = 0.0
    """The zone's variable network rate, which orders where a net-metering surplus goes."""


def _zone_display(zone: Any) -> str:
    """Return the name a tariff zone is shown and keyed under."""
    return COST_ZONE_DISPLAY.get(str(zone), str(zone))


def _add_hours(
    usage: dict[str, _ZoneUsage],
    tariff: Any,
    hours: list[tuple[datetime, float]],
    akcyza: float,
) -> float:
    """Add each hour's kWh and charges to the zone the tariff puts it in.

    Each hour is priced by the tariff period of its own day, so a bill across a
    price change or a change of tariff group charges every part at its own
    prices.  Returns the kWh of hours the tariff has no price for.
    """
    unpriced = 0.0
    for start, kwh in hours:
        day = start.date()
        period = tariff.get_period_for_date(day)
        zone = period.get_zone_at_hour(start.hour, day=day) if period is not None else None
        pricing = period.zones.get(zone) if period is not None else None
        if pricing is None:
            unpriced += kwh
            continue
        zone_usage = usage.setdefault(_zone_display(zone), _ZoneUsage())
        zone_usage.kwh += kwh
        zone_usage.energy += kwh * (pricing.energy + akcyza)
        zone_usage.variable_network += kwh * pricing.variable_network
        zone_usage.quality += kwh * pricing.quality
        zone_usage.oze += kwh * pricing.oze
        zone_usage.cogeneration += kwh * pricing.cogeneration
        zone_usage.variable_rate = pricing.variable_network
    return unpriced


async def async_estimate_bill(
    hass: HomeAssistant,
    meter_code: str,
    cfg: PricesConfig,
    start: date,
    end: date,
    net_metering_ratio: float | None = None,
) -> BillEstimate | None:
    """Estimate the electricity bill for the period (start, end].

    Mirrors the calculation method used on Enea invoices:
    - kWh come from the hourly energy statistics, put in zones by the tariff's
      own schedule — not by the zone statistics the Portal Odbiorcy Enea names,
      which follow the meter's registers and lag behind a change of tariff
      group.  cfg.tariff may be a TariffHistory, so each hour is priced by the
      group of the agreement in force that day.
    - Each line item is rounded to 2 decimal places at netto prices.
    - Variable distribution fees per zone are summed from four components
      rounded individually (variable_network, quality, oze, cogeneration).
    - Fixed monthly fees, rounded individually, come from the period at end.
    - VAT (23%) is applied once to the grand total netto at the very end.

    Args:
        hass: Home Assistant instance.
        meter_code: Meter identifier used to locate external statistics.
        cfg: Prices configuration from the matching enea_prices entry.
        start: Day of the previous reading (exclusive boundary).
        end: Day of the current reading (inclusive boundary).
        net_metering_ratio: The prosumer's net-metering ratio (0.8 or 0.7), or
            None when the meter is not settled by net metering.

    Returns:
        BillEstimate or None if the period is empty or the tariff has no
        prices for its last day.
    """
    if end <= start:
        return None

    period = cfg.tariff.get_period_for_date(end)
    if period is None:
        _LOGGER.debug("No tariff period found for %s", end)
        return None

    # The zones of the period at end come first and always, so the attributes
    # keep their order and a zone with nothing used still shows 0.
    usage = {
        _zone_display(zone): _ZoneUsage(variable_rate=pricing.variable_network)
        for zone, pricing in period.zones.items()
    }
    consumed = await async_hourly_kwh(
        hass, get_statistic_id(meter_code, STAT_NAME_BY_KEY[STAT_KEY_ENERGY_CONSUMED]), start, end
    )
    unpriced_kwh = _add_hours(usage, cfg.tariff, consumed, cfg.akcyza)
    kwh_by_zone = {zone: round(zone_usage.kwh, 3) for zone, zone_usage in usage.items()}

    returned_kwh_by_zone: dict[str, float] = {}
    billed_kwh_by_zone = kwh_by_zone
    net_metering_left_kwh: float | None = None
    if net_metering_ratio is not None:
        returned_usage = {zone: _ZoneUsage() for zone in usage}
        returned = await async_hourly_kwh(
            hass,
            get_statistic_id(meter_code, STAT_NAME_BY_KEY[STAT_KEY_ENERGY_RETURNED]),
            start,
            end,
        )
        _add_hours(returned_usage, cfg.tariff, returned, cfg.akcyza)
        returned_kwh_by_zone = {
            zone: round(zone_usage.kwh, 3) for zone, zone_usage in returned_usage.items()
        }
        for zone in returned_kwh_by_zone:
            usage.setdefault(zone, _ZoneUsage())
            kwh_by_zone.setdefault(zone, 0.0)
        # A surplus goes first to the zone with the highest variable network rate.
        order = sorted(usage, key=lambda zone: usage[zone].variable_rate, reverse=True)
        billed_kwh_by_zone, net_metering_left_kwh = settle_net_metering(
            kwh_by_zone, returned_kwh_by_zone, net_metering_ratio, order
        )

    energy_by_zone_netto: dict[str, float] = {}
    variable_network_by_zone_netto: dict[str, float] = {}
    quality_by_zone_netto: dict[str, float] = {}
    oze_by_zone_netto: dict[str, float] = {}
    cogeneration_by_zone_netto: dict[str, float] = {}

    for zone, zone_usage in usage.items():
        # Net metering leaves part of the zone's kWh to pay for; its hours keep
        # their prices in the same proportion.
        share = (
            billed_kwh_by_zone.get(zone, 0.0) / zone_usage.kwh if zone_usage.kwh else 0.0
        )
        # Sprzedaż energii: energia netto = (cena energii + akcyza) × kWh
        energy_by_zone_netto[zone] = round(zone_usage.energy * share, 2)
        # Usługa dystrybucji (zmienne): każdy składnik zaokrąglony osobno jak na fakturze
        variable_network_by_zone_netto[zone] = round(zone_usage.variable_network * share, 2)
        quality_by_zone_netto[zone] = round(zone_usage.quality * share, 2)
        oze_by_zone_netto[zone] = round(zone_usage.oze * share, 2)
        cogeneration_by_zone_netto[zone] = round(zone_usage.cogeneration * share, 2)

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
        returned_kwh_by_zone=returned_kwh_by_zone,
        billed_kwh_by_zone=billed_kwh_by_zone if net_metering_ratio is not None else {},
        net_metering_left_kwh=net_metering_left_kwh,
        unpriced_kwh=round(unpriced_kwh, 3),
    )


async def async_hourly_kwh(
    hass: HomeAssistant, statistic_id: str, start: date, end: date
) -> list[tuple[datetime, float]]:
    """Return the kWh of each hour in (start, end] of an energy statistic, in local time.

    The recorder works out each hour's change from the cumulative sums,
    starting from the newest sum before the window, so an hour is counted
    right however long the gap before it.
    """
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.start_of_local_day(start + timedelta(days=1)),
        dt_util.start_of_local_day(end + timedelta(days=1)),
        {statistic_id},
        "hour",
        None,
        {"change"},
    )
    return [
        (dt_util.as_local(dt_util.utc_from_timestamp(row["start"])), row.get("change") or 0.0)
        for row in stats.get(statistic_id, [])
    ]
