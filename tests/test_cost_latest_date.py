"""Finding the newest day for which cost statistics exist."""
from __future__ import annotations

import datetime
from typing import Any

import pytest
from conftest import TEST_TIME_ZONE
from homeassistant.util import dt as dt_util

from custom_components.enea import costs

TZ = dt_util.get_time_zone(TEST_TIME_ZONE)
"""Build inputs in the same zone the integration reports dates in."""


class _Zone(str):
    """Zone key that stringifies to the value used in statistic names."""


class _Period:
    def __init__(self, zones: list[str]) -> None:
        self.zones = {_Zone(z): object() for z in zones}


class _Tariff:
    def __init__(self, zones: list[str]) -> None:
        self._period = _Period(zones)

    def get_current_period(self) -> _Period:
        return self._period


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    """Wire the module to an in-memory newest-entry lookup."""

    def _wire(newest: dict[str, datetime.datetime]) -> None:
        class Rec:
            async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
                return target(*args)

        def last(hass: Any, count: int, sid: str, convert: bool, types: set) -> dict:
            dt = newest.get(sid)
            return {sid: [{"start": dt.timestamp(), "sum": 1.0}]} if dt else {}

        def during(hass: Any, start: Any, end: Any, ids: set, *rest: Any) -> dict:
            sid = next(iter(ids))
            dt = newest.get(sid)
            if dt is None or not start <= dt < end:
                return {}
            return {sid: [{"start": dt.timestamp(), "sum": 1.0}]}

        monkeypatch.setattr(costs, "get_instance", lambda hass: Rec())
        monkeypatch.setattr(costs, "get_last_statistics", last)
        # Kept wired so the pre-fix code path runs too and the regression test
        # fails for the defect itself, not for a missing stub.
        monkeypatch.setattr(costs, "statistics_during_period", during)

    return _wire


def _sid(zone: str) -> str:
    from custom_components.enea.statistics import get_statistic_id

    return get_statistic_id("PPE", costs.get_cost_statistic_name("pobrana", zone))


async def test_returns_the_newest_day_across_zones(recorder) -> None:
    """The most recent date found in any zone is the one reported."""
    now = datetime.datetime.now(tz=TZ)
    recorder(
        {
            _sid("peak"): now - datetime.timedelta(days=2),
            _sid("off_peak"): now - datetime.timedelta(days=1),
        }
    )

    got = await costs.async_get_cost_latest_date(
        object(), "PPE", _Tariff(["peak", "off_peak"]), True, False
    )
    assert got == (now - datetime.timedelta(days=1)).date()


async def test_statistics_older_than_thirty_days_are_still_found(recorder) -> None:
    """Stored statistics must be reported however old they are.

    The lookup used to ask only for the last thirty days.  Once the newest
    cost statistic was older than that, the function reported none at all, the
    caller treated the meter as having no cost history, and started writing
    again from the date the meter was installed, over dates already stored.
    """
    old = datetime.datetime.now(tz=TZ) - datetime.timedelta(days=400)
    recorder({_sid("peak"): old})

    got = await costs.async_get_cost_latest_date(
        object(), "PPE", _Tariff(["peak"]), True, False
    )
    assert got == old.date()


async def test_no_statistics_yields_none(recorder) -> None:
    """A meter without any cost statistics reports None."""
    recorder({})

    got = await costs.async_get_cost_latest_date(
        object(), "PPE", _Tariff(["peak"]), True, False
    )
    assert got is None


async def test_disabled_directions_are_not_queried(recorder) -> None:
    """With both directions disabled there is nothing to look up."""
    recorder({_sid("peak"): datetime.datetime.now(tz=TZ)})

    got = await costs.async_get_cost_latest_date(
        object(), "PPE", _Tariff(["peak"]), False, False
    )
    assert got is None
