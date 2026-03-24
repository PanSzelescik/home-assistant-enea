"""Testy integracyjne dla config flow, options flow, reauth i reconfigure."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.enea.const import (
    CONF_BACKFILL_DAYS,
    CONF_FETCH_CONSUMPTION,
    CONF_FETCH_GENERATION,
    CONF_METER_ID,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)
from custom_components.enea.connector import EneaApiError, EneaAuthError

METER_ID = 73689
METER_CODE = "590310600000000001"

SAMPLE_DASHBOARD = {
    "tariffGroupName": "G12",
    "agreementPower": 14.0,
    "detailedStatus": "Aktywny",
    "address": {"street": "Pastelowa", "houseNum": "8", "postCode": "60-198", "city": "Poznań"},
    "meters": [{"serialNumber": "ABC", "typeName": "OTUS3", "assemblyDate": 1700000000000}],
    "currentValues": [],
}

SELECT_METER_INPUT = {
    CONF_METER_ID: str(METER_ID),
    CONF_BACKFILL_DAYS: "0",
    CONF_UPDATE_INTERVAL: {"hours": 3, "minutes": 30, "seconds": 0},
    CONF_FETCH_CONSUMPTION: True,
    CONF_FETCH_GENERATION: True,
}


def mock_api_client(authenticate=None, get_meters=None, get_ppe_dashboard=None):
    """Buduje zamockowanego EneaApiClient."""
    client = MagicMock()
    client.authenticate = authenticate or AsyncMock()
    client.get_meters = get_meters or AsyncMock(
        return_value=[{"id": METER_ID, "code": METER_CODE, "tariffGroup": {"name": "G12"}}]
    )
    client.get_ppe_dashboard = get_ppe_dashboard or AsyncMock(return_value=SAMPLE_DASHBOARD)
    return client


# ---------------------------------------------------------------------------
# Pełny przepływ konfiguracji
# ---------------------------------------------------------------------------


async def test_pełny_przepływ_tworzy_config_entry(hass: HomeAssistant, enable_custom_integrations):
    client = mock_api_client()
    with (
        patch("custom_components.enea.config_flow.async_create_clientsession"),
        patch("custom_components.enea.config_flow.EneaApiClient", return_value=client),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test@example.com", "password": "hasło"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "select_meter"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], SELECT_METER_INPUT
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_METER_ID] == METER_ID


async def test_przepływ_błąd_logowania(hass: HomeAssistant, enable_custom_integrations):
    client = mock_api_client(authenticate=AsyncMock(side_effect=EneaAuthError("bad")))
    with (
        patch("custom_components.enea.config_flow.async_create_clientsession"),
        patch("custom_components.enea.config_flow.EneaApiClient", return_value=client),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "x@x.pl", "password": "złe"},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_przepływ_błąd_połączenia(hass: HomeAssistant, enable_custom_integrations):
    client = mock_api_client(authenticate=AsyncMock(side_effect=EneaApiError("timeout")))
    with (
        patch("custom_components.enea.config_flow.async_create_clientsession"),
        patch("custom_components.enea.config_flow.EneaApiClient", return_value=client),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "x@x.pl", "password": "hasło"},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_przepływ_błąd_gdy_żaden_typ_nie_wybrany(hass: HomeAssistant, enable_custom_integrations):
    """Próba konfiguracji bez fetch_consumption i fetch_generation → błąd."""
    client = mock_api_client()
    with (
        patch("custom_components.enea.config_flow.async_create_clientsession"),
        patch("custom_components.enea.config_flow.EneaApiClient", return_value=client),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test@example.com", "password": "hasło"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**SELECT_METER_INPUT, CONF_FETCH_CONSUMPTION: False, CONF_FETCH_GENERATION: False},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "at_least_one_fetch_type"


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def test_options_flow_zmienia_interwał(hass: HomeAssistant, mock_config_entry, enable_custom_integrations):
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_UPDATE_INTERVAL: {"hours": 1, "minutes": 0, "seconds": 0},
            CONF_FETCH_CONSUMPTION: True,
            CONF_FETCH_GENERATION: False,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FETCH_GENERATION] is False


async def test_options_flow_za_krótki_interwał(hass: HomeAssistant, mock_config_entry, enable_custom_integrations):
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_UPDATE_INTERVAL: {"hours": 0, "minutes": 5, "seconds": 0},  # za krótki
            CONF_FETCH_CONSUMPTION: True,
            CONF_FETCH_GENERATION: True,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_UPDATE_INTERVAL] == "interval_too_short"
