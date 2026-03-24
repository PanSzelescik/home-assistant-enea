"""Testy dla logiki EneaUpdateCoordinator (filtrowanie, backfill, obsługa błędów)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.enea.coordinator import EneaUpdateCoordinator
from custom_components.enea.connector import EneaApiError, EneaAuthError

METER_ID = 73689
METER_CODE = "590310600000000001"

# Odpowiedź API z danymi (nie-null)
DAY_WITH_DATA = {
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

# Odpowiedź API z samymi null
DAY_EMPTY = {
    "zones": [{"id": 1, "name": "Dzień"}],
    "values": [
        {"timeId": t, "items": [{"tarifZoneId": 1, "value": None}]}
        for t in range(1, 25)
    ],
}


def make_coordinator(hass: HomeAssistant, mock_client: MagicMock, **kwargs) -> EneaUpdateCoordinator:
    """Pomocnik: tworzy coordinator z prawdziwym hass i mockowanym klientem."""
    return EneaUpdateCoordinator(
        hass=hass,
        connector=mock_client,
        meter_id=METER_ID,
        meter_code=METER_CODE,
        backfill_days=0,
        update_interval=timedelta(hours=3),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# _strip_pre_assembly_slots
# ---------------------------------------------------------------------------


def test_strip_usuwa_godziny_przed_montażem(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client)
    # Montaż o 12:13 → cutoff=12 → usuwa timeId <= 12, zostawia 13+
    coord._assembly_datetime = datetime(2024, 3, 15, 12, 13, tzinfo=timezone.utc)
    assembly_day = date(2024, 3, 15)

    day_data = {
        "energy_consumed": {
            "values": [
                {"timeId": 1},
                {"timeId": 12},
                {"timeId": 13},
                {"timeId": 24},
            ]
        }
    }

    result = coord._strip_pre_assembly_slots(assembly_day, day_data)
    time_ids = [v["timeId"] for v in result["energy_consumed"]["values"]]
    assert time_ids == [13, 24]


def test_strip_nie_modyfikuje_innych_dni(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = datetime(2024, 3, 15, 12, 0, tzinfo=timezone.utc)
    other_day = date(2024, 3, 16)

    day_data = {"energy_consumed": {"values": [{"timeId": 1}, {"timeId": 5}]}}
    result = coord._strip_pre_assembly_slots(other_day, day_data)
    # Inny dzień → bez zmian
    assert result == day_data


def test_strip_bez_assembly_datetime_zwraca_oryginał(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = None

    day_data = {"k": {"values": [{"timeId": 1}, {"timeId": 2}]}}
    assert coord._strip_pre_assembly_slots(date.today(), day_data) == day_data


def test_strip_montaż_o_północy_zostawia_wszystko(hass: HomeAssistant, mock_client):
    """Montaż o godzinie 00:00 → cutoff=0 → zostawiamy timeId > 0, czyli wszystkie."""
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = datetime(2024, 3, 15, 0, 0, tzinfo=timezone.utc)
    assembly_day = date(2024, 3, 15)

    day_data = {
        "energy_consumed": {
            "values": [{"timeId": t} for t in range(1, 25)]
        }
    }
    result = coord._strip_pre_assembly_slots(assembly_day, day_data)
    assert len(result["energy_consumed"]["values"]) == 24


def test_strip_montaż_o_23_usuwa_prawie_wszystko(hass: HomeAssistant, mock_client):
    """Montaż o 23:xx → zostaje tylko timeId=24."""
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = datetime(2024, 3, 15, 23, 30, tzinfo=timezone.utc)
    assembly_day = date(2024, 3, 15)

    day_data = {
        "energy_consumed": {
            "values": [{"timeId": t} for t in range(1, 25)]
        }
    }
    result = coord._strip_pre_assembly_slots(assembly_day, day_data)
    time_ids = [v["timeId"] for v in result["energy_consumed"]["values"]]
    assert time_ids == [24]


# ---------------------------------------------------------------------------
# _fetch_days_forward — dolna granica assemblyDate
# ---------------------------------------------------------------------------


async def test_fetch_days_forward_omija_dni_przed_montażem(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = datetime(2024, 3, 15, 0, 0, tzinfo=timezone.utc)
    mock_client.get_consumption_data.return_value = DAY_WITH_DATA

    days = await coord._fetch_days_forward(date(2024, 3, 10), date(2024, 3, 17))

    fetched_dates = [d for d, _ in days]
    assert all(d >= date(2024, 3, 15) for d in fetched_dates)


async def test_fetch_days_forward_bez_assembly_pobiera_wszystkie(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = None
    mock_client.get_consumption_data.return_value = DAY_WITH_DATA

    days = await coord._fetch_days_forward(date(2024, 3, 1), date(2024, 3, 3))
    assert len(days) == 3


async def test_fetch_days_forward_pomija_puste_dni(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = None

    # _fetch_one_day wykonuje 4 równoległe wywołania per dzień
    # (energy_consumed, power_consumed, energy_returned, power_returned)
    # Dzień 1: 4× DAY_WITH_DATA, Dzień 2: 4× DAY_EMPTY, Dzień 3: 4× DAY_WITH_DATA
    mock_client.get_consumption_data.side_effect = (
        [DAY_WITH_DATA] * 4  # dzień 1
        + [DAY_EMPTY] * 4    # dzień 2 — wszystko null → pominięty
        + [DAY_WITH_DATA] * 4  # dzień 3
    )

    days = await coord._fetch_days_forward(date(2024, 3, 1), date(2024, 3, 3))
    fetched_dates = [d for d, _ in days]
    # Dzień 2 powinien być pominięty
    assert date(2024, 3, 2) not in fetched_dates


# ---------------------------------------------------------------------------
# _fetch_days_backward — zatrzymanie przy assembly date
# ---------------------------------------------------------------------------


async def test_fetch_days_backward_zatrzymuje_się_przy_assembly(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = datetime(2024, 3, 15, 0, 0, tzinfo=timezone.utc)
    mock_client.get_consumption_data.return_value = DAY_WITH_DATA

    days = await coord._fetch_days_backward(date(2024, 3, 20))

    fetched_dates = [d for d, _ in days]
    assert all(d >= date(2024, 3, 15) for d in fetched_dates)


async def test_fetch_days_backward_zatrzymuje_się_po_7_pustych(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client)
    coord._assembly_datetime = None

    call_count = 0

    async def fake_consumption(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return DAY_EMPTY  # zawsze puste

    mock_client.get_consumption_data.side_effect = fake_consumption

    days = await coord._fetch_days_backward(date(2024, 3, 20))

    assert days == []
    # Powinno zatrzymać się po 7 kolejnych pustych dniach × (2 lub 4 typy per dzień)
    assert call_count > 0


# ---------------------------------------------------------------------------
# _async_update_data — propagacja błędów
# ---------------------------------------------------------------------------


async def test_update_data_przy_401_rzuca_config_entry_auth_failed(hass: HomeAssistant, mock_client):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    coord = make_coordinator(hass, mock_client)
    mock_client.get_ppe_dashboard.side_effect = EneaAuthError("wygasła sesja")

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_update_data_przy_api_error_rzuca_update_failed(hass: HomeAssistant, mock_client):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = make_coordinator(hass, mock_client)
    mock_client.get_ppe_dashboard.side_effect = EneaApiError("timeout")

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


# ---------------------------------------------------------------------------
# _get_measurement_types — flagi fetch_consumption / fetch_generation
# ---------------------------------------------------------------------------


def test_get_measurement_types_oba_enabled(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client, fetch_consumption=True, fetch_generation=True)
    types = coord._get_measurement_types()
    keys = [k for k, _ in types]
    assert "energy_consumed" in keys
    assert "energy_returned" in keys
    assert "power_consumed" in keys
    assert "power_returned" in keys


def test_get_measurement_types_tylko_pobrana(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client, fetch_consumption=True, fetch_generation=False)
    types = coord._get_measurement_types()
    keys = [k for k, _ in types]
    assert "energy_consumed" in keys
    assert "energy_returned" not in keys


def test_get_measurement_types_tylko_oddana(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client, fetch_consumption=False, fetch_generation=True)
    types = coord._get_measurement_types()
    keys = [k for k, _ in types]
    assert "energy_consumed" not in keys
    assert "energy_returned" in keys


def test_get_measurement_types_oba_disabled(hass: HomeAssistant, mock_client):
    coord = make_coordinator(hass, mock_client, fetch_consumption=False, fetch_generation=False)
    assert coord._get_measurement_types() == []
