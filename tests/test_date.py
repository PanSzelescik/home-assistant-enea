"""Billing dates survive restarts and update the correct billing boundary."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.enea import date as module
from conftest import FakeConfigEntry, FakeHass


@pytest.fixture
def coordinator():
    """Provide both reading boundaries and the bill recomputation collaborator."""
    return SimpleNamespace(
        data={"tariffGroupName": "G12", "meters": []},
        tariff_name="G12",
        bill_prev_reading=date(2026, 6, 5),
        bill_last_reading=date(2026, 8, 5),
        async_recompute_bills=AsyncMock(),
    )


@pytest.mark.parametrize("tariff,loaded,expected", [
    pytest.param("g12", True, True, id="matching-case-insensitive"),
    pytest.param("G11", True, False, id="different-tariff"),
    pytest.param("G12", False, False, id="prices-not-loaded"),
    pytest.param(None, False, False, id="no-prices-entry"),
])
async def test_dates_require_matching_loaded_prices(coordinator, tariff, loaded, expected):
    """Only matching, loaded prices enable both editable billing boundaries."""
    entries = [] if tariff is None else [FakeConfigEntry(
        domain="enea_prices", data={"tariff": tariff},
        runtime_data=SimpleNamespace(tariff=object() if loaded else None),
    )]
    entry = FakeConfigEntry(
        domain="enea", data={"meter_name": "590310600000001234"},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    add = Mock()

    await module.async_setup_entry(FakeHass(entries), entry, add)

    if not expected:
        add.assert_not_called()
        return
    add.assert_called_once()
    entities = add.call_args.args[0]
    assert [entity.unique_id for entity in entities] == [
        "enea-590310600000001234-bill_prev_reading",
        "enea-590310600000001234-bill_last_reading",
    ]
    assert [entity.native_value for entity in entities] == [None, None]
    assert all(entity.device_info["identifiers"] == {("enea", "590310600000001234")} for entity in entities)


@pytest.mark.parametrize("key", ["bill_prev_reading", "bill_last_reading"])
async def test_edit_updates_only_selected_boundary_before_recomputing(coordinator, key):
    """Editing a date updates state and recomputes using the new boundary."""
    entity = module.EneaBillDateEntity(coordinator, "590310600000001234", key)
    observed = []

    async def recompute():
        """Capture the boundaries visible to bill computation."""
        observed.append((coordinator.bill_prev_reading, coordinator.bill_last_reading))

    coordinator.async_recompute_bills.side_effect = recompute
    entity.async_write_ha_state = Mock()

    await entity.async_set_value(date(2026, 9, 5))

    assert entity.native_value == date(2026, 9, 5)
    assert observed == [
        (date(2026, 9, 5), date(2026, 8, 5)) if key == "bill_prev_reading"
        else (date(2026, 6, 5), date(2026, 9, 5)),
    ]
    entity.async_write_ha_state.assert_called_once_with()
    coordinator.async_recompute_bills.assert_awaited_once_with()


@pytest.mark.parametrize("key", ["bill_prev_reading", "bill_last_reading"])
@pytest.mark.parametrize("stored,expected", [
    pytest.param("2026-07-05", date(2026, 7, 5), id="valid"),
    pytest.param(None, None, id="no-state"),
    pytest.param("unknown", None, id="unknown"),
    pytest.param("unavailable", None, id="unavailable"),
    pytest.param("2026-02-30", None, id="invalid-date"),
    pytest.param(123, None, id="wrong-type"),
])
async def test_restore_handles_saved_and_invalid_dates(coordinator, monkeypatch, key, stored, expected):
    """Restore updates only its boundary; absent or corrupt state stays unset."""
    coordinator.bill_prev_reading = None
    coordinator.bill_last_reading = None
    entity = module.EneaBillDateEntity(coordinator, "590310600000001234", key)
    monkeypatch.setattr(module.RestoreEntity, "async_added_to_hass", AsyncMock())
    entity.async_get_last_state = AsyncMock(
        return_value=None if stored is None else SimpleNamespace(state=stored),
    )
    tasks = []

    def schedule(coro):
        """Schedule and retain the real coroutine so the test can drain it."""
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    entity.hass = SimpleNamespace(async_create_task=schedule)

    await entity.async_added_to_hass()
    await asyncio.gather(*tasks)

    assert entity.native_value == expected
    assert coordinator.bill_prev_reading == (expected if key == "bill_prev_reading" else None)
    assert coordinator.bill_last_reading == (expected if key == "bill_last_reading" else None)
    assert len(tasks) == 1
    coordinator.async_recompute_bills.assert_awaited_once_with()
