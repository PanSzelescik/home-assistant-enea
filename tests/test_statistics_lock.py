"""One writer of a meter's statistics at a time.

Every write counts its running total on from what a database read returns.
A refresh, the import in the background and the enea.backfill action running
into each other would each count on from a total the other is about to
change, so they take turns.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest

from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_PPE,
    NOW,
    FakePortal,
    clock,
    database,
    hass,
    make_entry,
    portal,
)

CONSUMED = f"enea:{FAKE_PPE}_energia_pobrana"
WEEK = {"start_date": "2026-03-27", "end_date": "2026-04-02"}
OTHER_WEEK = {"start_date": "2026-05-01", "end_date": "2026-05-07"}


async def _spin() -> None:
    """Let every task that can run get as far as it can."""
    for _ in range(30):
        await asyncio.sleep(0)


async def _set_up(hass, wait: bool = True) -> Any:
    """Add the fixture meter's entry; wait for its import unless told not to."""
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    if wait:
        await hass.async_block_till_done(wait_background_tasks=True)
    else:
        await _spin()
    return entry


def _ranges(portal: FakePortal) -> list[tuple[Any, ...]]:
    """Return the range requests made so far, without the meter id."""
    return [request[2:] for request in portal.requests if request[0] == "range"]


def _backfill(hass, data: dict[str, Any]) -> asyncio.Task[Any]:
    """Start the enea.backfill action without waiting for it."""
    return asyncio.ensure_future(hass.services.async_call("enea", "backfill", data, blocking=True))


def _assert_continuous(series: list[dict[str, Any]]) -> None:
    """Every hour's total is the total before it plus the hour's own reading."""
    total = 0.0
    for row in series:
        total += row["state"]
        assert row["sum"] == pytest.approx(total)


async def test_import_is_a_background_task_of_the_entry(hass, portal):
    """Home Assistant cancels it with the entry and does not wait for it when stopping."""
    portal.gate = asyncio.Event()
    entry = await _set_up(hass, wait=False)
    task = entry.runtime_data.coordinator._backfill_task

    assert task is not None and not task.done()
    assert task in entry._background_tasks
    assert task not in hass._tasks
    assert FAKE_PPE not in task.get_name()
    assert task.get_name().endswith("…1234")
    portal.gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_stopping_does_not_wait_for_the_import(hass, portal):
    """Waiting for Home Assistant's own tasks returns while the import is still held."""
    portal.gate = asyncio.Event()
    entry = await _set_up(hass, wait=False)

    await asyncio.wait_for(hass.async_block_till_done(), timeout=5)

    assert entry.runtime_data.coordinator.diagnostics_state()["initial_backfill"] == "running"
    portal.gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_backfill_action_waits_for_the_import(hass, portal, database):
    """The action started during the import fetches nothing until the import is done."""
    portal.gate = asyncio.Event()
    entry = await _set_up(hass, wait=False)
    coordinator = entry.runtime_data.coordinator
    held = len(_ranges(portal))
    assert held and coordinator._statistics_lock.locked()

    action = _backfill(hass, WEEK)
    await _spin()

    assert not action.done()
    assert len(_ranges(portal)) == held
    portal.gate.set()
    await action
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.diagnostics_state()["initial_backfill"] == "done"
    assert _ranges(portal)[-2:] == [
        (date(2026, 3, 27), date(2026, 4, 2), 1, None),
        (date(2026, 3, 27), date(2026, 4, 2), 5, None),
    ]
    _assert_continuous(database.series(CONSUMED))


async def test_two_backfill_actions_take_turns(hass, portal, database):
    """The second action asks the portal for nothing while the first one is under way."""
    await _set_up(hass)
    before = database.series(CONSUMED)
    portal.requests.clear()
    portal.gate = asyncio.Event()

    first = _backfill(hass, WEEK)
    await _spin()
    second = _backfill(hass, OTHER_WEEK)
    await _spin()

    assert [request[0] for request in _ranges(portal)] == [date(2026, 3, 27)] * 2
    portal.gate.set()
    await asyncio.gather(first, second)
    assert [request[0] for request in _ranges(portal)] == [
        date(2026, 3, 27), date(2026, 3, 27), date(2026, 5, 1), date(2026, 5, 1),
    ]
    assert database.series(CONSUMED) == before


async def test_refresh_waits_for_the_backfill_action(hass, portal, database, clock):
    """A refresh bringing in a new day lets the action finish its writes first."""
    entry = await _set_up(hass)
    coordinator = entry.runtime_data.coordinator
    clock.now = NOW + timedelta(days=3)
    portal.requests.clear()
    portal.gate = asyncio.Event()

    action = _backfill(hass, WEEK)
    await _spin()
    refresh = asyncio.ensure_future(coordinator.async_refresh())
    await _spin()

    # The dashboard is read at once; the statistics step is what waits.
    assert ("dashboard", entry.data["meter_id"]) in portal.requests
    assert not refresh.done()
    assert len(_ranges(portal)) == 2
    portal.gate.set()
    await asyncio.gather(action, refresh)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert coordinator.last_update_success
    assert len(_ranges(portal)) > 2
    assert not coordinator._statistics_lock.locked()
    _assert_continuous(database.series(CONSUMED))


async def test_refresh_does_not_wait_for_the_import(hass, portal):
    """During the import in the background a refresh returns with the dashboard at once."""
    portal.gate = asyncio.Event()
    entry = await _set_up(hass, wait=False)
    coordinator = entry.runtime_data.coordinator

    await asyncio.wait_for(coordinator.async_refresh(), timeout=5)

    assert coordinator.last_update_success
    assert coordinator.diagnostics_state()["initial_backfill"] == "running"
    portal.gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_failed_backfill_action_releases_the_lock(hass, portal):
    """A portal error in the action leaves the next refresh free to write."""
    from custom_components.enea.connector import EneaApiError

    entry = await _set_up(hass)
    coordinator = entry.runtime_data.coordinator
    portal.fail_ranges_with = EneaApiError("offline")

    with pytest.raises(EneaApiError):
        await hass.services.async_call("enea", "backfill", WEEK, blocking=True)

    assert not coordinator._statistics_lock.locked()
    portal.fail_ranges_with = None
    await coordinator.async_refresh()
    assert coordinator.diagnostics_state()["statistics_error"] is None


async def test_unloading_during_a_held_backfill_action_cancels_nothing_else(hass, portal):
    """Unloading cancels the import; an action waiting for it gets the lock and finishes."""
    portal.gate = asyncio.Event()
    entry = await _set_up(hass, wait=False)
    coordinator = entry.runtime_data.coordinator
    action = _backfill(hass, WEEK)
    await _spin()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await _spin()
    assert coordinator.diagnostics_state()["initial_backfill"] == "cancelled"
    portal.gate.set()
    await asyncio.wait_for(action, timeout=5)

    assert not coordinator._statistics_lock.locked()
