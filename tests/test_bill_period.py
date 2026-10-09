"""kWh and charges of a billing period, worked out hour by hour from the statistics."""
from __future__ import annotations

import datetime
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enea import billing
from custom_components.enea.billing import PricesConfig, async_estimate_bill
from custom_components.enea.costs import TariffHistory
from custom_components.enea.statistics import get_statistic_id

CONSUMED = get_statistic_id("PPE", "Energia pobrana")
START = datetime.date(2026, 3, 1)
END = datetime.date(2026, 3, 31)


class _Pricing:
    """One zone's per-kWh netto prices."""

    def __init__(self, energy: float = 0.6, variable_network: float = 0.27) -> None:
        self.energy = energy
        self.variable_network = variable_network
        self.quality = 0.03
        self.oze = 0.007
        self.cogeneration = 0.003


class _Monthly:
    """Fixed monthly fees, scaled so each period's are told apart."""

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = scale

    def get_network_fixed(self, phases: int) -> float:
        return 20.0 * self.scale

    def get_capacity(self, annual_kwh: int) -> float:
        return 10.0 * self.scale

    def get_subscription(self, billing_months: int) -> float:
        return 1.0 * self.scale


class _G12wPeriod:
    """Peak from 6:00 to 21:00, off-peak otherwise."""

    def __init__(self, energy: float = 0.6, scale: float = 1.0) -> None:
        self.zones = {"peak": _Pricing(energy, 0.3), "off_peak": _Pricing(energy / 2, 0.1)}
        self.monthly = _Monthly(scale)

    def get_zone_at_hour(self, hour: int, day: datetime.date | None = None) -> str:
        return "peak" if 6 <= hour < 21 else "off_peak"


class _G11Period:
    """One zone for every hour."""

    def __init__(self) -> None:
        self.zones = {"day": _Pricing(0.5, 0.25)}
        self.monthly = _Monthly(0.5)

    def get_zone_at_hour(self, hour: int, day: datetime.date | None = None) -> str:
        return "day"


class _Tariff:
    """A tariff group whose prices change on a given day."""

    def __init__(self, name: str, before: Any, after: Any, change: datetime.date) -> None:
        self.name = name
        self._before, self._after, self._change = before, after, change
        self.periods = [before, after]

    def get_period_for_date(self, d: datetime.date) -> Any:
        return self._after if d >= self._change else self._before


def _g12w(change: datetime.date = START) -> _Tariff:
    """G12w whose energy price doubles on change (pass START for one price)."""
    return _Tariff("G12w", _G12wPeriod(0.3, 0.5), _G12wPeriod(0.6), change)


def _cfg(tariff: Any) -> PricesConfig:
    return PricesConfig(tariff=tariff, phases=3, annual_kwh=5000, billing_months=1, akcyza=0.0)


def _at(day: datetime.date, hour: int) -> datetime.datetime:
    """An hour on a local day."""
    return dt_util.start_of_local_day(day) + datetime.timedelta(hours=hour)


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch):
    """Serve hourly changes per statistic, as the recorder works them out from the sums."""
    windows: list[tuple[datetime.datetime, datetime.datetime]] = []

    def _wire(hours: dict[str, list[tuple[datetime.datetime, float]]]) -> list:
        class Rec:
            async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
                return target(*args)

        def during(
            hass: Any, start: Any, end: Any, ids: set, period: str, units: Any, types: set
        ) -> dict:
            assert (period, types) == ("hour", {"change"})
            windows.append((start, end))
            sid = next(iter(ids))
            rows = [
                {"start": hour.timestamp(), "change": kwh}
                for hour, kwh in hours.get(sid, [])
                if start <= hour < end
            ]
            return {sid: rows} if rows else {}

        monkeypatch.setattr(billing, "get_instance", lambda hass: Rec())
        monkeypatch.setattr(billing, "statistics_during_period", during)
        return windows

    return _wire


async def test_each_hour_goes_to_the_zone_of_the_tariff_schedule(stored) -> None:
    """The zone statistics the portal names play no part — only the hour does.

    After a change of tariff group the portal kept naming hours "Bezstrefowo"
    for weeks, and it calls G12w off-peak "Pozaszczyt" where the tariff says
    "Poza szczytem"; a bill read from those statistics missed both.
    """
    stored({CONSUMED: [(_at(START + datetime.timedelta(days=1), 3), 2.0), (_at(END, 10), 3.0)]})

    estimate = await async_estimate_bill(object(), "PPE", _cfg(_g12w()), START, END)

    assert estimate is not None
    assert estimate.kwh_by_zone == {"Szczyt": 3.0, "Poza szczytem": 2.0}
    assert estimate.energy_by_zone_netto == {"Szczyt": 1.8, "Poza szczytem": 0.6}
    assert estimate.unpriced_kwh == 0.0


async def test_the_period_is_read_from_the_day_after_the_previous_reading(stored) -> None:
    """(start, end] in days: from midnight after start to midnight after end."""
    windows = stored({CONSUMED: [(_at(START, 23), 5.0), (_at(END, 23), 1.0)]})

    estimate = await async_estimate_bill(object(), "PPE", _cfg(_g12w()), START, END)

    assert estimate is not None
    assert estimate.kwh_by_zone == {"Szczyt": 0.0, "Poza szczytem": 1.0}
    assert windows == [
        (
            dt_util.start_of_local_day(START + datetime.timedelta(days=1)),
            dt_util.start_of_local_day(END + datetime.timedelta(days=1)),
        )
    ]


async def test_each_hour_is_priced_by_its_own_day(stored) -> None:
    """A bill across a price change charges each part at its own prices."""
    change = datetime.date(2026, 3, 16)
    stored({CONSUMED: [(_at(datetime.date(2026, 3, 10), 10), 10.0), (_at(change, 10), 10.0)]})

    estimate = await async_estimate_bill(object(), "PPE", _cfg(_g12w(change)), START, END)

    assert estimate is not None
    assert estimate.energy_by_zone_netto["Szczyt"] == pytest.approx(10 * 0.3 + 10 * 0.6)
    # Fixed fees come from the period at the end of the bill.
    assert estimate.fixed_network_netto == 20.0


async def test_a_change_of_tariff_group_prices_each_part_by_its_group(stored) -> None:
    """G11 hours before the change go to its single zone, at G11 prices."""
    change = datetime.date(2026, 3, 16)
    g11 = _Tariff("G11", _G11Period(), _G11Period(), START)
    g12w = _g12w()
    history = TariffHistory(
        g12w,
        [(datetime.date(2022, 1, 1), change, g11, "G11"), (change, None, g12w, "G12W")],
    )
    stored({CONSUMED: [(_at(datetime.date(2026, 3, 10), 10), 4.0), (_at(change, 10), 6.0)]})

    estimate = await async_estimate_bill(object(), "PPE", _cfg(history), START, END)

    assert estimate is not None
    assert estimate.kwh_by_zone == {"Szczyt": 6.0, "Poza szczytem": 0.0, "Dzień": 4.0}
    assert estimate.energy_by_zone_netto["Dzień"] == pytest.approx(4 * 0.5)


async def test_hours_of_a_group_without_prices_are_left_out_and_counted(stored) -> None:
    """Without an enea_prices entry for G11 its hours cannot be priced."""
    change = datetime.date(2026, 3, 16)
    history = TariffHistory(_g12w(), [(datetime.date(2022, 1, 1), change, None, "G11")])
    stored({CONSUMED: [(_at(datetime.date(2026, 3, 10), 10), 4.0), (_at(change, 10), 6.0)]})

    estimate = await async_estimate_bill(object(), "PPE", _cfg(history), START, END)

    assert estimate is not None
    assert estimate.kwh_by_zone == {"Szczyt": 6.0, "Poza szczytem": 0.0}
    assert estimate.unpriced_kwh == 4.0


async def test_no_prices_on_the_last_day_means_no_bill(stored) -> None:
    """The fixed fees need the period at the end of the bill."""
    history = TariffHistory(_g12w(), [(datetime.date(2022, 1, 1), None, None, "G11")])
    stored({CONSUMED: []})

    assert await async_estimate_bill(object(), "PPE", _cfg(history), START, END) is None


class _OfferMonthly(_Monthly):
    """Fixed fees of a market offer: the seller adds a monthly trade fee."""

    trade = 9.82


def _offer() -> _Tariff:
    """G12w priced from the customer's contract."""
    period = _G12wPeriod(0.6)
    period.monthly = _OfferMonthly()
    return _Tariff("G12w", period, period, START)


async def test_a_trade_fee_joins_the_energy_section(stored) -> None:
    """On a market offer the invoice's 'Sprzedaż energii' includes the trade fee.

    Contract prices entered in enea_prices put it on the period's monthly fees;
    a tariff period has none, which the plain fakes above stand for.
    """
    stored({CONSUMED: [(_at(END, 10), 100.0)]})

    estimate = await async_estimate_bill(object(), "PPE", _cfg(_offer()), START, END)

    assert estimate is not None
    assert estimate.months == 1
    assert estimate.trade_fee_netto == 9.82
    assert estimate.energy_netto == round(100 * 0.6 + 9.82, 2)


async def test_no_trade_fee_on_the_tariff(stored) -> None:
    """A tariff period's monthly fees carry no trade fee."""
    stored({CONSUMED: [(_at(END, 10), 100.0)]})

    estimate = await async_estimate_bill(object(), "PPE", _cfg(_g12w()), START, END)

    assert estimate is not None
    assert estimate.trade_fee_netto == 0.0
    assert estimate.energy_netto == round(100 * 0.6, 2)
