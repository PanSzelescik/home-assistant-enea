"""Configuration validates user input and preserves entries during credential changes."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.data_entry_flow import AbortFlow

from custom_components.enea import config_flow as module


@pytest.fixture
def flow_api(monkeypatch):
    """Use real flow steps with an isolated HTTP client and config registry."""
    session = SimpleNamespace(close=AsyncMock())
    client = SimpleNamespace(
        authenticate=AsyncMock(),
        get_meters=AsyncMock(return_value=[
            {"id": 12345, "code": "590310600000001234", "tariffGroup": {"name": "G12"}},
        ]),
        get_ppe_dashboard=AsyncMock(return_value={}),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "async_create_clientsession", Mock(return_value=session))
    monkeypatch.setattr(module, "EneaApiClient", factory)
    flow = module.EneaConfigFlow()
    flow.handler = "enea"
    flow.flow_id = "test-flow"
    flow.context = {"source": "user"}
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_entry_for_domain_unique_id=Mock(return_value=None),
        async_update_entry=Mock(),
        async_reload=AsyncMock(),
    ))
    monkeypatch.setattr(flow, "_async_in_progress", Mock(return_value=[]))
    return SimpleNamespace(flow=flow, client=client, session=session, factory=factory)


def _options(**updates):
    """Return a complete options submission with independently chosen defaults."""
    return {
        "update_interval": {"minutes": 30},
        "fetch_consumption": True,
        "fetch_generation": False,
        "fetch_power_consumption": False,
        "fetch_power_generation": False,
        **updates,
    }


async def test_single_meter_creates_entry_with_credentials_and_options(flow_api):
    """A single meter skips selection and saves identity separately from options."""
    flow = flow_api.flow
    result = await flow.async_step_user({"username": "test@example.invalid", "password": "test-password"})
    assert (result["type"], result["step_id"]) == ("form", "configure")
    flow_api.factory.assert_called_once_with(flow_api.session, "test@example.invalid", "test-password")
    flow_api.client.authenticate.assert_awaited_once_with()
    flow_api.client.get_ppe_dashboard.assert_awaited_once_with(12345)
    flow_api.session.close.assert_awaited_once_with()

    result = await flow.async_step_configure(_options())

    assert result["type"] == "create_entry"
    assert result["title"] == "Enea 590310600000001234"
    assert result["data"] == {
        "username": "test@example.invalid", "password": "test-password",
        "meter_id": 12345, "meter_name": "590310600000001234", "tariff": "G12",
    }
    assert result["options"] == _options()
    assert flow.unique_id == "590310600000001234"


@pytest.mark.parametrize("stage", ["authenticate", "get_meters"])
@pytest.mark.parametrize("error,expected", [
    pytest.param(module.EneaAuthError("rejected"), "invalid_auth", id="authentication"),
    pytest.param(module.EneaApiError("offline"), "cannot_connect", id="connection"),
    pytest.param(RuntimeError("unexpected"), "unknown", id="unexpected"),
])
async def test_login_errors_keep_form_and_close_session(flow_api, stage, error, expected):
    """Failures at either required API call stay recoverable and release HTTP resources."""
    getattr(flow_api.client, stage).side_effect = error

    result = await flow_api.flow.async_step_user({"username": "test@example.invalid", "password": "bad"})

    assert (result["type"], result["step_id"], result["errors"]) == ("form", "user", {"base": expected})
    flow_api.session.close.assert_awaited_once_with()
    flow_api.client.get_ppe_dashboard.assert_not_awaited()


async def test_multiple_meters_allow_selection_despite_missing_dashboard(flow_api):
    """An optional dashboard failure does not prevent choosing that meter."""
    flow_api.client.get_meters.return_value.append({"id": 67890, "code": "590310600000000001"})
    flow_api.client.get_ppe_dashboard.side_effect = [{}, module.EneaApiError("offline")]
    flow = flow_api.flow

    result = await flow.async_step_user({"username": "test@example.invalid", "password": "test"})
    assert result["step_id"] == "select_meter"
    selector = next(iter(result["data_schema"].schema.values()))
    assert selector.config["options"] == [
        {"value": "12345", "label": "590310600000001234 (G12)"},
        {"value": "67890", "label": "590310600000000001 (?)"},
    ]
    result = await flow.async_step_select_meter({"meter_id": "99999"})
    assert result["errors"] == {"base": "unknown"}
    result = await flow.async_step_select_meter({"meter_id": "67890"})
    assert result["step_id"] == "configure"
    result = await flow.async_step_configure(_options())
    assert result["data"]["meter_id"] == 67890
    assert result["data"]["meter_name"] == "590310600000000001"
    assert result["data"]["tariff"] == ""
    flow_api.session.close.assert_awaited_once_with()


async def test_duplicate_meter_is_rejected(flow_api):
    """An existing unique ID prevents creating a second entry for the same PPE."""
    flow = flow_api.flow
    flow._selected_meter = flow_api.client.get_meters.return_value[0]
    flow.hass.config_entries.async_entry_for_domain_unique_id.return_value = SimpleNamespace(source="user")

    with pytest.raises(AbortFlow) as exc:
        await flow.async_step_configure(_options())

    assert exc.value.reason == "already_configured"


async def test_configure_without_meter_aborts(flow_api):
    """An incomplete flow cannot create an entry without a meter identity."""
    result = await flow_api.flow.async_step_configure(_options())
    assert (result["type"], result["reason"]) == ("abort", "unknown")


@pytest.mark.parametrize("duration,expected", [
    pytest.param({}, {"update_interval": "interval_too_short"}, id="empty"),
    pytest.param({"minutes": 29}, {"update_interval": "interval_too_short"}, id="below-minimum"),
    pytest.param({"minutes": 30}, {}, id="at-minimum"),
    pytest.param({"minutes": 31}, {}, id="above-minimum"),
    pytest.param({"hours": 1}, {}, id="hours-only"),
])
@pytest.mark.parametrize("step", ["configure", "options"])
async def test_interval_validation_in_both_flows(flow_api, duration, expected, step):
    """Initial setup and later options enforce the same polling boundary."""
    flow = flow_api.flow
    flow._selected_meter = flow_api.client.get_meters.return_value[0]
    if step == "options":
        flow = module.EneaOptionsFlow()
        flow.handler = "entry"
        flow.flow_id = "options-flow"
        flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_get_known_entry=Mock(return_value=SimpleNamespace(options=_options())),
        ))
    options = _options(update_interval=duration)

    result = await (flow.async_step_init(options) if step == "options" else flow.async_step_configure(options))

    if expected:
        assert result["type"] == "form"
        assert result["errors"] == expected
        interval_field = next(key for key in result["data_schema"].schema if key == "update_interval")
        assert interval_field.default() == duration
    else:
        assert result["type"] == "create_entry"
        assert result["data" if step == "options" else "options"] == options


@pytest.mark.parametrize("enabled", [
    None, "fetch_consumption", "fetch_generation", "fetch_power_consumption", "fetch_power_generation",
], ids=["none", "consumption", "generation", "power-consumption", "power-generation"])
async def test_each_measurement_can_be_selected_alone(flow_api, enabled):
    """Any one measurement suffices, but disabling every measurement is rejected."""
    options = _options(fetch_consumption=False)
    if enabled is not None:
        options[enabled] = True
    flow_api.flow._selected_meter = flow_api.client.get_meters.return_value[0]

    result = await flow_api.flow.async_step_configure(options)

    if enabled is None:
        assert result["type"] == "form"
        assert result["errors"] == {"base": "at_least_one_fetch_type"}
    else:
        assert result["type"] == "create_entry"
        assert result["options"] == options


@pytest.mark.parametrize("payload", [
    {}, {"username": "test@example.invalid"}, {"password": "old"},
    {"username": "test@example.invalid", "password": "old", "meter_id": 12345},
], ids=["empty", "username-only", "password-only", "initial-entry-data"])
async def test_initial_reauth_does_not_resubmit_old_credentials(flow_api, monkeypatch, payload):
    """HA's initial reauth payload prompts the user without authenticating it."""
    monkeypatch.setattr(flow_api.flow, "_get_reauth_entry", Mock(return_value=SimpleNamespace()))

    result = await flow_api.flow.async_step_reauth(payload)

    assert (result["type"], result["step_id"], result["errors"]) == ("form", "reauth", {})
    flow_api.factory.assert_not_called()


@pytest.mark.parametrize("step", ["reauth", "reconfigure"])
@pytest.mark.parametrize("error,expected", [
    pytest.param(None, None, id="success"),
    pytest.param(module.EneaAuthError("rejected"), "invalid_auth", id="authentication"),
    pytest.param(module.EneaApiError("offline"), "cannot_connect", id="connection"),
    pytest.param(RuntimeError("unexpected"), "unknown", id="unexpected"),
])
async def test_credential_updates_preserve_meter_and_reload_only_on_success(
    flow_api, monkeypatch, step, error, expected,
):
    """Credential changes keep all other entry data and never reload on failure."""
    entry = SimpleNamespace(entry_id="entry", data={
        "username": "old@example.invalid", "password": "old", "meter_id": 12345,
        "meter_name": "590310600000001234", "tariff": "G12", "balanced_history": True,
    })
    flow = flow_api.flow
    flow.context = {"source": step}
    monkeypatch.setattr(flow, f"_get_{step}_entry", Mock(return_value=entry))
    flow_api.client.authenticate.side_effect = error
    credentials = {"username": "new@example.invalid", "password": "new"}

    result = await getattr(flow, f"async_step_{step}")(credentials)

    flow_api.factory.assert_called_once_with(flow_api.session, "new@example.invalid", "new")
    flow_api.session.close.assert_awaited_once_with()
    registry = flow.hass.config_entries
    if expected:
        assert (result["type"], result["step_id"], result["errors"]) == ("form", step, {"base": expected})
        registry.async_update_entry.assert_not_called()
        registry.async_reload.assert_not_awaited()
    else:
        assert (result["type"], result["reason"]) == ("abort", f"{step}_successful")
        registry.async_update_entry.assert_called_once_with(entry, data={
            "username": "new@example.invalid", "password": "new", "meter_id": 12345,
            "meter_name": "590310600000001234", "tariff": "G12", "balanced_history": True,
        })
        registry.async_reload.assert_awaited_once_with("entry")


async def test_initial_forms_preserve_defaults_without_logging_in(flow_api, monkeypatch):
    """Opening forms keeps stored options and does not submit credentials."""
    result = await flow_api.flow.async_step_user()
    assert (result["type"], result["step_id"], result["errors"]) == ("form", "user", {})
    monkeypatch.setattr(flow_api.flow, "_get_reconfigure_entry", Mock(return_value=SimpleNamespace()))
    result = await flow_api.flow.async_step_reconfigure()
    assert (result["type"], result["step_id"], result["errors"]) == ("form", "reconfigure", {})
    flow_api.factory.assert_not_called()

    stored = _options(update_interval={"hours": 2}, fetch_consumption=False, fetch_generation=True)
    entry = SimpleNamespace(options=stored)
    flow = module.EneaConfigFlow.async_get_options_flow(entry)
    flow.handler = "entry"
    flow.flow_id = "options-flow"
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_known_entry=Mock(return_value=entry)))
    result = await flow.async_step_init()
    assert (result["type"], result["step_id"], result["errors"]) == ("form", "init", {})
    assert result["data_schema"]({}) == stored
    result = await flow.async_step_init(_options(fetch_consumption=False))
    assert result["errors"] == {"base": "at_least_one_fetch_type"}
    assert result["data_schema"]({}) == _options(fetch_consumption=False)


async def test_meter_labels_use_the_corresponding_dashboard_address(flow_api):
    """Each meter receives its own address even when dashboard requests run together."""
    flow_api.client.get_meters.return_value.append({"id": 67890, "code": "590310600000000001"})
    flow_api.client.get_ppe_dashboard.side_effect = [
        {"address": {"street": "Testowa", "houseNum": "1", "city": "Miasto A"}},
        {"address": {"street": "Przykladowa", "houseNum": "2", "city": "Miasto B"}},
    ]

    result = await flow_api.flow.async_step_user({"username": "test@example.invalid", "password": "test"})

    selector = next(iter(result["data_schema"].schema.values()))
    assert selector.config["options"] == [
        {"value": "12345", "label": "590310600000001234 (G12) – Testowa 1, Miasto A"},
        {"value": "67890", "label": "590310600000000001 (?) – Przykladowa 2, Miasto B"},
    ]
