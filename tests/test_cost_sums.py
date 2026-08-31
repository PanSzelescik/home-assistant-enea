"""How the running total carries on when cost statistics are written."""
from __future__ import annotations

import datetime

from custom_components.enea import costs, statistics

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
    store = wire_recorder(statistics, [(MARCH_1_LAST_HOUR, 100.0)])

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
    store = wire_recorder(statistics, already_stored)

    await costs._inject_cost_series(object(), "PPE", "Koszt", _series(2, 3, 2.0))

    assert [row["sum"] for row in store.injected[0][1]] == [102.0, 104.0, 106.0]


async def test_each_hour_reports_the_total_so_far_as_its_state(wire_recorder) -> None:
    """Unlike energy, a cost hour's state is the running total, not the hour's own cost.

    The Energy Dashboard costs a period by the difference between the value at
    its two ends, so both columns have to carry the total.
    """
    store = wire_recorder(statistics, [])

    await costs._inject_cost_series(object(), "PPE", "Koszt", _series(2, 2, 1.5))

    assert [row["state"] for row in store.injected[0][1]] == [1.5, 3.0]


async def test_reimporting_changed_costs_moves_the_hours_after_them(wire_recorder) -> None:
    """Rewriting a range with different costs has to move what follows it.

    Same defect as on the energy side, with one addition: a cost hour keeps the
    running total in its state as well as its sum, so both columns move.
    """
    stored = [
        (MARCH_1_LAST_HOUR, 100.0, 100.0),
        (datetime.datetime(2026, 3, 2, 0, 0, tzinfo=TZ), 100.0, 100.0),
        (datetime.datetime(2026, 3, 3, 0, 0, tzinfo=TZ), 106.0, 106.0),
    ]
    store = wire_recorder(statistics, stored)

    await costs._inject_cost_series(
        object(), "PPE", "Koszt", [(datetime.datetime(2026, 3, 2, 0, 0, tzinfo=TZ), 4.0)]
    )

    assert [row["sum"] for row in store.injected[0][1]] == [104.0]
    later = store.injected[1][1]
    assert [row["sum"] for row in later] == [110.0]
    assert [row["state"] for row in later] == [110.0]


async def test_an_empty_range_writes_nothing(wire_recorder) -> None:
    """Nothing to write means nothing is sent to the recorder."""
    store = wire_recorder(statistics, [])

    await costs._inject_cost_series(object(), "PPE", "Koszt", [])

    assert store.injected == []
