"""The fixture meter reported as a prosumer's: energy after balancing, power without.

A prosumer's invoice is settled from the data after balancing, which the
portal serves when asked for in a sixth path segment; power has no balanced
data.  History imported before balanced data was used is imported again,
once.
"""
from __future__ import annotations

from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntryState

from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    SAVED_ON,
    clock,
    database,
    hass,
    make_entry,
    portal,
)

ASSEMBLY_DAY = date(2026, 3, 20)
YESTERDAY = SAVED_ON - timedelta(days=1)
BALANCED = 2


def _sources(portal) -> dict[int, set]:
    """Return the data sources asked for, by measurement type."""
    found: dict[int, set] = {}
    for request in portal.requests:
        if request[0] == "range":
            found.setdefault(request[4], set()).add(request[5])
    return found


async def _set_up(hass, entry) -> None:
    """Add the entry and wait for its setup and background import."""
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()


async def test_prosumer_asks_for_balanced_energy_and_plain_power(hass, portal):
    """Energy consumed and returned come after balancing; power stays as it is."""
    portal.prosumer = True
    entry = make_entry(fetch_power_consumption=True, fetch_power_generation=True)

    await _set_up(hass, entry)

    assert _sources(portal) == {1: {BALANCED}, 5: {BALANCED}, 4: {None}, 9: {None}}
    assert entry.data["balanced_history"] is True


async def test_history_from_before_balancing_is_imported_again_once(hass, portal):
    """A meter that became a prosumer's re-imports its history once, without a reload."""
    entry = make_entry()
    await _set_up(hass, entry)
    assert _sources(portal) == {1: {None}, 5: {None}}
    assert await hass.config_entries.async_unload(entry.entry_id)
    portal.prosumer = True
    portal.requests.clear()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    ranges = [r[2:] for r in portal.requests if r[0] == "range"]
    assert ranges[0] == (ASSEMBLY_DAY, ASSEMBLY_DAY + timedelta(days=179), 1, BALANCED)
    assert ranges[-1][1] == YESTERDAY
    assert entry.data["balanced_history"] is True
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator is coordinator  # the flag did not reload the entry

    portal.requests.clear()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [r for r in portal.requests if r[0] == "range"] == []
