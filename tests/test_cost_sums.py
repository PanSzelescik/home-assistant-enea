"""How the running total carries on when cost statistics are written."""
from __future__ import annotations

import datetime

from custom_components.enea import costs

TZ = datetime.timezone(datetime.timedelta(hours=2))

MARCH_1_LAST_HOUR = datetime.datetime(2026, 3, 1, 23, 0, tzinfo=TZ)


def _hours(day: int, count: int) -> list[datetime.datetime]:
    """The first count hours of the given day in March 2026."""
    return [datetime.datetime(2026, 3, day, hour, 0, tzinfo=TZ) for hour in range(count)]


def _series(day: int, count: int, cost: float) -> list[tuple[datetime.datetime, float]]:
    """A run of hours that each cost the same."""
    return [(hour, cost) for hour in _hours(day, count)]


async def test_new_days_carry_on_from_the_stored_total(wire_recorder) -> None:
    """Days written after everything stored continue from the last stored total."""
    store = wire_recorder(costs, [(MARCH_1_LAST_HOUR, 100.0)])

    await costs._inject_cost_series(object(), "PPE", "Koszt", _series(2, 3, 2.0))

    assert [row["sum"] for row in store.injected[0][1]] == [102.0, 104.0, 106.0]


async def test_writing_a_stored_range_again_does_not_double_it(wire_recorder) -> None:
    """Writing hours that are already stored must not add them to themselves.

    The backfill action re-reads the portal and overwrites whatever range it is
    given, so writing a range that is already stored is its normal use.  The
    total used to carry on from the end of the whole series, which for such a
    range means from a total that already includes it, so the values roughly
    doubled.
    """
    already_stored = [
        (MARCH_1_LAST_HOUR, 100.0),
        (datetime.datetime(2026, 3, 2, 0, 0, tzinfo=TZ), 102.0),
        (datetime.datetime(2026, 3, 2, 1, 0, tzinfo=TZ), 104.0),
        (datetime.datetime(2026, 3, 2, 2, 0, tzinfo=TZ), 106.0),
    ]
    store = wire_recorder(costs, already_stored)

    await costs._inject_cost_series(object(), "PPE", "Koszt", _series(2, 3, 2.0))

    assert [row["sum"] for row in store.injected[0][1]] == [102.0, 104.0, 106.0]


async def test_the_first_write_starts_from_zero(wire_recorder) -> None:
    """With nothing stored at all the total starts at the first hour's cost."""
    store = wire_recorder(costs, [])

    await costs._inject_cost_series(object(), "PPE", "Koszt", _series(2, 2, 1.5))

    assert [row["sum"] for row in store.injected[0][1]] == [1.5, 3.0]


async def test_a_range_older_than_everything_starts_from_zero(wire_recorder) -> None:
    """Hours before all stored history have nothing to carry on from."""
    store = wire_recorder(costs, [(datetime.datetime(2026, 3, 5, 0, 0, tzinfo=TZ), 500.0)])

    await costs._inject_cost_series(object(), "PPE", "Koszt", _series(2, 2, 1.0))

    assert [row["sum"] for row in store.injected[0][1]] == [1.0, 2.0]


async def test_an_empty_range_writes_nothing(wire_recorder) -> None:
    """Nothing to write means nothing is sent to the recorder."""
    store = wire_recorder(costs, [])

    await costs._inject_cost_series(object(), "PPE", "Koszt", [])

    assert store.injected == []
