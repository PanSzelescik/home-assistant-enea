"""The contract with the real enea_prices integration, checked against its code.

The two integrations never import each other: each reads the other duck-typed,
by attribute names, entry data keys and module names.  Elsewhere the tests
stand enea_prices in with fakes written after it, which would stay green if
enea_prices renamed something; here its own code is loaded, from a checkout
of home-assistant-enea-prices (ENEA_PRICES_PATH, or next to this repository).

What enea reads from enea_prices: entries keyed by data["tariff"], their
runtime_data (tariff, phases, annual_kwh, billing_months), TARIFFS, AKCYZA from
the const module, and of the tariff groups their periods, zones and fees.
What enea_prices reads from enea: each meter coordinator's tariff name and
detected_installation, and it reloads the meters of its group after setup.
"""
from __future__ import annotations

import importlib
import sys
from collections import defaultdict
from datetime import date, timedelta
from numbers import Real
from pathlib import Path
from types import MappingProxyType

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from conftest import load_portal_fixture
from custom_components.enea import const as enea_const
from custom_components.enea.billing import find_prices_config
from custom_components.enea.costs import TariffHistory, _akcyza, _kwh_price, find_tariff_group
from custom_components.enea.parsing import slot_start_dt
from ha_instance import (  # noqa: F401 - the imported fixtures are used by name
    FAKE_PPE,
    SAVED_ON,
    at_local,
    clock,
    database,
    find_enea_prices_repository,
    hass,
    make_entry,
    portal,
    real_enea_prices,
)

pytestmark = pytest.mark.usefixtures("real_enea_prices")

SETTINGS = {"tariff": "G12", "phases": 3, "annual_kwh": 2000, "billing_months": 2}


def _module(name: str):
    """Return a module of the real enea_prices, loaded by the fixture."""
    return importlib.import_module(f"custom_components.enea_prices.{name}")


def _days(period) -> list[date]:
    """Return days spread over a price period: its ends, a weekend and a holiday."""
    first, last = period.valid_from, period.valid_until
    days = {first, last, first + (last - first) / 2}
    saturday = first + timedelta(days=(5 - first.weekday()) % 7)
    days |= {saturday, saturday + timedelta(days=1)}
    days |= {date(year, 11, 11) for year in range(first.year, last.year + 1)}
    return sorted(day for day in days if first <= day <= last)


# --- what enea reads from enea_prices -----------------------------------------


def test_the_real_integration_is_loaded(real_enea_prices):
    """The package comes from the checkout, not from the stand-in in tests/fake_integrations."""
    repository = find_enea_prices_repository()
    assert repository is not None
    assert Path(real_enea_prices.__file__).resolve().is_relative_to(repository.resolve())


def test_entry_keys_and_domains_agree():
    """The data keys enea writes a repair fix into are the ones enea_prices reads."""
    prices = _module("const")

    assert enea_const.ENEA_PRICES_DOMAIN == prices.DOMAIN
    assert prices.ENEA_DOMAIN == enea_const.DOMAIN
    assert (
        enea_const.ENEA_PRICES_CONF_TARIFF,
        enea_const.ENEA_PRICES_CONF_PHASES,
        enea_const.ENEA_PRICES_CONF_ANNUAL_KWH,
        enea_const.ENEA_PRICES_CONF_BILLING_MONTHS,
    ) == (prices.CONF_TARIFF, prices.CONF_PHASES, prices.CONF_ANNUAL_KWH, prices.CONF_BILLING_MONTHS)


def test_excise_duty_is_loaded_with_the_package():
    """AKCYZA is in the const module enea looks it up in, once the package is loaded."""
    assert "custom_components.enea_prices.const" in sys.modules
    assert _akcyza() == _module("tariffs").AKCYZA > 0


def test_portal_aliases_name_tariff_groups():
    """Every group name the portal shortens maps to a group enea_prices prices."""
    tariffs = _module("tariffs").TARIFFS
    assert set(enea_const.TARIFF_GROUP_ALIASES.values()) <= set(tariffs)


@pytest.mark.parametrize("group_name", ["G11", "G12", "G12w", "G12sezON", "G13active"])
def test_tariff_group_shape(group_name):
    """Each group has what costs, bills and diagnostics read, in every period."""
    group = _module("tariffs").TARIFFS[group_name]

    assert group.name == group_name
    assert group.periods
    for period in group.periods:
        assert isinstance(period.valid_from, date) and isinstance(period.valid_until, date)
        assert group.get_period_for_date(period.valid_from) is period
        for zone, pricing in period.zones.items():
            assert str(zone) in enea_const.COST_ZONE_DISPLAY, zone
            for name in ("energy", "variable_network", "quality", "oze", "cogeneration", "total_distribution"):
                assert isinstance(getattr(pricing, name), Real), (zone, name)
        monthly = period.monthly
        for phases in (1, 3):
            assert isinstance(monthly.get_network_fixed(phases), Real)
        for annual_kwh in (250, 850, 2000, 5000):
            assert isinstance(monthly.get_capacity(annual_kwh), Real)
        for months in enea_const.BILLING_PERIOD_MONTHS:
            assert isinstance(monthly.get_subscription(months), Real)
        assert isinstance(getattr(monthly, "trade", 0.0), Real)


@pytest.mark.parametrize("group_name", ["G11", "G12", "G12w", "G12sezON", "G13active"])
def test_every_hour_falls_in_a_priced_zone(group_name):
    """An hour the schedule puts outside the period's zones would silently cost nothing."""
    group = _module("tariffs").TARIFFS[group_name]

    for period in group.periods:
        for day in _days(period):
            for hour in range(24):
                assert period.get_zone_at_hour(hour, day=day) in period.zones, (period.valid_from, day, hour)


@pytest.mark.parametrize("group_name", ["G11", "G12", "G12w", "G12sezON", "G13active"])
def test_brutto_price_agrees_with_the_costs(group_name):
    """enea_prices' brutto kWh price and the one costs charge round to the same value."""
    tariffs = _module("tariffs")

    for period in tariffs.TARIFFS[group_name].periods:
        for zone, pricing in period.zones.items():
            assert round(pricing.total_brutto, 4) == _kwh_price(pricing, tariffs.AKCYZA), zone


def test_tariff_history_wraps_real_groups():
    """Costs combine groups of several agreements; real groups fit TariffHistory."""
    tariffs = _module("tariffs").TARIFFS
    g11, g12 = tariffs["G11"], tariffs["G12"]
    change = g12.periods[-1].valid_from

    history = TariffHistory(g12, [(date(2000, 1, 1), change, g11, "G11"), (change, None, g12, "G12")])

    day_before = change - timedelta(days=1)
    assert history.get_period_for_date(day_before) is g11.get_period_for_date(day_before)
    assert history.get_period_for_date(change) is g12.get_period_for_date(change)
    assert {id(p) for p in g11.periods} <= {id(p) for p in history.periods}


def test_enea_g12_schedule_matches_the_portals_zones():
    """Each hour of the saved responses is in the zone enea_prices' G12 puts it in.

    The portal marks every hour's kWh under its zone; costs and bills pick the
    zone from enea_prices' schedule.  Disagreeing would cost hours at the
    other zone's price.
    """
    g12 = _module("tariffs").TARIFFS["G12"]
    response = load_portal_fixture("range_2026-03-01_2026-10-10_1")
    names = {zone["id"]: zone["name"] for zone in response["zones"]}
    checked = 0
    for entry in response["valuesToTable"]:
        hour = slot_start_dt(entry)
        period = g12.get_period_for_date(hour.date())
        if period is None:
            continue
        for item in entry["items"]:
            if item["value"] is not None:
                zone = period.get_zone_at_hour(hour.hour, day=hour.date())
                assert enea_const.COST_ZONE_DISPLAY[str(zone)] == names[item["tarifZoneId"]], hour
                checked += 1
    assert checked > 4000


# --- the two set up together ----------------------------------------------------


def _prices_entry(**settings) -> ConfigEntry:
    """Return an enea_prices config entry for G12 with the given settings changed."""
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


async def _add(hass, entry):
    """Add a config entry and wait for everything it starts."""
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry


async def test_enea_finds_the_real_entry_and_its_settings(hass):
    """A real enea_prices entry is what find_prices_config and find_tariff_group return."""
    prices = await _add(hass, _prices_entry())

    assert prices.state is ConfigEntryState.LOADED
    cfg = find_prices_config(hass, "g12")
    assert cfg is not None
    assert cfg.tariff is prices.runtime_data.tariff is find_tariff_group(hass, "G12")
    assert (cfg.phases, cfg.annual_kwh, cfg.billing_months) == (3, 2000, 2)
    assert cfg.akcyza == _module("tariffs").AKCYZA


async def test_setting_prices_up_reloads_the_meter_with_costs_and_bills(hass, database):
    """enea_prices added after the meter reloads it; the meter then costs and bills."""
    meter = await _add(hass, make_entry())
    old = meter.runtime_data.coordinator
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", "enea", f"enea-{FAKE_PPE}-bill_current") is None

    await _add(hass, _prices_entry())

    assert meter.runtime_data.coordinator is not old
    assert registry.async_get_entity_id("sensor", "enea", f"enea-{FAKE_PPE}-bill_current") is not None
    for zone in ("dzien", "noc"):
        assert database.series(f"enea:{FAKE_PPE}_koszt_energii_pobrana_{zone}")[-1]["sum"] > 0


async def test_costs_use_the_real_prices_of_each_day(hass, database):
    """Each zone's cost is its hours' kWh at the brutto price of the hour's own period."""
    prices = await _add(hass, _prices_entry())
    await _add(hass, make_entry())
    g12 = prices.runtime_data.tariff
    akcyza = _module("tariffs").AKCYZA

    expected: dict[str, float] = defaultdict(float)
    start, end = at_local(date(2026, 3, 20), 12), at_local(SAVED_ON)
    for entry in load_portal_fixture("range_2026-03-01_2026-10-10_1")["valuesToTable"]:
        hour = slot_start_dt(entry)
        period = g12.get_period_for_date(hour.date())
        if start <= hour < end and period is not None:
            zone = period.get_zone_at_hour(hour.hour, day=hour.date())
            kwh = sum(item["value"] or 0.0 for item in entry["items"])
            expected[str(zone)] += kwh * _kwh_price(period.zones[zone], akcyza)

    assert database.series(f"enea:{FAKE_PPE}_koszt_energii_pobrana_dzien")[-1]["sum"] == pytest.approx(expected["day"])
    assert database.series(f"enea:{FAKE_PPE}_koszt_energii_pobrana_noc")[-1]["sum"] == pytest.approx(expected["night"])


async def test_prices_reads_what_the_meter_detected(hass):
    """enea_prices' form hints come from the meter's coordinator."""
    await _add(hass, make_entry())

    [hint] = _module("meter").meter_hints(hass)

    assert (hint.tariff, hint.phases, hint.billing_months) == ("G12", 3, 2)


async def test_installed_prices_without_an_entry_are_offered(hass):
    """With the real TARIFFS knowing G12, a meter without its entry gets the repair."""
    meter = await _add(hass, make_entry())

    assert ir.async_get(hass).async_get_issue("enea", f"prices_not_configured_{meter.entry_id}") is not None


async def test_phases_fix_goes_through_the_real_reload(hass):
    """Fixing the phases saves them in the real entry, whose reload clears the issue."""
    from custom_components.enea.repairs import async_create_fix_flow

    prices = await _add(hass, _prices_entry(phases=1))
    meter = await _add(hass, make_entry())
    issue_id = f"phases_mismatch_{meter.entry_id}"
    issue = ir.async_get(hass).async_get_issue("enea", issue_id)
    assert issue is not None

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass, flow.handler, flow.issue_id = hass, "enea", issue_id
    await flow.async_step_init()
    await flow.async_step_confirm({})
    await hass.async_block_till_done()

    assert prices.runtime_data.phases == 3
    assert ir.async_get(hass).async_get_issue("enea", issue_id) is None
