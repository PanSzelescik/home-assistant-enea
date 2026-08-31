"""How the running total carries on when energy statistics are written."""
from __future__ import annotations

import datetime

from custom_components.enea import statistics

TZ = datetime.timezone(datetime.timedelta(hours=2))

MARCH_1_LAST_HOUR = datetime.datetime(2026, 3, 1, 23, 0, tzinfo=TZ)


def _at(day: int, hour: int) -> datetime.datetime:
    """One hour of one day in March 2026."""
    return datetime.datetime(2026, 3, day, hour, 0, tzinfo=TZ)


def _hours(day: int, count: int) -> list[datetime.datetime]:
    """The first count hours of the given day in March 2026."""
    return [_at(day, hour) for hour in range(count)]


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
    """Hours before all stored history have nothing to carry on from.

    The history that follows them does, though: two kWh now precede it, so its
    running total has to make room for them or the series steps backwards
    where the two meet.
    """
    store = wire_recorder(statistics, [(_at(5, 0), 500.0, 4.0)])

    await statistics._inject_energy_series(object(), "PPE", "Energia pobrana", _series(2, 2, 1.0))

    assert [row["sum"] for row in store.injected[0][1]] == [1.0, 2.0]
    assert [row["sum"] for row in store.injected[1][1]] == [502.0]


async def test_reimporting_changed_values_moves_the_hours_after_them(wire_recorder) -> None:
    """Rewriting a range with different values has to move what follows it.

    A day the portal had not published yet is stored with zeroes.  When the
    data turns up later and enea.backfill is run over that range again, the
    range's own totals go up, but every hour after it still carries a total
    counted on from the zeroes.  The series then steps down where the two meet,
    and Home Assistant reads a running total going backwards as the meter
    having been replaced.

    Each later hour's own reading is unaffected, so adding the difference to
    their totals restores the chain without fetching anything again.
    """
    stored = [
        (_at(1, 23), 100.0, 0.0),
        (_at(2, 0), 100.0, 0.0),   # zero-filled: published as nothing at the time
        (_at(2, 1), 100.0, 0.0),
        (_at(3, 0), 105.0, 5.0),   # a later day, untouched by this import
        (_at(3, 1), 112.0, 7.0),
    ]
    store = wire_recorder(statistics, stored)

    await statistics._inject_energy_series(
        object(), "PPE", "Energia pobrana", [(_at(2, 0), 2.0), (_at(2, 1), 3.0)]
    )

    assert [row["sum"] for row in store.injected[0][1]] == [102.0, 105.0]
    later = store.injected[1][1]
    assert [row["sum"] for row in later] == [110.0, 117.0]
    assert [row["state"] for row in later] == [5.0, 7.0]


async def test_reimporting_the_same_values_leaves_the_rest_alone(wire_recorder) -> None:
    """A range that comes back unchanged must not rewrite the history after it."""
    stored = [
        (_at(1, 23), 100.0, 1.0),
        (_at(2, 0), 102.0, 2.0),
        (_at(2, 1), 105.0, 3.0),
        (_at(3, 0), 110.0, 5.0),
    ]
    store = wire_recorder(statistics, stored)

    await statistics._inject_energy_series(
        object(), "PPE", "Energia pobrana", [(_at(2, 0), 2.0), (_at(2, 1), 3.0)]
    )

    assert [row["sum"] for row in store.injected[0][1]] == [102.0, 105.0]
    assert len(store.injected) == 1


async def test_each_hour_keeps_its_own_reading(wire_recorder) -> None:
    """Only the total adds up; each hour still reports the kWh read for it.

    The Energy Dashboard draws consumption from the difference between totals,
    but the per-hour value has to stay the reading itself.
    """
    store = wire_recorder(statistics, [])

    await statistics._inject_energy_series(object(), "PPE", "Energia pobrana", _series(2, 2, 1.5))

    assert [row["state"] for row in store.injected[0][1]] == [1.5, 1.5]


async def test_the_series_left_behind_never_steps_backwards(wire_recorder) -> None:
    """The running totals left in the series must not decrease anywhere.

    Home Assistant reads a running total that goes down as the meter having
    been replaced, and starts counting from zero again.  That is the harm all
    three running-total fixes on this branch are really about, and it is the
    one property nothing checked: every other test here asserts the hours being
    written and stops there, so a defect confined to the hours after the range
    stayed invisible even in a test that had such hours stored.

    The re-imported day is worth more than the day after it was counted on
    from, which is what makes the step down show up at the boundary.
    """
    stored = [
        (_at(1, 23), 100.0, 0.0),
        (_at(2, 0), 100.0, 0.0),
        (_at(2, 1), 100.0, 0.0),
        (_at(3, 0), 105.0, 5.0),
        (_at(3, 1), 112.0, 7.0),
    ]
    store = wire_recorder(statistics, stored)

    await statistics._inject_energy_series(
        object(), "PPE", "Energia pobrana", [(_at(2, 0), 4.0), (_at(2, 1), 4.0)]
    )

    assert store.totals == sorted(store.totals)
