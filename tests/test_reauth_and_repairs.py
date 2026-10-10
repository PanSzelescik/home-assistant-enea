"""Getting a meter going again: new credentials after a rejected login, and Repairs fixes."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState, SOURCE_REAUTH
from homeassistant.helpers import issue_registry as ir

from custom_components.enea import config_flow
from custom_components.enea.connector import EneaAuthError
from custom_components.enea.repairs import async_create_fix_flow
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    clock,
    database,
    hass,
    make_entry,
    make_prices_entry,
    portal,
    register_prices_entry,
)


async def test_new_password_from_the_reauth_flow_sets_the_meter_up(hass, portal, monkeypatch):
    """A rejected login asks for credentials; the new ones are saved and the entry loads."""
    monkeypatch.setattr(config_flow, "EneaApiClient", lambda *args: portal)
    monkeypatch.setattr(
        config_flow, "async_create_clientsession", lambda hass: SimpleNamespace(close=AsyncMock())
    )
    portal.fail_with = EneaAuthError("rejected")
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    [flow] = hass.config_entries.flow.async_progress_by_handler("enea")
    assert flow["context"]["source"] == SOURCE_REAUTH

    portal.fail_with = None
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"username": "test@example.invalid", "password": "new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-password"
    assert entry.state is ConfigEntryState.LOADED


async def test_repair_writes_the_meters_value_into_enea_prices(hass, monkeypatch):
    """Fixing the phases issue sets enea_prices to three phases; the issue then goes away."""
    prices = make_prices_entry()
    prices.runtime_data.phases = 1
    register_prices_entry(hass, prices)
    reloaded = []
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(side_effect=reloaded.append))
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    issue_id = f"phases_mismatch_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue("enea", issue_id)
    assert issue is not None and issue.is_fixable

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass, flow.handler, flow.issue_id = hass, "enea", issue_id
    form = await flow.async_step_init()
    assert form["type"] == "form"
    result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    assert prices.data["phases"] == 3
    assert reloaded == [prices.entry_id]

    prices.runtime_data.phases = prices.data["phases"]  # what reloading enea_prices does
    await entry.runtime_data.coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue("enea", issue_id) is None
