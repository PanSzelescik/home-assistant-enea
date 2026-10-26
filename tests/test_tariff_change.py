"""A meter whose agreement moved it from G11 to G12 on 1 June 2026.

Each day is costed and billed with the prices of the tariff group of the
agreement in force that day.  A group enea_prices has no entry for leaves its
days without prices rather than costing them at another group's.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import pytest
from homeassistant.helpers import entity_registry as er

from conftest import load_portal_fixture
from custom_components.enea.costs import _kwh_price
from custom_components.enea.parsing import slot_start_dt
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_PPE,
    SAVED_ON,
    FakeG11Tariff,
    FakeG12Period,
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

CHANGE = date(2026, 6, 1)
YESTERDAY = SAVED_ON - timedelta(days=1)
COST = f"enea:{FAKE_PPE}_koszt_energii_pobrana"


@pytest.fixture(autouse=True)
def g11_until_june(portal):
    """Replace the meter's agreements: G11 until the end of May, G12 since."""
    change = int(at_local(CHANGE).timestamp() * 1000)
    old, new = portal.dashboard["agreements"]
    portal.dashboard["agreements"] = [
        {**new, "to": None, "from": change, "tariffGroupName": "G12"},
        {**old, "to": change, "tariffGroupName": "G11"},
    ]


def _kwh(first_day: date, last_day: date, zone_of) -> dict[str, float]:
    """Return the fixture's kWh by zone over whole days, from the installation hour on."""
    start = max(at_local(first_day), at_local(date(2026, 3, 20), 12))
    end = at_local(last_day + timedelta(days=1))
    totals: dict[str, float] = defaultdict(float)
    for entry in load_portal_fixture("range_2026-03-01_2026-10-10_1")["valuesToTable"]:
        hour = slot_start_dt(entry)
        if start <= hour < end:
            totals[zone_of(hour.hour)] += sum(item["value"] or 0.0 for item in entry["items"])
    return totals


async def _set_up(hass, *tariffs):
    """Configure enea_prices for the given groups, then add the meter."""
    for tariff in tariffs:
        register_prices_entry(hass, make_prices_entry(tariff))
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry


async def test_each_day_is_costed_by_its_agreements_group(hass, database):
    """May at G11 prices in its single zone, June on at G12 day and night prices."""
    g11, g12 = FakeG11Tariff(), FakeG12Tariff()
    entry = await _set_up(hass, g11, g12)
    g12_zone = FakeG12Period(CHANGE, YESTERDAY).get_zone_at_hour
    before = _kwh(date(2026, 3, 20), CHANGE - timedelta(days=1), lambda hour: "day")
    after = _kwh(CHANGE, YESTERDAY, g12_zone)

    day = database.series(f"{COST}_dzien")
    night = database.series(f"{COST}_noc")

    expected_day = (
        before["day"] * _kwh_price(g11.periods[0].zones["day"], 0.0)
        + after["day"] * _kwh_price(g12.periods[0].zones["day"], 0.0)
    )
    assert day[-1]["sum"] == pytest.approx(expected_day, abs=1e-6)
    assert night[0]["start"] >= at_local(CHANGE).timestamp()
    assert night[-1]["sum"] == pytest.approx(
        after["night"] * _kwh_price(g12.periods[0].zones["night"], 0.0), abs=1e-6
    )
    groups = entry.runtime_data.coordinator.diagnostics_state()["tariff_groups"]
    assert [(g["group"], g["priced"]) for g in groups] == [("G11", True), ("G12", True)]


async def test_days_of_a_group_without_prices_stay_uncosted(hass, database):
    """Without a G11 entry the G11 days get no cost at all, not G12's."""
    entry = await _set_up(hass, FakeG12Tariff())

    first = min(row["start"] for zone in ("dzien", "noc") for row in database.series(f"{COST}_{zone}"))
    assert first == at_local(CHANGE).timestamp()
    groups = entry.runtime_data.coordinator.diagnostics_state()["tariff_groups"]
    assert [(g["group"], g["priced"]) for g in groups] == [("G11", False), ("G12", True)]


async def test_a_bill_across_the_change_leaves_out_the_unpriced_days(hass):
    """The May part of a period has no G11 prices; its kWh are reported, not charged."""
    await _set_up(hass, FakeG12Tariff())
    registry = er.async_get(hass)
    for key, value in (("bill_prev_reading", "2026-05-15"), ("bill_last_reading", "2026-06-15")):
        entity_id = registry.async_get_entity_id("date", "enea", f"enea-{FAKE_PPE}-{key}")
        await hass.services.async_call(
            "date", "set_value", {"entity_id": entity_id, "date": value}, blocking=True
        )
    await hass.async_block_till_done()

    bill = hass.states.get(registry.async_get_entity_id("sensor", "enea", f"enea-{FAKE_PPE}-bill_previous"))

    may = _kwh(date(2026, 5, 16), CHANGE - timedelta(days=1), lambda hour: "day")
    june = _kwh(CHANGE, date(2026, 6, 15), FakeG12Period(CHANGE, YESTERDAY).get_zone_at_hour)
    assert bill.attributes["unpriced_kwh"] == pytest.approx(may["day"], abs=1e-3)
    assert bill.attributes["kwh_dzien"] == pytest.approx(june["day"], abs=1e-3)
    assert bill.attributes["kwh_noc"] == pytest.approx(june["night"], abs=1e-3)
