"""Test fixtures for the Enea integration.

The integration is exercised without starting Home Assistant.  Its modules
import cleanly on their own, and every collaborator they reach for — the
recorder helpers and the config entry registry — is a module-level name that
a test can replace.  That keeps the suite fast and free of a version-pinned
Home Assistant test harness.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_TIME_ZONE = "Europe/Warsaw"
"""The zone a real installation of this integration runs in."""


@pytest.fixture(autouse=True)
def _local_time_zone() -> Any:
    """Pin the zone the integration treats as local.

    Every date the integration reports comes from converting a UTC timestamp
    into dt_util.DEFAULT_TIME_ZONE, which Home Assistant sets from the user's
    configuration and which is plain UTC in a bare test process.  Without
    pinning it, a test that builds its input in one zone and compares dates
    produced in another is only right while the two happen to agree, and turns
    red for a couple of hours around midnight.
    """
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(dt_util.get_time_zone(TEST_TIME_ZONE))
    yield
    dt_util.set_default_time_zone(previous)


@dataclass
class FakeConfigEntry:
    """Stand-in for a Home Assistant config entry."""

    domain: str
    data: dict[str, Any] = field(default_factory=dict)
    runtime_data: Any = None
    entry_id: str = "entry"


class _FakeConfigEntries:
    """Minimal hass.config_entries surface used by the integration."""

    def __init__(self, entries: list[FakeConfigEntry]) -> None:
        self._entries = entries
        self.reloaded: list[str] = []

    def async_entries(self, domain: str) -> list[FakeConfigEntry]:
        """Return the entries registered for a domain."""
        return [e for e in self._entries if e.domain == domain]

    async def async_reload(self, entry_id: str) -> None:
        """Record a reload request instead of performing one."""
        self.reloaded.append(entry_id)


class FakeHass:
    """Minimal hass object: config entries plus a synchronous task runner."""

    def __init__(self, entries: list[FakeConfigEntry] | None = None) -> None:
        self.config_entries = _FakeConfigEntries(entries or [])

    def async_create_task(self, coro: Any, name: str | None = None) -> None:
        """Run the coroutine immediately so tests stay deterministic."""
        import asyncio

        asyncio.get_event_loop().run_until_complete(coro)


class _FakeRecorderInstance:
    """Runs executor jobs inline."""

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        """Call the target directly rather than in a thread."""
        return target(*args)


@pytest.fixture
def recorder_instance() -> _FakeRecorderInstance:
    """Return a recorder instance whose executor jobs run inline."""
    return _FakeRecorderInstance()


class FakeRecorder:
    """Answers recorder queries from an in-memory series.

    Rows are (hour, running total) or (hour, running total, that hour's state).
    """

    def __init__(self, stored: list[tuple[Any, ...]]) -> None:
        self.stored = sorted(
            ((row[0], row[1], row[2] if len(row) > 2 else None) for row in stored),
            key=lambda row: row[0],
        )

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        """Run the query straight away instead of handing it to a thread."""
        return target(*args)

    async def async_block_till_done(self) -> None:
        """Waiting for queued writes is instant here: writes apply at once."""

    def newest(self, hass: Any, count: int, sid: str, convert: bool, types: set) -> dict:
        """Stand in for get_last_statistics: the last hour of the whole series."""
        if not self.stored:
            return {}
        hour, total, _state = self.stored[-1]
        return {sid: [{"start": hour.timestamp(), "sum": total}]}

    def merge(self, rows: list[Any]) -> None:
        """Apply a write the way the recorder does: replace entries by start time."""
        merged = {row[0].timestamp(): row for row in self.stored}
        for row in rows:
            merged[row["start"].timestamp()] = (
                row["start"], row.get("sum"), row.get("state")
            )
        self.stored = sorted(merged.values(), key=lambda row: row[0])

    def in_window(self, hass: Any, start: Any, end: Any, ids: set, *rest: Any) -> dict:
        """Stand in for statistics_during_period: the hours inside [start, end).

        An end of None means no upper bound, as the recorder reads it.
        """
        sid = next(iter(ids))
        found = [
            {"start": hour.timestamp(), "sum": total, "state": state}
            for hour, total, state in self.stored
            if start <= hour and (end is None or hour < end)
        ]
        return {sid: found} if found else {}


@dataclass
class StatsStore:
    """Captures what the integration writes, and feeds it back into the series.

    async_add_external_statistics replaces stored entries by start time, so a
    write is merged into the series the same way.  Without that a test can only
    ask what one call wrote, never what the call left the rest of the series
    looking like.
    """

    recorder: FakeRecorder
    injected: list[tuple[Any, list[Any]]] = field(default_factory=list)

    def add_external(self, hass: Any, metadata: Any, rows: list[Any]) -> None:
        """Record an async_add_external_statistics call and apply it."""
        self.injected.append((metadata, rows))
        self.recorder.merge(rows)

    @property
    def totals(self) -> list[float]:
        """The running total of every entry now stored, oldest first."""
        return [total for _hour, total, _state in self.recorder.stored]


@pytest.fixture
def wire_recorder(monkeypatch: pytest.MonkeyPatch):
    """Point one module's recorder calls at an in-memory series and capture writes.

    Call it as wire_recorder(module, stored) and it returns the StatsStore that
    collects what the code writes back.

    Every name is replaced with raising=False, including the ones the code
    under test does not reach for.  That is what lets a regression test be
    checked against the revision before its fix, where the code looks up a
    different set of names, and still fail on the behaviour it is about rather
    than on a missing stand-in.  A replacement that genuinely fails to apply
    still shows up at once, because the real recorder call raises without a
    running Home Assistant behind it.
    """

    def _wire(module: Any, stored: list[tuple[Any, float]]) -> StatsStore:
        recorder = FakeRecorder(stored)
        store = StatsStore(recorder)
        monkeypatch.setattr(module, "get_instance", lambda hass: recorder, raising=False)
        monkeypatch.setattr(module, "get_last_statistics", recorder.newest, raising=False)
        monkeypatch.setattr(
            module, "statistics_during_period", recorder.in_window, raising=False
        )
        monkeypatch.setattr(
            module, "async_add_external_statistics", store.add_external, raising=False
        )
        return store

    return _wire
