"""The fixture meter reported as a prosumer's: energy after balancing, power without.

A prosumer's invoice is settled from the data after balancing, which the
portal serves when asked for in a sixth path segment; power has no balanced
data.  History imported before balanced data was used is imported again,
once.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from datetime import date, timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.enea.costs import _kwh_price
from custom_components.enea.parsing import slot_start_dt
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_PPE,
    SAVED_ON,
    FakeG12Tariff,
    at_local,
    clock,
    database,
    hass,
    make_entry,
    make_prices_entry,
    portal,
    register_prices_entry,
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


def _returned_from_consumption(portal, share: float) -> dict[str, float]:
    """Serve energy returned as a share of every consumed hour; return its kWh by G12 zone.

    The fixture meter has no generation, so its consumption, scaled, stands in
    for a prosumer's export — same hours, zones and gaps.
    """
    consumed = copy.deepcopy(portal.ranges[1][0])
    zone_names = {zone["id"]: zone["name"] for zone in consumed["zones"]}
    totals: dict[str, float] = defaultdict(float)
    start = at_local(ASSEMBLY_DAY, 12)
    end = at_local(SAVED_ON)
    for entry in consumed["valuesToTable"]:
        for item in entry["items"]:
            if item["value"] is not None:
                item["value"] = round(item["value"] * share, 6)
                if start <= slot_start_dt(entry) < end:
                    totals[zone_names[item["tarifZoneId"]]] += item["value"]
    portal.ranges[5] = [consumed]
    return totals


async def test_net_metering_credits_returned_energy(hass, portal, database):
    """Each returned kWh is worth 0.8 of a consumed one, in costs and on the bill."""
    portal.prosumer = True
    returned = _returned_from_consumption(portal, 0.4)
    tariff = FakeG12Tariff()
    register_prices_entry(hass, make_prices_entry(tariff))
    entry = make_entry(net_metering="0_8")

    await _set_up(hass, entry)

    period = tariff.periods[0]
    for zone, name, display in (("day", "Dzień", "dzien"), ("night", "Noc", "noc")):
        series = database.series(f"enea:{FAKE_PPE}_koszt_energii_oddana_{display}")
        assert series[-1]["sum"] == pytest.approx(
            returned[name] * _kwh_price(period.zones[zone], 0.0) * 0.8, abs=1e-6
        )

    registry = er.async_get(hass)
    for key, value in (("bill_prev_reading", "2026-06-06"), ("bill_last_reading", "2026-08-05")):
        entity_id = registry.async_get_entity_id("date", "enea", f"enea-{FAKE_PPE}-{key}")
        await hass.services.async_call(
            "date", "set_value", {"entity_id": entity_id, "date": value}, blocking=True
        )
    await hass.async_block_till_done()
    bill = hass.states.get(
        registry.async_get_entity_id("sensor", "enea", f"enea-{FAKE_PPE}-bill_previous")
    ).attributes

    for zone in ("dzien", "noc"):
        assert bill[f"returned_kwh_{zone}"] == pytest.approx(0.4 * bill[f"kwh_{zone}"], abs=1e-2)
        assert bill[f"billed_kwh_{zone}"] == pytest.approx(
            bill[f"kwh_{zone}"] - 0.8 * bill[f"returned_kwh_{zone}"], abs=1e-2
        )
    assert bill["net_metering_left_kwh"] == 0
