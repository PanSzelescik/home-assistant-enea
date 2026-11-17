"""New credentials reach the whole account, and only an account that has the meter.

The meters of one Portal Odbiorcy Enea account share one client.  An entry
left with the old password hands it back to that client the next time it is
set up, so the reauthentication of one meter updates them all.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntryDisabler,
    ConfigEntryState,
)

from custom_components.enea import config_flow
from custom_components.enea.connector import EneaAuthError
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_METER_ID,
    FAKE_PPE,
    clock,
    database,
    hass,
    make_entry,
    portal,
)

SECOND_PPE = "590310600000005678"
SECOND_METER_ID = 54321
NEW = {"username": "test@example.invalid", "password": "new-password"}


@pytest.fixture
def flow_portal(portal, monkeypatch):
    """Point the config flow's own client at the fake portal, which has two meters."""
    monkeypatch.setattr(config_flow, "EneaApiClient", lambda *args: portal)
    monkeypatch.setattr(
        config_flow, "async_create_clientsession", lambda hass: SimpleNamespace(close=AsyncMock())
    )
    portal.ppes.append({**portal.ppes[0], "id": SECOND_METER_ID, "code": SECOND_PPE})
    return portal


async def _two_meters_asking_for_credentials(hass, portal):
    """Set up two meters of one account whose login is rejected; return both entries."""
    portal.fail_with = EneaAuthError("rejected")
    first = make_entry()
    second = make_entry(meter_name=SECOND_PPE, meter_id=SECOND_METER_ID)
    for entry in (first, second):
        await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    assert len(hass.config_entries.flow.async_progress_by_handler("enea")) == 2
    portal.fail_with = None
    return first, second


def _reauth_flows(hass, entry) -> list[str]:
    """Return the ids of the reauthentication flows of an entry."""
    return [
        flow["flow_id"]
        for flow in hass.config_entries.flow.async_progress_by_handler(
            "enea", match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}
        )
    ]


async def test_new_password_reaches_every_meter_of_the_account(hass, flow_portal):
    """One reauthentication updates and reloads all the meters of the account."""
    first, second = await _two_meters_asking_for_credentials(hass, flow_portal)

    result = await hass.config_entries.flow.async_configure(_reauth_flows(hass, first)[0], NEW)
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert first.data["password"] == second.data["password"] == "new-password"
    assert first.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED
    # The other meter no longer asks for what it has just been given.
    assert hass.config_entries.flow.async_progress_by_handler("enea") == []


async def test_no_entry_hands_the_old_password_back(hass, flow_portal):
    """After the reauthentication every setup gives the shared client the new password."""
    first, second = await _two_meters_asking_for_credentials(hass, flow_portal)
    await hass.config_entries.flow.async_configure(_reauth_flows(hass, first)[0], NEW)
    await hass.async_block_till_done()
    flow_portal.passwords.clear()

    for entry in (second, first):
        await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert flow_portal.passwords
    assert set(flow_portal.passwords) == {"new-password"}


async def test_reauthentication_keeps_the_data_of_each_meter(hass, flow_portal):
    """Updating the other entry of the account changes its credentials and nothing else."""
    first, second = await _two_meters_asking_for_credentials(hass, flow_portal)

    await hass.config_entries.flow.async_configure(_reauth_flows(hass, second)[0], NEW)
    await hass.async_block_till_done()

    assert (first.data["meter_name"], first.data["meter_id"]) == (FAKE_PPE, FAKE_METER_ID)
    assert (second.data["meter_name"], second.data["meter_id"]) == (SECOND_PPE, SECOND_METER_ID)
    assert first.data["tariff"] == second.data["tariff"] == "G12"


async def test_meters_of_another_account_keep_their_credentials(hass, flow_portal):
    """Only entries logging in with the same account are given the new credentials."""
    entry = make_entry()
    other = make_entry(meter_name=SECOND_PPE, meter_id=SECOND_METER_ID)
    object.__setattr__(
        other, "data", {**other.data, "username": "other@example.invalid", "password": "theirs"}
    )
    flow_portal.fail_with = EneaAuthError("rejected")
    for added in (entry, other):
        await hass.config_entries.async_add(added)
    await hass.async_block_till_done()
    flow_portal.fail_with = None

    await hass.config_entries.flow.async_configure(_reauth_flows(hass, entry)[0], NEW)
    await hass.async_block_till_done()

    assert entry.data["password"] == "new-password"
    assert (other.data["username"], other.data["password"]) == ("other@example.invalid", "theirs")
    assert len(_reauth_flows(hass, other)) == 1


async def test_account_without_the_meter_is_refused(hass, flow_portal):
    """Credentials of an account that does not list the meter are not saved.

    They log in fine; every request for the meter would then be refused.
    """
    flow_portal.fail_with = EneaAuthError("rejected")
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    [flow_id] = _reauth_flows(hass, entry)
    flow_portal.fail_with = None
    flow_portal.ppes[:] = [{**flow_portal.ppes[0], "code": "590310600000009999"}]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"username": "someone@example.invalid", "password": "theirs"}
    )
    await hass.async_block_till_done()

    assert (result["type"], result["errors"]) == ("form", {"base": "meter_not_on_account"})
    assert entry.data["username"] == "test@example.invalid"
    assert entry.data["password"] == "test-password"
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_another_meter_missing_from_the_account_is_left_alone(hass, flow_portal):
    """The new account has only one of the two meters: only that entry moves to it."""
    first, second = await _two_meters_asking_for_credentials(hass, flow_portal)
    flow_portal.ppes[:] = [ppe for ppe in flow_portal.ppes if ppe["code"] == FAKE_PPE]

    result = await hass.config_entries.flow.async_configure(
        _reauth_flows(hass, first)[0],
        {"username": "moved@example.invalid", "password": "new-password"},
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert first.data["username"] == "moved@example.invalid"
    assert second.data["username"] == "test@example.invalid"
    assert second.data["password"] == "test-password"
    assert len(_reauth_flows(hass, second)) == 1


async def test_account_change_takes_the_id_the_meter_has_there(hass, flow_portal):
    """Reconfiguring to another account that has the meter stores its id on that account."""
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    flow_portal.ppes[0]["id"] = 77777

    result = await hass.config_entries.flow.async_init(
        "enea", context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "new@example.invalid", "password": "new-password"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data["username"] == "new@example.invalid"
    assert entry.data["meter_id"] == 77777
    assert entry.state is ConfigEntryState.LOADED
    assert list(hass.data["enea"]) == ["new@example.invalid"]
    assert ("dashboard", 77777) in flow_portal.requests


async def test_disabled_meter_gets_the_credentials_and_stays_disabled(hass, flow_portal):
    """A disabled entry of the account is updated too, without being loaded."""
    first, second = await _two_meters_asking_for_credentials(hass, flow_portal)
    await hass.config_entries.async_set_disabled_by(second.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()

    await hass.config_entries.flow.async_configure(_reauth_flows(hass, first)[0], NEW)
    await hass.async_block_till_done()

    assert second.data["password"] == "new-password"
    assert second.state is ConfigEntryState.NOT_LOADED
    assert first.state is ConfigEntryState.LOADED


async def test_login_changed_after_a_failed_setup_closes_the_old_account(hass, flow_portal):
    """An entry that never loaded under its old login still lets go of that account.

    Its setup made the account and failed, so nothing unloaded it; given
    another login it joins a new account and the old one must not stay open.
    """
    flow_portal.fail_with = EneaAuthError("rejected")
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    old_account = hass.data["enea"]["test@example.invalid"]
    assert old_account.entry_ids == {entry.entry_id}
    flow_portal.fail_with = None

    await hass.config_entries.flow.async_configure(
        _reauth_flows(hass, entry)[0],
        {"username": "moved@example.invalid", "password": "new-password"},
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert list(hass.data["enea"]) == ["moved@example.invalid"]
    assert old_account.entry_ids == set()
    assert hass.data["enea"]["moved@example.invalid"].entry_ids == {entry.entry_id}


async def test_account_lists_every_meter_using_it(hass, flow_portal):
    """Both meters are members of the one account; unloading one leaves the other in."""
    first = make_entry()
    second = make_entry(meter_name=SECOND_PPE, meter_id=SECOND_METER_ID)
    for entry in (first, second):
        await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    account = hass.data["enea"]["test@example.invalid"]
    assert account.entry_ids == {first.entry_id, second.entry_id}

    assert await hass.config_entries.async_unload(first.entry_id)

    assert account.entry_ids == {second.entry_id}
    assert hass.data["enea"]["test@example.invalid"] is account
