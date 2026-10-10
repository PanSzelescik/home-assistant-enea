"""Two meters on one account: their own statistics, prices and repairs.

The second meter answers with the fixture data under its own id and PPE
number.  What one meter configures in enea_prices must not leak into the
other, and a setting two meters share must not be "fixed" for one of them.
"""
from __future__ import annotations

from homeassistant.helpers import entity_registry as er, issue_registry as ir

from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_PPE,
    clock,
    database,
    hass,
    make_entry,
    make_prices_entry,
    portal,
    register_prices_entry,
)

SECOND_ID = 12346
SECOND_PPE = "590310600000005678"


def _second_meter(portal, tariff: str) -> None:
    """Add a second meter to the account, in the given tariff group."""
    dashboard = {
        **portal.dashboard,
        "id": SECOND_ID,
        "code": SECOND_PPE,
        "name": SECOND_PPE,
        "tariffGroupName": tariff,
        "agreements": [{**a, "tariffGroupName": tariff} for a in portal.dashboard["agreements"]],
    }
    portal.dashboards[SECOND_ID] = dashboard


async def _set_up_both(hass):
    """Add both meters' entries and wait for their imports."""
    entries = [make_entry(), make_entry(meter_name=SECOND_PPE, meter_id=SECOND_ID)]
    for entry in entries:
        await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entries


def _has_entity(hass, ppe: str, key: str, domain: str = "sensor") -> bool:
    """Return whether the meter has the entity with the given key."""
    return er.async_get(hass).async_get_entity_id(domain, "enea", f"enea-{ppe}-{key}") is not None


async def test_each_meter_keeps_its_own_statistics_and_prices(hass, portal, database):
    """Only the G12 meter has bills and costs when enea_prices prices G12 alone."""
    _second_meter(portal, "G11")
    register_prices_entry(hass, make_prices_entry())

    first, second = await _set_up_both(hass)

    assert first.runtime_data.coordinator.tariff_name == "G12"
    assert second.runtime_data.coordinator.tariff_name == "G11"
    for ppe in (FAKE_PPE, SECOND_PPE):
        assert database.series(f"enea:{ppe}_energia_pobrana")
    assert database.series(f"enea:{FAKE_PPE}_koszt_energii_pobrana_dzien")
    assert not [sid for sid in database.rows if sid.startswith(f"enea:{SECOND_PPE}_koszt")]
    assert _has_entity(hass, FAKE_PPE, "bill_current")
    assert _has_entity(hass, FAKE_PPE, "bill_last_reading", "date")
    assert not _has_entity(hass, SECOND_PPE, "bill_current")
    assert not _has_entity(hass, SECOND_PPE, "bill_last_reading", "date")
    assert {(r[1], r[4]) for r in portal.requests if r[0] == "range"} == {
        (meter, mtype) for meter in (12345, SECOND_ID) for mtype in (1, 5)
    }


async def test_a_setting_two_meters_share_is_not_raised_as_a_repair(hass, portal):
    """Two G12 meters share one enea_prices entry, so a phases fix for one would break the other."""
    _second_meter(portal, "G12")
    prices = make_prices_entry()
    prices.runtime_data.phases = 1
    register_prices_entry(hass, prices)

    await _set_up_both(hass)

    assert not [issue for (domain, issue) in ir.async_get(hass).issues if domain == "enea"]


async def test_a_meter_alone_in_its_group_gets_the_repair(hass, portal):
    """With the second meter in another group the G12 meter's mismatch is raised again."""
    _second_meter(portal, "G11")
    prices = make_prices_entry()
    prices.runtime_data.phases = 1
    register_prices_entry(hass, prices)

    first, _ = await _set_up_both(hass)

    issues = {issue for (domain, issue) in ir.async_get(hass).issues if domain == "enea"}
    assert issues == {f"phases_mismatch_{first.entry_id}"}
