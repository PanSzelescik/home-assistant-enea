"""Invoice fee bases and historical fixed fees, including issue #19."""
from __future__ import annotations

import importlib
import json
from datetime import date
from hashlib import blake2s
from pathlib import Path

import pytest

from custom_components.enea.billing import PricesConfig, async_estimate_bill
from custom_components.enea.costs import TariffHistory, _kwh_price, first_repriced_day, price_signatures
from custom_components.enea.sensor import EneaBillSensor
from custom_components.enea.statistics import get_statistic_id
from ha_instance import database, real_enea_prices  # noqa: F401
from test_bill_period import (
    CONSUMED, _G11Period, _Monthly, _Tariff, _at, _cfg, stored,
)
from test_net_metering import _CostPricing, _CostTariff

RETURNED = get_statistic_id("PPE", "Energia oddana")


async def test_issue19_anonymized_invoice_fee_bases(stored, real_enea_prices):
    """Altered invoice volumes still pay levies before opust, capacity from July."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/invoices/issue19_g11.json").read_text(encoding="utf-8")
    )
    expected = fixture["expected"]
    tariffs = importlib.import_module("custom_components.enea_prices.tariffs")
    tariff = tariffs.with_price_changes(tariffs.TARIFFS["G11"], [
        tariffs.PriceChange(
            valid_from=date(2025, 1, 1),
            energy={tariffs.Zone.DAY: fixture["invoice_energy_price"] - 0.005},
            trade_fee=fixture["trade_fee"],
        )
    ])
    cfg = PricesConfig(tariff, fixture["phases"], fixture["annual_kwh"], fixture["billing_months"], 0.005)
    stored({
        sid: [(_at(date.fromisoformat(row["day"]), row["hour"]), row["kwh"]) for row in fixture[key]]
        for sid, key in ((CONSUMED, "consumed"), (RETURNED, "returned"))
    })

    bill = await async_estimate_bill(
        object(), "PPE", cfg, date.fromisoformat(fixture["previous_reading"]),
        date.fromisoformat(fixture["last_reading"]), fixture["net_metering_ratio"],
    )

    assert bill is not None
    assert bill.kwh_by_zone == {"Dzień": expected["consumed_kwh"]}
    assert bill.returned_kwh_by_zone == {"Dzień": expected["returned_kwh"]}
    assert bill.billed_kwh_by_zone == {"Dzień": expected["billed_kwh"]}
    assert bill.energy_by_zone_netto == {"Dzień": expected["energy_netto"]}
    assert bill.variable_network_by_zone_netto == {"Dzień": expected["variable_network_netto"]}
    assert bill.quality_by_zone_netto == {"Dzień": expected["quality_netto"]}
    assert bill.oze_by_zone_netto == {"Dzień": expected["oze_netto"]}
    assert bill.cogeneration_by_zone_netto == {"Dzień": expected["cogeneration_netto"]}
    assert bill.fixed_capacity_netto == expected["fixed_capacity_netto"]
    assert bill.fixed_network_netto == expected["fixed_network_netto"]
    assert bill.fixed_transition_netto == expected["fixed_transition_netto"]
    assert bill.distribution_months == 11.3871
    assert bill.months == 12
    assert bill.fixed_subscription_netto == expected["fixed_subscription_netto"]
    assert bill.trade_fee_netto == expected["trade_fee_netto"]
    assert bill.total_netto == expected["total_netto"]
    assert bill.total == expected["total_brutto"]


@pytest.mark.parametrize("ratio", [0.7, 0.8])
async def test_full_opust_still_charges_oze_and_cogeneration(stored, ratio):
    """Even a surplus cannot cancel the two levies on imported energy."""
    tariff = _Tariff("G11", _G11Period(), _G11Period(), date(2026, 1, 1))
    stored({CONSUMED: [(_at(date(2026, 3, 10), 10), 100)], RETURNED: [(_at(date(2026, 3, 10), 12), 200)]})
    bill = await async_estimate_bill(object(), "PPE", _cfg(tariff), date(2026, 2, 28), date(2026, 3, 31), ratio)
    assert bill.billed_kwh_by_zone == {"Dzień": 0.0}
    assert bill.energy_by_zone_netto == {"Dzień": 0.0}
    assert bill.variable_network_by_zone_netto == {"Dzień": 0.0}
    assert bill.quality_by_zone_netto == {"Dzień": 0.0}
    assert bill.oze_by_zone_netto == {"Dzień": 0.7}
    assert bill.cogeneration_by_zone_netto == {"Dzień": 0.3}


@pytest.mark.parametrize(("start", "end", "network", "fraction"), [
    (date(2024, 2, 28), date(2024, 2, 29), 0.34, 0.0345),
    (date(2024, 1, 31), date(2024, 2, 29), 10.0, 1.0),
    (date(2025, 1, 31), date(2025, 2, 28), 10.0, 1.0),
])
async def test_fixed_fees_use_calendar_days_including_leap_day(stored, start, end, network, fraction):
    """A day uses 1/29 or 1/28 month; both whole Februaries cost one month."""
    tariff = _Tariff("G11", _G11Period(), _G11Period(), start)
    stored({CONSUMED: []})
    bill = await async_estimate_bill(object(), "PPE", _cfg(tariff), start, end)
    assert bill.fixed_network_netto == network
    assert bill.distribution_months == fraction
    assert bill.fixed_subscription_netto == 0.5


@pytest.mark.parametrize(("start", "end"), [
    (date(2026, 6, 6), date(2026, 8, 5)),
    (date(2026, 6, 1), date(2026, 8, 6)),
    (date(2026, 6, 10), date(2026, 8, 4)),
])
async def test_a_two_month_period_pays_two_whole_months(stored, real_enea_prices, start, end):
    """A G12 invoice for 7.06–5.08.2026 charges 2 × 14.56, 2 × 24.05 and 2 × 1.92."""
    tariffs = importlib.import_module("custom_components.enea_prices.tariffs")
    cfg = PricesConfig(tariffs.TARIFFS["G12"], 3, 5000, 2, 0.005)
    stored({CONSUMED: []})
    bill = await async_estimate_bill(object(), "PPE", cfg, start, end)
    assert bill.distribution_months == 2.0
    assert bill.fixed_network_netto == 29.12
    assert bill.fixed_capacity_netto == 48.10
    assert bill.fixed_subscription_netto == 3.84


async def test_a_window_off_whole_months_is_prorated_by_calendar_days(stored, real_enea_prices):
    """53 days are further than the reading-day drift from two months."""
    tariffs = importlib.import_module("custom_components.enea_prices.tariffs")
    cfg = PricesConfig(tariffs.TARIFFS["G12"], 3, 5000, 2, 0.005)
    stored({CONSUMED: []})
    bill = await async_estimate_bill(object(), "PPE", cfg, date(2026, 6, 6), date(2026, 7, 29))
    # June 7-30 is 24/30, July 1-29 is 29/31.
    assert bill.distribution_months == 1.7355
    assert bill.fixed_network_netto == 25.27


async def test_transition_fee_stops_and_capacity_changes_at_new_year(stored, real_enea_prices):
    """Only December bears transition; capacity uses each year's rate."""
    tariffs = importlib.import_module("custom_components.enea_prices.tariffs")
    cfg = PricesConfig(tariffs.TARIFFS["G11"], 3, 5000, 12, 0.005)
    stored({CONSUMED: []})
    bill = await async_estimate_bill(object(), "PPE", cfg, date(2025, 11, 30), date(2026, 1, 31))
    assert bill.fixed_transition_netto == 0.33
    assert bill.fixed_capacity_netto == 40.06
    assert bill.fixed_network_netto == 20.55
    assert bill.fixed_subscription_netto == 0.64


@pytest.mark.parametrize(("day", "capacity"), [
    (date(2025, 6, 30), 0.0), (date(2025, 7, 1), 0.52),
])
async def test_capacity_resumes_on_july_first(stored, real_enea_prices, day, capacity):
    """The first chargeable day uses July's 31 days, the previous day is free."""
    from datetime import timedelta
    tariffs = importlib.import_module("custom_components.enea_prices.tariffs")
    cfg = PricesConfig(tariffs.TARIFFS["G11"], 3, 5000, 12, 0.005)
    stored({CONSUMED: []})
    bill = await async_estimate_bill(object(), "PPE", cfg, day - timedelta(days=1), day)
    assert bill.fixed_capacity_netto == capacity


async def test_monthly_trade_fee_uses_each_months_offer(stored):
    """A new February offer cannot rewrite January's trade fee."""
    before, after = _G11Period(), _G11Period()
    before.monthly.trade, after.monthly.trade = 5.0, 9.0
    tariff = _Tariff("G11", before, after, date(2026, 2, 1))
    stored({CONSUMED: []})
    bill = await async_estimate_bill(object(), "PPE", _cfg(tariff), date(2025, 12, 31), date(2026, 2, 12))
    assert bill.trade_fee_netto == 14.0
    assert bill.fixed_subscription_netto == 1.0
    assert bill.months == 2


async def test_fixed_fees_follow_tariff_group_and_skip_unpriced_days(stored):
    """Missing historical prices must not borrow the current group's fees."""
    g11 = _Tariff("G11", _G11Period(), _G11Period(), date(2026, 1, 1))
    history = TariffHistory(g11, [(date(2026, 1, 1), date(2026, 3, 16), None, "G12")])
    stored({CONSUMED: []})
    bill = await async_estimate_bill(object(), "PPE", _cfg(history), date(2026, 2, 28), date(2026, 3, 31))
    assert bill.fixed_network_netto == 5.16
    assert bill.fixed_capacity_netto == 2.58
    assert bill.distribution_months == 0.5161


def test_return_credit_excludes_levies_but_consumption_price_keeps_them():
    """The Energy Dashboard must leave OZE and cogeneration payable too."""
    pricing = _CostPricing()
    assert _kwh_price(pricing, 0.0) == 0.984
    assert _kwh_price(pricing, 0.0, net_metering_return=True) == 0.9717


def test_net_metering_fingerprint_tracks_credit_even_if_total_price_is_unchanged(monkeypatch):
    """Changing only the levy share must reprice the returned-energy history."""
    tariff = _CostTariff()
    day = date(2026, 3, 2)
    plain = price_signatures(tariff, day, day)
    net = price_signatures(tariff, day, day, 0.8)
    monkeypatch.setattr(_CostPricing, "oze", 0.02)
    assert price_signatures(tariff, day, day) == plain
    assert price_signatures(tariff, day, day, 0.8) != net


def test_old_net_metering_costs_are_selected_for_repricing():
    """Previously stored hashes must trigger a rewrite after this correction."""
    day = date(2026, 3, 2)
    old = blake2s("|".join(["peak=0.984"] * 24 + ["returned=0.8"]).encode(), digest_size=8).hexdigest()
    current = price_signatures(_CostTariff(), day, day, 0.8)
    assert first_repriced_day({day.isoformat(): old}, current, day, day) == day


async def test_bill_sensor_exposes_transition_and_fractional_months(stored):
    """The invoice breakdown exposes the new fee and its proration basis."""
    from types import SimpleNamespace
    class TransitionMonthly(_Monthly):
        """Simple rates with a visible transition fee."""

        def get_transition(self, annual_kwh):
            """Return a rate that costs one grosz per day in March."""
            return 0.31

    period = _G11Period()
    period.monthly = TransitionMonthly()
    tariff = _Tariff("G11", period, period, date(2026, 1, 1))
    stored({CONSUMED: []})
    bill = await async_estimate_bill(object(), "PPE", _cfg(tariff), date(2026, 2, 28), date(2026, 3, 12))
    sensor = object.__new__(EneaBillSensor)
    sensor.coordinator = SimpleNamespace(bill_estimates={"previous": bill})
    sensor._bill_key = "previous"
    assert sensor.extra_state_attributes["fixed_transition_netto"] == 0.12
    assert sensor.extra_state_attributes["distribution_months"] == 0.3871
