"""Costing each day at the tariff group of the agreement in force that day."""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import FakeConfigEntry, FakeHass
from homeassistant.util import dt as dt_util

from custom_components.enea import costs
from custom_components.enea.connector import agreement_tariffs
from custom_components.enea.const import STAT_KEY_ENERGY_CONSUMED
from custom_components.enea.costs import TariffHistory, find_tariff_history
from custom_components.enea.statistics import get_statistic_id

CHANGE = datetime.date(2025, 12, 13)
"""The first day of the G12W agreement; G11 applied before it."""


def _ms(day: datetime.date) -> int:
    """Local midnight of a day as the portal's millisecond timestamp."""
    return int(dt_util.start_of_local_day(day).timestamp() * 1000)


DASHBOARD = {
    "agreements": [
        {"from": _ms(CHANGE), "to": None, "tariffGroupName": "G12W"},
        {"from": _ms(datetime.date(2022, 1, 8)), "to": _ms(CHANGE), "tariffGroupName": "G11"},
    ]
}


class _Pricing:
    def __init__(self, energy: float) -> None:
        self.energy = energy
        self.total_distribution = 0.3


@dataclass
class _Period:
    """A price period with one zone for every hour."""

    zone: str
    energy: float
    valid_from: datetime.date = datetime.date(2024, 7, 1)
    valid_until: datetime.date = datetime.date(2026, 12, 31)

    @property
    def zones(self) -> dict[str, _Pricing]:
        return {self.zone: _Pricing(self.energy)}

    def get_zone_at_hour(self, hour: int, day: datetime.date | None = None) -> str:
        return self.zone


class _Tariff:
    """A tariff group pricing every day with one period."""

    def __init__(self, name: str, zone: str, energy: float) -> None:
        self.name = name
        self.periods = [_Period(zone, energy)]

    def get_period_for_date(self, day: datetime.date) -> _Period:
        return self.periods[0]


G12W = _Tariff("G12w", "peak", 0.6)
G11 = _Tariff("G11", "all_day", 0.5)


@dataclass
class _Runtime:
    tariff: Any


def _hass(*tariffs: _Tariff) -> FakeHass:
    """A hass with one enea_prices entry per tariff."""
    return FakeHass(
        [
            FakeConfigEntry(domain="enea_prices", data={"tariff": t.name}, runtime_data=_Runtime(t))
            for t in tariffs
        ]
    )


def test_agreements_become_day_ranges() -> None:
    assert agreement_tariffs(DASHBOARD) == [
        (datetime.date(2022, 1, 8), CHANGE, "G11"),
        (CHANGE, None, "G12W"),
    ]


def test_each_day_takes_the_prices_of_its_agreements_group() -> None:
    history = find_tariff_history(_hass(G12W, G11), "G12W", DASHBOARD)

    assert history is not None
    assert history.get_period_for_date(CHANGE - datetime.timedelta(days=1)) is G11.periods[0]
    assert history.get_period_for_date(CHANGE) is G12W.periods[0]
    assert history.group_for_date(datetime.date(2025, 1, 1)) == "G11"
    assert {p.zone for p in history.periods} == {"peak", "all_day"}


def test_a_group_without_prices_leaves_its_days_unpriced() -> None:
    """G11 consumption costed at G12w zones and prices is worse than no cost."""
    history = find_tariff_history(_hass(G12W), "G12W", DASHBOARD)

    assert history is not None
    assert history.get_period_for_date(datetime.date(2025, 1, 1)) is None
    assert history.get_period_for_date(CHANGE) is G12W.periods[0]


def test_days_no_agreement_covers_keep_the_current_group() -> None:
    """Days before the first agreement, and meters with no agreements listed."""
    history = find_tariff_history(_hass(G12W), "G12W", DASHBOARD)
    bare = find_tariff_history(_hass(G12W), "G12W", {})

    assert history is not None and bare is not None
    assert history.get_period_for_date(datetime.date(2021, 1, 1)) is G12W.periods[0]
    assert bare.get_period_for_date(datetime.date(2025, 1, 1)) is G12W.periods[0]
    assert costs.price_signatures(bare, CHANGE, CHANGE) == costs.price_signatures(
        G12W, CHANGE, CHANGE
    )


def test_no_current_group_means_no_costs() -> None:
    assert find_tariff_history(_hass(G11), "G12W", DASHBOARD) is None


def test_the_tariff_change_reprices_the_earlier_groups_days() -> None:
    """Fingerprints follow the group, so the existing re-pricing picks the change up."""
    history = find_tariff_history(_hass(G12W, G11), "G12W", DASHBOARD)
    g11_day = datetime.date(2025, 1, 1)

    assert history is not None
    assert costs.price_signatures(history, g11_day, g11_day) != costs.price_signatures(
        G12W, g11_day, g11_day
    )
    assert costs.price_signatures(history, CHANGE, CHANGE) == costs.price_signatures(
        G12W, CHANGE, CHANGE
    )


def _day(day: datetime.date, hours: int = 2) -> tuple[datetime.date, dict[str, Any]]:
    """A fetched day with a reading of 1 kWh in each of its first hours."""
    start = dt_util.start_of_local_day(day)
    return (
        day,
        {
            STAT_KEY_ENERGY_CONSUMED: {
                "values": [
                    {
                        "integrationEnd": (start + datetime.timedelta(hours=h + 1)).timestamp() * 1000,
                        "items": [{"value": 1.0}],
                    }
                    for h in range(hours)
                ]
            }
        },
    )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    """Serve stored cost hours per statistic and capture what is written."""
    stored: dict[str, set[float]] = {}
    written: dict[str, list[tuple[datetime.datetime, float]]] = {}

    class Rec:
        async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
            return target(*args)

    def during(hass: Any, start: Any, end: Any, ids: set, *rest: Any) -> dict:
        sid = next(iter(ids))
        rows = [{"start": ts} for ts in sorted(stored.get(sid, set()))
                if start.timestamp() <= ts < end.timestamp()]
        return {sid: rows} if rows else {}

    def newest(hass: Any, count: int, sid: str, *rest: Any) -> dict:
        return {sid: [{"start": max(stored[sid])}]} if stored.get(sid) else {}

    async def inject(hass: Any, meter_code: str, name: str, series: list) -> float:
        written[name] = series
        return 0.0

    monkeypatch.setattr(costs, "get_instance", lambda hass: Rec())
    monkeypatch.setattr(costs, "statistics_during_period", during)
    monkeypatch.setattr(costs, "get_last_statistics", newest)
    monkeypatch.setattr(costs, "_inject_cost_series", inject)
    return stored, written


async def test_rewritten_days_take_old_costs_out_of_the_old_zone(recorder) -> None:
    """G11 days once costed in the G12w peak series are zeroed there.

    Re-pricing writes each hour to the series of its zone now.  The hour's
    old cost in the peak series would otherwise stay and be counted on top.
    """
    stored, written = recorder
    g11_day = datetime.date(2025, 1, 1)
    peak_sid = get_statistic_id("PPE", "Koszt energii pobrana – Szczyt")
    first_hour = dt_util.start_of_local_day(g11_day)
    stored[peak_sid] = {first_hour.timestamp(), (first_hour + datetime.timedelta(hours=1)).timestamp()}
    history = find_tariff_history(_hass(G12W, G11), "G12W", DASHBOARD)

    await costs.async_insert_cost_statistics(
        object(), "PPE", [_day(g11_day)], history, True, False, rewrite=True
    )

    assert [cost for _dt, cost in written["Koszt energii pobrana – Szczyt"]] == [0.0, 0.0]
    assert [cost for _dt, cost in written["Koszt energii pobrana – all_day"]] == pytest.approx(
        [(0.5 + 0.3) * 1.23] * 2
    )


async def test_unpriced_days_are_zeroed_and_reported_by_group(
    recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a G11 entry its days lose the G12w costs and get none."""
    stored, written = recorder
    g11_day = datetime.date(2025, 1, 1)
    peak_sid = get_statistic_id("PPE", "Koszt energii pobrana – Szczyt")
    stored[peak_sid] = {dt_util.start_of_local_day(g11_day).timestamp()}
    history = find_tariff_history(_hass(G12W), "G12W", DASHBOARD)
    caplog.set_level(logging.WARNING)

    await costs.async_insert_cost_statistics(
        object(), "PPE", [_day(g11_day), _day(CHANGE)], history, True, False, rewrite=True
    )

    # Only the hour the series held is zeroed; G12W's own day is costed as usual.
    peak = written["Koszt energii pobrana – Szczyt"]
    assert [cost for _dt, cost in peak] == pytest.approx([0.0, 1.107, 1.107])
    assert "G11" in caplog.records[0].getMessage()


async def test_a_plain_write_leaves_other_zones_alone(recorder) -> None:
    """Only a rewrite looks for hours stored elsewhere."""
    stored, written = recorder
    g11_day = datetime.date(2025, 1, 1)
    stored[get_statistic_id("PPE", "Koszt energii pobrana – Szczyt")] = {
        dt_util.start_of_local_day(g11_day).timestamp()
    }
    history = find_tariff_history(_hass(G12W, G11), "G12W", DASHBOARD)

    await costs.async_insert_cost_statistics(
        object(), "PPE", [_day(g11_day)], history, True, False
    )

    assert "Koszt energii pobrana – Szczyt" not in written


def test_history_stands_in_for_a_tariff_group() -> None:
    history = TariffHistory(G12W, [])

    assert history.name == "G12w"
    assert history.periods == G12W.periods
