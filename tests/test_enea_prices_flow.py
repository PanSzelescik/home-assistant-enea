"""The meter together with an installed enea_prices integration.

The stand-in in tests/fake_integrations is found by Home Assistant's loader
like the real one: its tariff table tells whether it can price the meter's
group, its config flow adds the entry, and setting an entry up reloads the
Enea meters of the group.
"""
from __future__ import annotations

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.enea.costs import _kwh_price
from custom_components.enea.repairs import async_create_fix_flow
from ha_instance import (  # noqa: F401 - the imported fixtures are used by name
    FAKE_PPE,
    clock,
    database,
    enea_prices_installed,
    hass,
    make_entry,
    portal,
)

pytestmark = pytest.mark.usefixtures("enea_prices_installed")

SETTINGS = {"tariff": "G12", "phases": 3, "annual_kwh": 2000, "billing_months": 2}


async def _add(hass, entry):
    """Add a config entry and wait for everything it starts."""
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry


async def _fix(hass, issue_id: str):
    """Run the fix flow of an issue to the end, confirming it."""
    issue = ir.async_get(hass).async_get_issue("enea", issue_id)
    assert issue is not None and issue.is_fixable
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass, flow.handler, flow.issue_id = hass, "enea", issue_id
    assert (await flow.async_step_init())["type"] is FlowResultType.FORM
    return await flow.async_step_confirm({})


def _has_bills(hass) -> bool:
    """Return whether the meter has its bill estimate entities."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id("sensor", "enea", f"enea-{FAKE_PPE}-bill_current") is not None


async def test_installed_but_not_configured_prices_are_offered_and_set_up(hass):
    """The repair opens enea_prices' config flow; adding the entry brings the bills."""
    entry = await _add(hass, make_entry())
    issue_id = f"prices_not_configured_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue("enea", issue_id) is not None
    assert not _has_bills(hass)

    result = await _fix(hass, issue_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    flow_type, flow_id = result["next_flow"]
    created = await hass.config_entries.flow.async_configure(flow_id, SETTINGS)
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue("enea", issue_id) is None
    assert _has_bills(hass)


async def test_group_the_prices_cannot_price_is_not_offered(hass, portal):
    """enea_prices without the meter's group in its table raises nothing."""
    portal.dashboard["tariffGroupName"] = "C11"

    entry = await _add(hass, make_entry())

    assert ir.async_get(hass).async_get_issue("enea", f"prices_not_configured_{entry.entry_id}") is None


async def test_phases_fix_reloads_prices_and_the_meter_clears_its_issue(hass):
    """Confirming the fix writes three phases; enea_prices' reload reloads the meter."""
    prices = await _add(hass, _prices_entry(phases=1))
    entry = await _add(hass, make_entry())
    issue_id = f"phases_mismatch_{entry.entry_id}"
    old = entry.runtime_data.coordinator

    await _fix(hass, issue_id)
    await hass.async_block_till_done()

    assert prices.data["phases"] == 3
    assert prices.runtime_data.phases == 3
    assert entry.runtime_data.coordinator is not old
    assert ir.async_get(hass).async_get_issue("enea", issue_id) is None


async def test_excise_duty_from_enea_prices_goes_into_the_costs(hass, database):
    """AKCYZA of the installed enea_prices is added to each kWh's price."""
    prices = await _add(hass, _prices_entry())
    await _add(hass, make_entry())
    period = prices.runtime_data.tariff.periods[0]

    day = database.series(f"enea:{FAKE_PPE}_koszt_energii_pobrana_dzien")
    kwh = database.series(f"enea:{FAKE_PPE}_energia_pobrana_dzien")

    with_duty = _kwh_price(period.zones["day"], 0.005)
    assert with_duty > _kwh_price(period.zones["day"], 0.0)
    assert day[-1]["sum"] == pytest.approx(kwh[-1]["sum"] * with_duty, rel=1e-6)


def _prices_entry(**settings):
    """Return an enea_prices config entry for G12 with the given settings changed."""
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigEntry

    return ConfigEntry(
        data={**SETTINGS, **settings},
        discovery_keys=MappingProxyType({}),
        domain="enea_prices",
        minor_version=1,
        options={},
        source="user",
        subentries_data=None,
        title="Ceny G12",
        unique_id="G12",
        version=1,
    )
