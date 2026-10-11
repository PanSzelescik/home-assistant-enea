"""Home Assistant restarting with the meter set up, enea_prices configured and dates set.

The first run sets the meter up from scratch; the second starts from what the
first left in its config directory — config entries, the entity registry,
restored states and the stored price fingerprints — and the same long-term
statistics, as a restart does.
"""
from __future__ import annotations

from datetime import date, timedelta

from homeassistant.helpers import entity_registry as er

from ha_instance import (  # noqa: F401 - clock, portal and database are fixtures
    FAKE_PPE,
    NOW,
    SAVED_ON,
    FakeG12Tariff,
    async_start_hass,
    clock,
    database,
    make_entry,
    make_prices_entry,
    portal,
    register_prices_entry,
)

YESTERDAY = SAVED_ON - timedelta(days=1)
ASSEMBLY_DAY = date(2026, 3, 20)


def _entity_id(hass, domain: str, key: str) -> str:
    """Return the entity id of the meter's entity with the given key."""
    entity_id = er.async_get(hass).async_get_entity_id(domain, "enea", f"enea-{FAKE_PPE}-{key}")
    assert entity_id is not None, key
    return entity_id


def _ranges(portal) -> list[tuple]:
    """Return the range requests made so far, without the meter id."""
    return [request[2:] for request in portal.requests if request[0] == "range"]


async def _first_run(config_dir: str, tariff: FakeG12Tariff) -> dict[str, str]:
    """Set the meter up, set both reading dates and stop; return the bill states."""
    hass = await async_start_hass(config_dir)
    register_prices_entry(hass, make_prices_entry(tariff))
    await hass.config_entries.async_add(make_entry())
    await hass.async_block_till_done()
    for key, value in (("bill_prev_reading", "2026-06-06"), ("bill_last_reading", "2026-08-05")):
        await hass.services.async_call(
            "date", "set_value", {"entity_id": _entity_id(hass, "date", key), "date": value},
            blocking=True,
        )
    await hass.async_block_till_done()
    bills = {
        key: hass.states.get(_entity_id(hass, "sensor", key)).state
        for key in ("bill_previous", "bill_current")
    }
    await hass.async_stop()
    return bills


async def _second_run(config_dir: str, tariff: FakeG12Tariff):
    """Start again from the same config directory and set the stored entry up."""
    hass = await async_start_hass(config_dir)
    register_prices_entry(hass, make_prices_entry(tariff))
    [entry] = hass.config_entries.async_entries("enea")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass, entry


async def test_restart_picks_up_where_it_left_off(tmp_path, portal, database):
    """Dates and bills come back, and nothing already imported or costed is fetched again."""
    tariff = FakeG12Tariff()
    bills = await _first_run(str(tmp_path), tariff)
    assert all(float(value) > 0 for value in bills.values())
    portal.requests.clear()

    hass, entry = await _second_run(str(tmp_path), tariff)
    try:
        coordinator = entry.runtime_data.coordinator
        assert hass.states.get(_entity_id(hass, "date", "bill_prev_reading")).state == "2026-06-06"
        assert hass.states.get(_entity_id(hass, "date", "bill_last_reading")).state == "2026-08-05"
        assert {
            key: hass.states.get(_entity_id(hass, "sensor", key)).state for key in bills
        } == bills
        assert _ranges(portal) == []
        assert coordinator.statistics_until == YESTERDAY
        state = coordinator.diagnostics_state()
        assert state["initial_backfill"] == "not_started"
        assert state["costs_repriced_from"] is None
    finally:
        await hass.async_stop()


async def test_restart_on_a_later_day_fetches_only_the_new_days(tmp_path, portal, database, clock):
    """Two days after the first run the restart asks only for the days since."""
    tariff = FakeG12Tariff()
    await _first_run(str(tmp_path), tariff)
    portal.requests.clear()
    clock.now = NOW + timedelta(days=2)

    hass, entry = await _second_run(str(tmp_path), tariff)
    try:
        assert _ranges(portal) == [
            (SAVED_ON, SAVED_ON + timedelta(days=1), 1, None),
            (SAVED_ON, SAVED_ON + timedelta(days=1), 5, None),
        ]
        assert entry.runtime_data.coordinator.diagnostics_state()["costs_repriced_from"] is None
    finally:
        await hass.async_stop()


async def test_prices_changed_while_stopped_reprice_after_the_restart(tmp_path, portal, database):
    """New prices found at startup re-price the stored history once."""
    tariff = FakeG12Tariff()
    await _first_run(str(tmp_path), tariff)
    tariff.periods[0].zones["night"].energy *= 2
    portal.requests.clear()

    hass, entry = await _second_run(str(tmp_path), tariff)
    try:
        assert entry.runtime_data.coordinator.diagnostics_state()["costs_repriced_from"] == "2026-03-20"
        assert _ranges(portal)[0][:2] == (ASSEMBLY_DAY, ASSEMBLY_DAY + timedelta(days=179))
    finally:
        await hass.async_stop()
