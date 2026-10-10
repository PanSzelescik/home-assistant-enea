"""The fixture account with enea_prices configured for its tariff group.

Costs land in the statistics, the bill entities appear and follow the
reading dates, a price change re-prices the history, and the installation the
meter shows is compared with the enea_prices settings.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import pytest
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.enea.costs import _kwh_price
from custom_components.enea.parsing import slot_start_dt
from conftest import load_portal_fixture
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_PPE,
    SAVED_ON,
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

YESTERDAY = SAVED_ON - timedelta(days=1)
COST = f"enea:{FAKE_PPE}_koszt_energii_pobrana"


async def _set_up(hass, tariff: FakeG12Tariff | None = None, **runtime: Any) -> Any:
    """Configure enea_prices for G12, then add the meter and wait for its import."""
    prices = make_prices_entry(tariff)
    for name, value in runtime.items():
        setattr(prices.runtime_data, name, value)
    register_prices_entry(hass, prices)
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass, domain: str, key: str) -> str:
    """Return the entity id of the meter's entity with the given key."""
    entity_id = er.async_get(hass).async_get_entity_id(domain, "enea", f"enea-{FAKE_PPE}-{key}")
    assert entity_id is not None, key
    return entity_id


def _kwh_by_zone(first_day: date, last_day: date, first_hour: int = 0) -> dict[str, float]:
    """Return the fixture's kWh per G12 zone over whole days, from first_hour on the first."""
    zone_of = FakeG12Period(date.min, date.max).get_zone_at_hour
    totals: dict[str, float] = defaultdict(float)
    start, end = at_local(first_day, first_hour), at_local(last_day + timedelta(days=1))
    for entry in load_portal_fixture("range_2026-03-01_2026-10-10_1")["valuesToTable"]:
        hour = slot_start_dt(entry)
        if start <= hour < end:
            totals[zone_of(hour.hour)] += sum(item["value"] or 0.0 for item in entry["items"])
    return totals


async def test_history_is_costed_per_zone(hass, database):
    """Each zone's cost series totals its kWh at its brutto price.

    The meter has no generation, so no cost series of energy returned is
    started from its zeroes.
    """
    tariff = FakeG12Tariff()
    await _set_up(hass, tariff)

    kwh = _kwh_by_zone(date(2026, 3, 20), YESTERDAY, first_hour=12)
    period = tariff.periods[0]
    for zone, display in (("day", "dzien"), ("night", "noc")):
        series = database.series(f"{COST}_{display}")
        assert series[-1]["sum"] == pytest.approx(
            kwh[zone] * _kwh_price(period.zones[zone], 0.0), abs=1e-6
        )
        assert series[-1]["state"] == series[-1]["sum"]
    assert not [sid for sid in database.rows if "koszt_energii_oddana" in sid]


async def test_days_before_the_price_table_get_no_cost(hass, database):
    """A table starting in April leaves the March days without cost, and says so."""
    await _set_up(hass, FakeG12Tariff(valid_from=date(2026, 4, 1)))

    first = min(row["start"] for zone in ("dzien", "noc") for row in database.series(f"{COST}_{zone}"))
    assert first == at_local(date(2026, 4, 1)).timestamp()


async def test_new_prices_reprice_the_stored_history(hass, database):
    """Changing a zone's price for days already costed rewrites their costs."""
    tariff = FakeG12Tariff()
    entry = await _set_up(hass, tariff)
    day_zone = tariff.periods[0].zones["day"]
    day_zone.energy *= 2

    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    kwh = _kwh_by_zone(date(2026, 3, 20), YESTERDAY, first_hour=12)
    assert database.series(f"{COST}_dzien")[-1]["sum"] == pytest.approx(
        kwh["day"] * _kwh_price(day_zone, 0.0), abs=1e-6
    )
    assert entry.runtime_data.coordinator.diagnostics_state()["costs_repriced_from"] == "2026-03-20"


async def test_reading_dates_drive_the_bill_estimates(hass):
    """Setting both reading dates estimates the previous and the current period.

    The kWh of each zone are the hours of the period, priced by the schedule.
    """
    await _set_up(hass)
    bill_previous = _entity_id(hass, "sensor", "bill_previous")
    bill_current = _entity_id(hass, "sensor", "bill_current")
    assert hass.states.get(bill_previous).state == "unknown"

    for key, value in (("bill_prev_reading", "2026-06-06"), ("bill_last_reading", "2026-08-05")):
        await hass.services.async_call(
            "date", "set_value", {"entity_id": _entity_id(hass, "date", key), "date": value},
            blocking=True,
        )
    await hass.async_block_till_done()

    previous = hass.states.get(bill_previous)
    current = hass.states.get(bill_current)
    assert float(previous.state) > 0 and float(current.state) > 0
    assert previous.attributes["start"] == "2026-06-06"
    assert previous.attributes["end"] == "2026-08-05"
    assert previous.attributes["months"] == 2
    assert current.attributes["end"] == YESTERDAY.isoformat()
    kwh = _kwh_by_zone(date(2026, 6, 7), date(2026, 8, 5))
    assert previous.attributes["kwh_dzien"] == pytest.approx(kwh["day"], abs=1e-3)
    assert previous.attributes["kwh_noc"] == pytest.approx(kwh["night"], abs=1e-3)


async def test_settings_the_meter_contradicts_are_raised_as_repairs(hass):
    """Single phase in enea_prices on a three-phase OTUS3 is a repair; the rest agrees."""
    entry = await _set_up(hass, phases=1)

    issues = {
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == "enea"
    }
    assert issues == {f"phases_mismatch_{entry.entry_id}"}


async def test_diagnostics_show_the_price_table_reach(hass):
    """The report says how far the enea_prices table prices days."""
    from custom_components.enea import diagnostics

    entry = await _set_up(hass)

    report = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert report["enea_prices"]["covered_until"] == "2026-12-31"
    assert report["installation"]["enea_prices"]["phases"] == 3
