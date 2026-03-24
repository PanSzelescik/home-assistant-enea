"""Fixtures współdzielone między wszystkimi testami integracji Enea."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

from unittest.mock import patch as _patch


@pytest.fixture
def mock_recorder_before_hass():
    """Patch recorder async_setup so it doesn't fail in tests without a real DB."""
    with _patch("homeassistant.components.recorder.async_setup", return_value=True):
        yield

from custom_components.enea.connector import EneaApiClient
from custom_components.enea.const import (
    CONF_BACKFILL_DAYS,
    CONF_FETCH_CONSUMPTION,
    CONF_FETCH_GENERATION,
    CONF_METER_ID,
    CONF_METER_NAME,
    CONF_TARIFF,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)
from custom_components.enea.coordinator import EneaUpdateCoordinator

METER_ID = 73689
METER_CODE = "590310600000000001"

SAMPLE_DASHBOARD = {
    "tariffGroupName": "G12",
    "agreementPower": 14.0,
    "detailedStatus": "Aktywny_Pod napięciem",
    "address": {
        "street": "Pastelowa",
        "houseNum": "8",
        "postCode": "60-198",
        "city": "Poznań",
        "district": None,
        "apartmentNum": None,
        "parcelNum": None,
    },
    "meters": [
        {
            "serialNumber": "ABC123",
            "typeName": "OTUS3",
            "assemblyDate": 1700000000000,
            "disassemblyDate": None,
        }
    ],
    "currentValues": [
        {
            "measurementId": 1,
            "readingDate": 1741000000000,
            "ppeZones": ["Dzień 1.8.1", "Noc 1.8.2"],
            "valueNoZones": {"value": 0.658},
            "valueZone1": {"value": 0.658},
            "valueZone2": {"value": 0.0},
        }
    ],
}

SAMPLE_DAY_WITH_DATA = {
    "zones": [{"id": 1, "name": "Dzień"}, {"id": 2, "name": "Noc"}],
    "values": [
        {
            "timeId": t,
            "items": [
                {"tarifZoneId": 1, "value": 0.10},
                {"tarifZoneId": 2, "value": 0.05},
            ],
        }
        for t in range(1, 25)
    ],
}

SAMPLE_DAY_EMPTY = {
    "zones": [{"id": 1, "name": "Dzień"}],
    "values": [
        {"timeId": t, "items": [{"tarifZoneId": 1, "value": None}]}
        for t in range(1, 25)
    ],
}


@pytest.fixture
def mock_client() -> MagicMock:
    """Zwraca mock EneaApiClient z sensownymi wartościami domyślnymi."""
    client = MagicMock(spec=EneaApiClient)
    client.session_closed = False
    client.authenticate = AsyncMock()
    client.update_credentials = MagicMock()
    client.get_meters = AsyncMock(
        return_value=[
            {"id": METER_ID, "code": METER_CODE, "tariffGroup": {"name": "G12"}}
        ]
    )
    client.get_ppe_dashboard = AsyncMock(return_value=SAMPLE_DASHBOARD)
    client.get_consumption_data = AsyncMock(return_value=SAMPLE_DAY_EMPTY)
    return client


@pytest.fixture
def coordinator(mock_client: MagicMock) -> EneaUpdateCoordinator:
    """Zwraca EneaUpdateCoordinator z mockowanym hass i klientem."""
    hass = MagicMock()
    return EneaUpdateCoordinator(
        hass=hass,
        connector=mock_client,
        meter_id=METER_ID,
        meter_code=METER_CODE,
        backfill_days=0,
        update_interval=timedelta(hours=3),
    )


@pytest.fixture
def mock_config_entry():
    """Zwraca MockConfigEntry dla integracji Enea."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "test@example.com",
            "password": "secret",
            CONF_METER_ID: METER_ID,
            CONF_METER_NAME: METER_CODE,
            CONF_TARIFF: "G12",
            CONF_BACKFILL_DAYS: 0,
        },
        options={
            CONF_UPDATE_INTERVAL: {"hours": 3, "minutes": 30, "seconds": 0},
            CONF_FETCH_CONSUMPTION: True,
            CONF_FETCH_GENERATION: True,
        },
        unique_id=METER_CODE,
    )
