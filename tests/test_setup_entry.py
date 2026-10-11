"""The fixture account set up as a config entry in a running Home Assistant.

These go through everything a user's installation does — setup, platforms,
the first refresh with its background import, later refreshes, the actions,
diagnostics, unloading — with only the network (FakePortal), the recorder's
statistics (StatisticsDatabase) and the clock replaced.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigEntryState, SOURCE_REAUTH
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

import custom_components.enea as integration
from custom_components.enea import diagnostics
from custom_components.enea.connector import EneaApiError, EneaAuthError
from custom_components.enea.parsing import slot_start_dt
from conftest import load_portal_fixture
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_METER_ID,
    FAKE_PPE,
    NOW,
    SAVED_ON,
    FakePortal,
    async_start_hass,
    at_local,
    clock,
    database,
    hass,
    make_entry,
    portal,
)

ASSEMBLY_DAY = date(2026, 3, 20)
YESTERDAY = SAVED_ON - timedelta(days=1)
CONSUMED = f"enea:{FAKE_PPE}_energia_pobrana"


async def _set_up(hass, **options) -> Any:
    """Add the fixture meter's entry and wait for its setup and background import."""
    entry = make_entry(**options)
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry


async def _set_up_without_waiting(hass, **options) -> Any:
    """Add the meter's entry; return as soon as it is set up, its import still running."""
    entry = make_entry(**options)
    await hass.config_entries.async_add(entry)
    for _ in range(20):  # let the import reach its first, held, request
        await asyncio.sleep(0)
    return entry


def _ranges(portal: FakePortal) -> list[tuple[Any, ...]]:
    """Return the range requests made so far, without the meter id."""
    return [request[2:] for request in portal.requests if request[0] == "range"]


def _state(hass, key: str) -> str:
    """Return the state of the meter's entity with the given unique key."""
    registry = er.async_get(hass)
    for domain in ("sensor", "binary_sensor", "date"):
        entity_id = registry.async_get_entity_id(domain, "enea", f"enea-{FAKE_PPE}-{key}")
        if entity_id:
            state = hass.states.get(entity_id)
            assert state is not None, entity_id
            return state.state
    raise AssertionError(f"no entity for {key}")


def _fixture_hours(first: datetime, last: datetime) -> list[tuple[datetime, float]]:
    """Return the hourly total kWh of the long-range fixture within [first, last]."""
    response = load_portal_fixture("range_2026-03-01_2026-10-10_1")
    hours = []
    for entry in response["valuesToTable"]:
        start = slot_start_dt(entry)
        if first <= start <= last:
            hours.append((start, sum(item["value"] or 0.0 for item in entry["items"])))
    return hours


async def test_setup_creates_the_meter_and_its_entities(hass, portal):
    """The entry loads, the device is named after the PPE and the sensors read the dashboard."""
    entry = await _set_up(hass)

    assert entry.state is ConfigEntryState.LOADED
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [device.identifiers for device in devices] == [{("enea", FAKE_PPE)}]
    dashboard = load_portal_fixture("dashboard")
    consumed = next(cv for cv in dashboard["currentValues"] if cv["measurementId"] == 1)

    assert _state(hass, "tariff") == "G12"
    assert _state(hass, "capacity") == "14"
    assert _state(hass, "meter_model") == "OTUS3"
    assert _state(hass, "meter_assembly") == "2026-03-20T11:12:44+00:00"
    assert _state(hass, "phases") == "three_phase"
    assert _state(hass, "billing_period_start") == "2026-08-06"
    assert _state(hass, "transmission") == "on"
    assert float(_state(hass, "consumption_total")) == consumed["valueNoZones"]["value"]
    assert float(_state(hass, "consumption_zone1")) == consumed["valueZone1"]["value"]
    assert float(_state(hass, "consumption_zone2")) == consumed["valueZone2"]["value"]

    registry = er.async_get(hass)
    for key in ("han_wmbus", "han_p1", "switch_state"):
        entity_id = registry.async_get_entity_id("sensor", "enea", f"enea-{FAKE_PPE}-{key}")
        assert entity_id is not None and registry.async_get(entity_id).disabled, key
        assert hass.states.get(entity_id) is None


async def test_first_setup_imports_the_history_from_the_assembly(hass, portal, database):
    """The background import starts at the assembly and runs to yesterday in 180-day chunks.

    Every hour the portal published since the installation hour is stored,
    and the running total is the plain sum of them.
    """
    await _set_up(hass)

    chunk_end = ASSEMBLY_DAY + timedelta(days=179)
    assert _ranges(portal) == [
        (ASSEMBLY_DAY, chunk_end, 1, None),
        (ASSEMBLY_DAY, chunk_end, 5, None),
        (chunk_end + timedelta(days=1), YESTERDAY, 1, None),
        (chunk_end + timedelta(days=1), YESTERDAY, 5, None),
    ]
    expected = _fixture_hours(at_local(ASSEMBLY_DAY, 12), at_local(YESTERDAY, 23))
    stored = database.series(CONSUMED)
    assert [row["start"] for row in stored] == [hour.timestamp() for hour, _ in expected]
    assert stored[-1]["sum"] == pytest.approx(sum(kwh for _, kwh in expected), abs=1e-6)
    assert {f"{CONSUMED}_dzien", f"{CONSUMED}_noc"} <= set(database.rows)


async def test_statistics_date_follows_the_import(hass, portal, clock):
    """The background import lands after the first refresh; the next one reports it."""
    entry = await _set_up(hass)
    coordinator = entry.runtime_data.coordinator
    assert _state(hass, "statistics_until") == "unknown"
    portal.requests.clear()

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, "statistics_until") == YESTERDAY.isoformat()
    assert _ranges(portal) == []  # everything up to yesterday is stored already


async def test_unpublished_day_waits_then_is_stored_as_zero(hass, portal, database, clock):
    """The day the responses were saved on has only nulls.

    The next day's refresh asks for it and stores nothing; once the grace
    period is over it is stored as zero consumption so later days count on
    from the right total.
    """
    entry = await _set_up(hass)
    coordinator = entry.runtime_data.coordinator
    total_before = database.series(CONSUMED)[-1]["sum"]

    clock.now = NOW + timedelta(days=1)
    portal.requests.clear()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _ranges(portal) == [(SAVED_ON, SAVED_ON, 1, None), (SAVED_ON, SAVED_ON, 5, None)]
    assert coordinator.statistics_until == YESTERDAY
    assert coordinator.diagnostics_state()["zero_filled_days"] == []

    clock.now = NOW + timedelta(days=3)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.statistics_until == SAVED_ON
    assert coordinator.diagnostics_state()["zero_filled_days"] == [SAVED_ON.isoformat()]
    stored = database.series(CONSUMED)
    assert stored[-1]["start"] == at_local(SAVED_ON, 23).timestamp()
    assert stored[-1]["sum"] == pytest.approx(total_before)


async def test_backfill_action_reimports_the_requested_days(hass, portal, database):
    """enea.backfill fetches exactly the range asked for and leaves the totals as they were."""
    await _set_up(hass)
    before = database.series(CONSUMED)
    portal.requests.clear()

    await hass.services.async_call(
        "enea", "backfill", {"start_date": "2026-03-27", "end_date": "2026-04-02"}, blocking=True
    )

    assert _ranges(portal) == [
        (date(2026, 3, 27), date(2026, 4, 2), 1, None),
        (date(2026, 3, 27), date(2026, 4, 2), 5, None),
    ]
    assert database.series(CONSUMED) == before


@pytest.mark.parametrize("data,first", [
    pytest.param({"days_back": 7}, YESTERDAY - timedelta(days=6), id="days-back"),
    pytest.param({}, YESTERDAY - timedelta(days=29), id="default-30-days"),
])
async def test_backfill_action_defaults(hass, portal, data, first):
    """Without dates the action counts back from yesterday."""
    await _set_up(hass)
    portal.requests.clear()

    await hass.services.async_call("enea", "backfill", data, blocking=True)

    assert _ranges(portal)[0] == (first, YESTERDAY, 1, None)


async def test_actions_only_reach_the_chosen_device(hass, portal):
    """A device_id of another device leaves this meter alone; its own id reaches it."""
    entry = await _set_up(hass)
    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    portal.requests.clear()

    await hass.services.async_call("enea", "refresh", {"device_id": ["other"]}, blocking=True)
    await hass.async_block_till_done()
    assert portal.requests == []

    await hass.services.async_call("enea", "refresh", {"device_id": device.id}, blocking=True)
    await hass.async_block_till_done()
    assert ("dashboard", FAKE_METER_ID) in portal.requests


async def test_unload_removes_the_actions_and_the_shared_client(hass):
    """The last unloaded entry takes the actions and the account's client with it."""
    entry = await _set_up(hass)
    assert hass.services.has_service("enea", "refresh")

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service("enea", "refresh")
    assert not hass.services.has_service("enea", "backfill")
    assert hass.data["enea"] == {}


async def test_rejected_login_asks_for_new_credentials(hass, portal):
    """An authentication error during setup starts the reauthentication flow."""
    portal.fail_with = EneaAuthError("rejected")

    entry = await _set_up(hass)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler("enea")
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_unreachable_portal_retries_the_setup(hass, portal):
    """Any other portal error leaves the entry to be retried."""
    portal.fail_with = EneaApiError("offline")

    entry = await _set_up(hass)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_statistics_wait_for_home_assistant_to_start(tmp_path, portal, database):
    """During startup the sensors come up at once and the statistics follow the start."""
    hass = await async_start_hass(str(tmp_path))
    hass.set_state(CoreState.starting)
    try:
        entry = await _set_up(hass)
        assert entry.state is ConfigEntryState.LOADED
        assert _state(hass, "tariff") == "G12"
        assert _ranges(portal) == []

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

        assert _ranges(portal)[0] == (ASSEMBLY_DAY, ASSEMBLY_DAY + timedelta(days=179), 1, None)
        assert database.series(CONSUMED)
    finally:
        await hass.async_stop(force=True)


async def test_diagnostics_report_of_a_loaded_entry(hass):
    """The report covers coordinator, entities and statistics without the PPE number."""
    entry = await _set_up(hass)

    report = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    text = repr(report)
    assert FAKE_PPE not in text
    assert str(FAKE_METER_ID) not in text
    assert report["coordinator"]["assembly_datetime"].startswith("2026-03-20T12:12:44")
    assert report["coordinator"]["statistics_until"] == YESTERDAY.isoformat()
    assert report["installation"]["detected"]["phases"] == 3
    assert report["installation"]["detected"]["billing_months"] == 2
    overview = report["statistics"]
    assert overview["enea:…1234_energia_pobrana"]["last_hour"].startswith(f"{YESTERDAY}T23:00")
    assert report["entities"]["tariff"]["state"] == "G12"
    assert report["entities"]["address"]["state"] == "**REDACTED**"


async def test_meters_of_one_account_share_the_client(hass, portal, monkeypatch):
    """A second meter reuses the account's client; the actions stay until the last unload."""
    factory = Mock(return_value=portal)
    monkeypatch.setattr(integration, "EneaApiClient", factory)
    first = await _set_up(hass)
    second = make_entry(meter_name="590310600000005678")
    await hass.config_entries.async_add(second)
    await hass.async_block_till_done()

    assert second.state is ConfigEntryState.LOADED
    assert factory.call_count == 1
    assert first.runtime_data.coordinator.client is second.runtime_data.coordinator.client

    assert await hass.config_entries.async_unload(first.entry_id)
    assert hass.services.has_service("enea", "refresh")
    assert "test@example.invalid" in hass.data["enea"]

    assert await hass.config_entries.async_unload(second.entry_id)
    assert not hass.services.has_service("enea", "refresh")
    assert hass.data["enea"] == {}


async def test_closed_session_gets_a_new_client(hass, portal, monkeypatch):
    """After the account's session was closed the next setup builds a new client."""
    factory = Mock(return_value=portal)
    monkeypatch.setattr(integration, "EneaApiClient", factory)
    entry = await _set_up(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    stale = SimpleNamespace(
        client=SimpleNamespace(session_closed=True), notifications=Mock()
    )
    hass.data["enea"]["test@example.invalid"] = stale

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert factory.call_count == 2
    assert hass.data["enea"]["test@example.invalid"].client is portal
    stale.notifications.unsubscribe.assert_called_once_with()


async def test_failed_import_keeps_the_sensors_and_is_reported(hass, portal):
    """A portal refusing the history leaves the dashboard working and says why."""
    portal.fail_ranges_with = EneaApiError("Unexpected response from range endpoint: 503")

    entry = await _set_up(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert _state(hass, "tariff") == "G12"
    backfill = entry.runtime_data.coordinator.diagnostics_state()["initial_backfill"]
    assert backfill == "failed: Unexpected response from range endpoint: 503"


async def test_failed_statistics_step_keeps_the_refresh(hass, portal, clock):
    """A refresh whose statistics fail still updates the dashboard and records the error."""
    entry = await _set_up(hass)
    coordinator = entry.runtime_data.coordinator
    clock.now = NOW + timedelta(days=1)
    portal.fail_ranges_with = EneaApiError("offline")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert coordinator.diagnostics_state()["statistics_error"] == "EneaApiError: offline"
    assert _state(hass, "tariff") == "G12"


async def test_refresh_during_the_import_leaves_the_days_to_it(hass, portal):
    """While the background import runs, a refresh fetches no days of its own."""
    portal.gate = asyncio.Event()
    entry = await _set_up_without_waiting(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.diagnostics_state()["initial_backfill"] == "running"
    requests = len(_ranges(portal))

    await coordinator.async_refresh()

    assert len(_ranges(portal)) == requests
    portal.gate.set()
    await hass.async_block_till_done()
    assert coordinator.diagnostics_state()["initial_backfill"] == "done"


async def test_unload_cancels_the_import(hass, portal):
    """Unloading the entry while the history is being imported stops the import."""
    portal.gate = asyncio.Event()
    entry = await _set_up_without_waiting(hass)
    coordinator = entry.runtime_data.coordinator

    task = coordinator._backfill_task
    assert task is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert coordinator.diagnostics_state()["initial_backfill"] == "cancelled"


async def test_power_only_meter_stores_power_and_no_costs(hass, database):
    """With only power enabled the hours are stored as means and no energy is fetched."""
    entry = await _set_up(
        hass, fetch_consumption=False, fetch_generation=False, fetch_power_consumption=True
    )

    power = database.series(f"enea:{FAKE_PPE}_moc_pobrana")
    assert len(power) == 7 * 24 - 1  # the one week the fixtures have power for
    assert all("mean" in row and "sum" not in row for row in power)
    assert not database.series(CONSUMED)
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    assert coordinator.statistics_until == date(2026, 4, 2)


async def test_meter_with_nothing_to_fetch_asks_for_no_history(hass, portal):
    """With every measurement turned off only the dashboard is read."""
    await _set_up(hass, fetch_consumption=False, fetch_generation=False)

    assert _ranges(portal) == []
    assert _state(hass, "statistics_until") == "unknown"


@pytest.mark.parametrize("language,template", [("pl", "new_meter_model_pl.yml"), ("en", "new_meter_model_en.yml")])
async def test_unknown_meter_model_is_reported_without_the_ppe(hass, portal, language, template):
    """An unlisted model raises a repair linking to the form, filled with model and capacity only.

    Once the model is known — the portal reports a listed one — the issue goes.
    """
    hass.config.language = language
    active = next(m for m in portal.dashboard["meters"] if m["disassemblyDate"] is None)
    active["typeName"] = "ZXF100"

    entry = await _set_up(hass)

    issue_id = f"unknown_meter_model_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue("enea", issue_id)
    assert issue is not None and not issue.is_fixable
    query = parse_qs(urlsplit(issue.learn_more_url).query)
    assert query["template"] == [template]
    assert (query["model"], query["capacity"]) == (["ZXF100"], ["14"])
    assert FAKE_PPE not in issue.learn_more_url
    assert (_state(hass, "phases"), hass.states.get(
        er.async_get(hass).async_get_entity_id("sensor", "enea", f"enea-{FAKE_PPE}-phases")
    ).attributes["source"]) == ("three_phase", "contractual_capacity")

    active["typeName"] = "OTUS3"
    await entry.runtime_data.coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue("enea", issue_id) is None
