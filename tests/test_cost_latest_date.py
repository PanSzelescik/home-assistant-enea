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


def _today() -> datetime.date:
    """Today in the fixture timezone, mirroring TariffGroup.get_current_period."""
    return datetime.datetime.now(tz=TZ).date()


class _Zone(str):
    """Zone key that stringifies to the value used in statistic names."""


class _Period:
    """Stand-in for a date-bounded enea_prices TariffPeriod."""

    def __init__(
        self, zones: list[str], valid_from: datetime.date, valid_until: datetime.date
    ) -> None:
        self.zones = {_Zone(z): object() for z in zones}
        self.valid_from = valid_from
        self.valid_until = valid_until


class _Tariff:
    """Stand-in for a TariffGroup: zones live inside date-bounded periods."""

    def __init__(
        self,
        zones: list[str],
        valid_from: datetime.date = datetime.date(2000, 1, 1),
        valid_until: datetime.date = datetime.date(2099, 12, 31),
    ) -> None:
        self.periods = [_Period(zones, valid_from, valid_until)]

    def get_period_for_date(self, d: datetime.date) -> _Period | None:
        for period in self.periods:
            if period.valid_from <= d <= period.valid_until:
                return period
        return None

    def get_current_period(self) -> _Period | None:
        return self.get_period_for_date(_today())


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


async def test_lookup_survives_a_tariff_table_that_ran_out(recorder) -> None:
    """Stored statistics must still be found once the table stops covering today.

    The zone names came from the period covering today.  The tariff table
    ends on a fixed date, so from the day after it there is no such period,
    the lookup reported nothing, and the caller read that as no cost history
    at all.  It then downloaded the meter's whole history on every refresh and
    threw all of it away, because those days had no price either.
    """
    old = datetime.datetime.now(tz=TZ) - datetime.timedelta(days=3)
    recorder({_sid("peak"): old})
    expired = _Tariff(
        ["peak"], valid_until=_today() - datetime.timedelta(days=1)
    )
    assert expired.get_current_period() is None

    got = await costs.async_get_cost_latest_date(object(), "PPE", expired, True, False)

    assert got == old.date()
