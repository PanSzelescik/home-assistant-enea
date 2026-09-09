"""kWh per zone for a billing period, read out of the stored statistics."""
from __future__ import annotations

import datetime
from typing import Any

import pytest

from custom_components.enea import billing
from custom_components.enea.billing import PricesConfig, async_estimate_bill

TZ = datetime.UTC


class _Pricing:
    """One zone's per-kWh prices."""

    energy = 0.6518
    variable_network = 0.2702
    quality = 0.0332
    oze = 0.0073
    cogeneration = 0.0030


class _Monthly:
    """Fixed monthly fees.  These tests check kWh, so the amounts do not matter."""

    def get_network_fixed(self, phases: int) -> float:
        """Fixed network fee, independent of the phase count here."""
        return 26.23

    def get_capacity(self, annual_kwh: int) -> float:
        """Capacity fee, independent of the annual volume here."""
        return 17.18

    def get_subscription(self, billing_months: int) -> float:
        """Subscription fee, independent of the billing period here."""
        return 3.84


class _Period:
    """A tariff period with a single zone."""

    def __init__(self) -> None:
        self.zones = {"peak": _Pricing()}
        self.monthly = _Monthly()


class _Tariff:
    """A tariff group that prices every date the same way."""

    name = "G12w"

    def get_period_for_date(self, d: datetime.date) -> _Period:
        """Every date falls in the one period this tariff has."""
        return _Period()


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch):
    """Serve cumulative daily sums to billing, mirroring the recorder."""

    def _wire(rows: list[tuple[datetime.date, float]]) -> None:
        ordered = sorted(rows)

        class Rec:
            async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
                return target(*args)

        def during(
            hass: Any, start: Any, end: Any, ids: set, *rest: Any
        ) -> dict:
            sid = next(iter(ids))
            picked = [
                {"start": _midnight(d).timestamp(), "sum": total}
                for d, total in ordered
                if start <= _midnight(d) < end
            ]
            return {sid: picked} if picked else {}

        monkeypatch.setattr(billing, "get_instance", lambda hass: Rec())
        monkeypatch.setattr(billing, "statistics_during_period", during)

    return _wire


def _midnight(d: datetime.date) -> datetime.datetime:
    """Local midnight of a day, as the recorder timestamps a daily row."""
    return datetime.datetime(d.year, d.month, d.day, tzinfo=TZ)


def _cfg() -> PricesConfig:
    """A prices configuration built on the single-zone fake tariff."""
    return PricesConfig(
        tariff=_Tariff(), phases=3, annual_kwh=5000, billing_months=1, akcyza=0.005
    )


async def test_kwh_is_the_difference_between_the_boundaries(stored) -> None:
    """kWh for a period is the meter total at its end minus the total at its start."""
    stored(
        [
            (datetime.date(2026, 3, 6), 1000.0),
            (datetime.date(2026, 3, 9), 1010.0),
            (datetime.date(2026, 3, 20), 1100.0),
        ]
    )

    estimate = await async_estimate_bill(
        object(), "PPE", _cfg(), datetime.date(2026, 3, 6), datetime.date(2026, 3, 20)
    )

    assert estimate is not None
    assert estimate.kwh_by_zone["Szczyt"] == pytest.approx(100.0)


async def test_boundary_day_without_rows_in_this_zone(stored) -> None:
    """A zone with no reading on the start date must still find its starting total.

    The starting total came from a query beginning on the first day of the
    period, and a zone need not have a reading that day.  G12w has no peak
    hours at a weekend or on a public holiday, and a day the portal never
    published has none at all.  The starting total then stayed at zero and the
    period's consumption came out as everything the meter has ever recorded,
    which showed up as an estimate of thousands of zloty.  The reading dates
    are typed in by hand, so any weekend date hit this.
    """
    friday = datetime.date(2026, 3, 6)
    saturday = datetime.date(2026, 3, 7)
    stored(
        [
            (friday, 1000.0),
            # 7-8 March is a weekend: no peak hours, so no rows in this zone.
            (datetime.date(2026, 3, 9), 1010.0),
            (datetime.date(2026, 3, 20), 1100.0),
        ]
    )

    estimate = await async_estimate_bill(
        object(), "PPE", _cfg(), saturday, datetime.date(2026, 3, 20)
    )

    assert estimate is not None
    assert estimate.kwh_by_zone["Szczyt"] == pytest.approx(100.0)


async def test_period_starting_before_all_history(stored) -> None:
    """With nothing stored before the start date, zero is the right starting total."""
    stored([(datetime.date(2026, 3, 20), 40.0)])

    estimate = await async_estimate_bill(
        object(), "PPE", _cfg(), datetime.date(2026, 3, 1), datetime.date(2026, 3, 20)
    )

    assert estimate is not None
    assert estimate.kwh_by_zone["Szczyt"] == pytest.approx(40.0)
