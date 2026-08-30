"""How the running total carries on when energy statistics are written."""
from __future__ import annotations

import datetime

from custom_components.enea import statistics

TZ = datetime.timezone(datetime.timedelta(hours=2))

MARCH_1_LAST_HOUR = datetime.datetime(2026, 3, 1, 23, 0, tzinfo=TZ)


def _hours(day: int, count: int) -> list[datetime.datetime]:
    """The first count hours of the given day in March 2026."""
    return [datetime.datetime(2026, 3, day, hour, 0, tzinfo=TZ) for hour in range(count)]


def _series(day: int, count: int, kwh: float) -> list[tuple[datetime.datetime, float]]:
    """A run of hours that each hold the same reading."""
    return [(hour, kwh) for hour in _hours(day, count)]


async def test_new_days_carry_on_from_the_stored_total(wire_recorder) -> None:
    """Days written after everything stored continue from the last stored total."""
    store = wire_recorder(statistics, [(MARCH_1_LAST_HOUR, 100.0)])

    await statistics._inject_energy_series(object(), "PPE", "Energia pobrana", _series(2, 3, 2.0))

    assert [row["sum"] for row in store.injected[0][1]] == [102.0, 104.0, 106.0]


async def test_writing_a_stored_range_again_does_not_double_it(wire_recorder) -> None:
    """Writing hours that are already stored must not add them to themselves.

    Same defect as the one fixed for the cost statistics in "Fix doubled cost
    totals when a date range is imported twice", left standing here.  The
    backfill action defaults to the last 30 days, which on a working
    installation are already stored, so this is what an ordinary call to it
    did.  The rewritten hours jumped to the total at the end of the series and
    the untouched day after them stayed low, which Home Assistant reads as the
    meter having been replaced.
    """
    already_stored = [
        (MARCH_1_LAST_HOUR, 100.0),
        (datetime.datetime(2026, 3, 2, 0, 0, tzinfo=TZ), 102.0),
        (datetime.datetime(2026, 3, 2, 1, 0, tzinfo=TZ), 104.0),
        (datetime.datetime(2026, 3, 2, 2, 0, tzinfo=TZ), 106.0),
    ]
    store = wire_recorder(statistics, already_stored)

    await statistics._inject_energy_series(object(), "PPE", "Energia pobrana", _series(2, 3, 2.0))

    assert [row["sum"] for row in store.injected[0][1]] == [102.0, 104.0, 106.0]


async def test_the_first_write_starts_from_zero(wire_recorder) -> None:
    """With nothing stored at all the total starts at the first hour's reading."""
    store = wire_recorder(statistics, [])

    await statistics._inject_energy_series(object(), "PPE", "Energia pobrana", _series(2, 2, 1.5))

    assert [row["sum"] for row in store.injected[0][1]] == [1.5, 3.0]


async def test_a_range_older_than_everything_starts_from_zero(wire_recorder) -> None:
    """Hours before all stored history have nothing to carry on from."""
    store = wire_recorder(statistics, [(datetime.datetime(2026, 3, 5, 0, 0, tzinfo=TZ), 500.0)])

    await statistics._inject_energy_series(object(), "PPE", "Energia pobrana", _series(2, 2, 1.0))

    assert [row["sum"] for row in store.injected[0][1]] == [1.0, 2.0]


async def test_each_hour_keeps_its_own_reading(wire_recorder) -> None:
    """Only the total adds up; each hour still reports the kWh read for it.

    The Energy Dashboard draws consumption from the difference between totals,
    but the per-hour value has to stay the reading itself.
    """
    store = wire_recorder(statistics, [])

    await statistics._inject_energy_series(object(), "PPE", "Energia pobrana", _series(2, 2, 1.5))

    assert [row["state"] for row in store.injected[0][1]] == [1.5, 1.5]
