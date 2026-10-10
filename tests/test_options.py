"""Changing the options of a set-up meter, through the options flow."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.helpers import entity_registry as er

from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_PPE,
    clock,
    database,
    hass,
    make_entry,
    portal,
)


async def _set_up(hass):
    """Add the meter with the default options and wait for its import."""
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry


async def _change_options(hass, entry, **changes):
    """Submit the options form with changes over the current options."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**entry.options, **changes}
    )
    await hass.async_block_till_done()
    return result


def _state(hass, key: str):
    """Return the state object of the meter's sensor with the given key, None if not there."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", "enea", f"enea-{FAKE_PPE}-{key}")
    assert entity_id is not None, key
    return hass.states.get(entity_id)


async def test_new_interval_reloads_the_entry(hass):
    """The coordinator of the reloaded entry polls at the new interval."""
    entry = await _set_up(hass)
    old = entry.runtime_data.coordinator

    result = await _change_options(hass, entry, update_interval={"hours": 1, "minutes": 15})

    assert result["type"] == "create_entry"
    assert entry.runtime_data.coordinator is not old
    assert entry.runtime_data.coordinator.update_interval == timedelta(hours=1, minutes=15)


async def test_too_short_interval_is_refused(hass):
    """Below 30 minutes the form comes back with an error and nothing reloads."""
    entry = await _set_up(hass)
    old = entry.runtime_data.coordinator

    result = await _change_options(hass, entry, update_interval={"minutes": 10})

    assert result["type"] == "form"
    assert result["errors"]
    assert entry.runtime_data.coordinator is old


async def test_disabling_generation_drops_its_sensors_and_requests(hass, portal):
    """Energy returned is neither updated nor fetched once it is turned off."""
    entry = await _set_up(hass)
    assert _state(hass, "generation_total") is not None
    portal.requests.clear()

    await _change_options(hass, entry, fetch_generation=False)
    await entry.runtime_data.coordinator.async_refresh()

    # The registry keeps the entity, which Home Assistant shows as no longer provided.
    returned = _state(hass, "generation_total")
    assert (returned.state, returned.attributes.get("restored")) == ("unavailable", True)
    assert _state(hass, "consumption_total") is not None
    assert all(r[4] != 5 for r in portal.requests if r[0] == "range")
