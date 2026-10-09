"""A refresh during startup must not wait for the recorder.

The recorder thread commits nothing before Home Assistant has started, and
startup waits for this integration's setup — which runs the first refresh.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import CoreState

from custom_components.enea import coordinator as coordinator_module
from custom_components.enea.coordinator import EneaUpdateCoordinator


@pytest.fixture
def coordinator(monkeypatch: pytest.MonkeyPatch):
    """Return a coordinator whose statistics step and startup hook are recorded."""
    calls: dict[str, list[Any]] = {"statistics": [], "at_started": [], "unload": []}

    async def get_ppe_dashboard(meter_id: int) -> dict[str, Any]:
        return {"tariffGroupName": "G12", "meters": []}

    async def update_statistics() -> None:
        calls["statistics"].append("run")

    def at_started(hass: Any, callback: Any) -> Any:
        calls["at_started"].append(callback)
        return lambda: None

    monkeypatch.setattr(coordinator_module, "async_at_started", at_started, raising=False)
    monkeypatch.setattr(coordinator_module, "async_update_issues", lambda *args: None)

    coord = object.__new__(EneaUpdateCoordinator)
    coord.hass = SimpleNamespace(state=CoreState.starting)  # type: ignore[assignment]
    coord.config_entry = SimpleNamespace(  # type: ignore[assignment]
        async_on_unload=lambda unsub: calls["unload"].append(unsub)
    )
    coord.client = SimpleNamespace(get_ppe_dashboard=get_ppe_dashboard)  # type: ignore[assignment]
    coord.meter_id = 1
    coord._entry_id = "entry1"
    coord._meter_code = "PPE"
    coord._statistics_deferred = False
    coord._async_update_statistics = update_statistics  # type: ignore[method-assign]
    return coord, calls


async def test_startup_refresh_defers_statistics(coordinator) -> None:
    """During startup the dashboard data is returned and statistics wait for the start."""
    coord, calls = coordinator

    data = await coord._async_update_data()

    assert data["tariffGroupName"] == "G12"
    assert calls["statistics"] == []
    assert len(calls["at_started"]) == 1
    assert len(calls["unload"]) == 1


async def test_deferred_statistics_are_scheduled_once(coordinator) -> None:
    """A second refresh before the start does not schedule the step again."""
    coord, calls = coordinator

    await coord._async_update_data()
    await coord._async_update_data()

    assert len(calls["at_started"]) == 1


async def test_running_refresh_updates_statistics_inline(coordinator) -> None:
    """Once Home Assistant runs, the statistics step is part of the refresh."""
    coord, calls = coordinator
    coord.hass.state = CoreState.running

    await coord._async_update_data()

    assert calls["statistics"] == ["run"]
    assert calls["at_started"] == []
