"""The total a rewritten range counts on from is found without reading the whole series.

write_cumulative_series used to read a series from its very first hour to find
the one entry before the range.  The entry is nearly always close, so the
search starts with a short window and widens it — but it must still find an
entry years back, and still say "nothing" for a range before all history.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.enea import statistics
from custom_components.enea.const import ANCHOR_SEARCH_WINDOWS_DAYS, EPOCH
from ha_instance import StatisticsDatabase

SID = "enea:590310600000001234_energia_pobrana"
RANGE_START = datetime(2026, 6, 1, tzinfo=UTC)


class _CountingDatabase(StatisticsDatabase):
    """A statistics database that records the windows it is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.windows: list[tuple[datetime, datetime | None, int]] = []

    def during(self, hass: Any, start: datetime, end: datetime | None = None, *args: Any, **kwargs: Any):
        """Answer like the recorder, noting the window and how many rows it held."""
        found = super().during(hass, start, end, *args, **kwargs)
        self.windows.append((start, end, sum(len(rows) for rows in found.values())))
        return found


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _CountingDatabase:
    """Point the statistics module at a counting in-memory database."""
    database = _CountingDatabase()
    database.install(monkeypatch)
    return database


def _put(store: _CountingDatabase, *rows: tuple[datetime, float]) -> None:
    """Store hours with the given running totals."""
    store.add_external(
        None, {"statistic_id": SID}, [{"start": start, "sum": total, "state": 1.0} for start, total in rows]
    )


async def test_nothing_stored_is_zero(store):
    """A series with no rows at all opens at zero."""
    assert await statistics._total_before(None, SID, RANGE_START) == 0.0


async def test_only_later_rows_is_zero(store):
    """Rows at or after the moment are not the total before it."""
    _put(store, (RANGE_START, 50.0), (RANGE_START + timedelta(hours=1), 51.0))

    assert await statistics._total_before(None, SID, RANGE_START) == 0.0


@pytest.mark.parametrize("back,queries", [
    pytest.param(timedelta(hours=1), 1, id="hour-before"),
    pytest.param(timedelta(days=1, hours=23), 1, id="within-first-window"),
    pytest.param(timedelta(days=10), 2, id="ten-days"),
    pytest.param(timedelta(days=200), 3, id="two-hundred-days"),
    pytest.param(timedelta(days=3 * 365), 4, id="three-years"),
])
async def test_entry_is_found_however_far_back(store, back, queries):
    """The newest earlier entry is found at any distance, in as few windows as it takes."""
    _put(store, (RANGE_START - back - timedelta(hours=5), 7.0), (RANGE_START - back, 12.5))

    assert await statistics._total_before(None, SID, RANGE_START) == 12.5
    assert len(store.windows) == queries


async def test_windows_do_not_overlap_and_end_unbounded(store):
    """Each window starts where the previous one ended; the last reaches the epoch."""
    await statistics._total_before(None, SID, RANGE_START)

    starts = [start for start, _end, _rows in store.windows]
    ends = [end for _start, end, _rows in store.windows]
    assert ends == [RANGE_START, *starts[:-1]]
    assert starts[:-1] == [RANGE_START - timedelta(days=days) for days in ANCHOR_SEARCH_WINDOWS_DAYS]
    assert starts[-1] == EPOCH


async def test_newest_of_several_earlier_entries_wins(store):
    """Within a window the last row is the one counted on from."""
    _put(
        store,
        (RANGE_START - timedelta(hours=3), 10.0),
        (RANGE_START - timedelta(hours=2), 11.0),
        (RANGE_START - timedelta(hours=1), 12.0),
    )

    assert await statistics._total_before(None, SID, RANGE_START) == 12.0


async def test_entry_without_a_sum_counts_as_zero(store):
    """A row the recorder stored without a sum does not break the search."""
    store.add_external(None, {"statistic_id": SID}, [{"start": RANGE_START - timedelta(hours=1), "sum": None}])

    assert await statistics._total_before(None, SID, RANGE_START) == 0.0


async def test_rewriting_a_recent_day_reads_days_not_years(store):
    """Writing yesterday again over three years of history loads a couple of days of rows."""
    start = datetime(2023, 6, 1, tzinfo=UTC)
    hours = [(start + timedelta(hours=index), float(index + 1)) for index in range(3 * 365 * 24)]
    _put(store, *hours)
    day = [(moment, 1.0) for moment, _total in hours[-48:-24]]
    store.windows.clear()

    total = await statistics.write_cumulative_series(
        None, {"statistic_id": SID, "source": "enea"}, day
    )

    assert total == hours[-25][1]
    assert sum(rows for _start, _end, rows in store.windows) < 100
    assert [row["sum"] for row in store.series(SID)] == [total_ for _moment, total_ in hours]


async def test_rewriting_the_first_day_of_the_history_opens_at_zero(store):
    """A range at the very start of the series counts on from nothing."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    _put(store, *[(start + timedelta(hours=index), float(index + 1)) for index in range(72)])

    await statistics.write_cumulative_series(
        None, {"statistic_id": SID, "source": "enea"},
        [(start + timedelta(hours=index), 2.0) for index in range(24)],
    )

    sums = [row["sum"] for row in store.series(SID)]
    assert sums[:24] == [2.0 * (index + 1) for index in range(24)]
    # The later hours keep their own readings and move up by what the day gained.
    assert sums[24:] == [float(index + 1) + 24.0 for index in range(24, 72)]


async def test_range_older_than_all_history_shifts_everything_after(store):
    """A range before the first stored hour opens at zero and lifts the whole series."""
    start = datetime(2026, 3, 1, tzinfo=UTC)
    _put(store, *[(start + timedelta(hours=index), float(index + 1)) for index in range(24)])
    earlier = datetime(2026, 1, 1, tzinfo=UTC)

    await statistics.write_cumulative_series(
        None, {"statistic_id": SID, "source": "enea"},
        [(earlier + timedelta(hours=index), 1.5) for index in range(4)],
    )

    sums = [row["sum"] for row in store.series(SID)]
    assert sums[:4] == [1.5, 3.0, 4.5, 6.0]
    assert sums[4:] == [float(index + 1) + 6.0 for index in range(24)]


async def test_zone_series_with_a_long_gap_keeps_its_total(store):
    """A zone series silent for months (a seasonal zone) still opens at its old total."""
    winter = datetime(2025, 12, 1, tzinfo=UTC)
    _put(store, (winter, 400.0), (winter + timedelta(hours=1), 401.0))
    summer = datetime(2026, 7, 1, tzinfo=UTC)
    _put(store, (summer, 402.0), (summer + timedelta(hours=1), 403.0))

    await statistics.write_cumulative_series(
        None, {"statistic_id": SID, "source": "enea"},
        [(summer, 3.0), (summer + timedelta(hours=1), 3.0)],
    )

    assert [row["sum"] for row in store.series(SID)] == [400.0, 401.0, 404.0, 407.0]
