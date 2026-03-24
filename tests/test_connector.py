"""Testy jednostkowe dla connector.py (EneaApiClient, format_address, get_active_meter)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.enea.connector import (
    EneaApiClient,
    EneaApiError,
    EneaAuthError,
    format_address,
    get_active_meter,
)


# ---------------------------------------------------------------------------
# format_address
# ---------------------------------------------------------------------------


def test_format_address_pełny_adres():
    addr = {
        "street": "Pastelowa",
        "houseNum": "8",
        "apartmentNum": "3",
        "postCode": "60-198",
        "city": "Poznań",
        "district": None,
        "parcelNum": None,
    }
    assert format_address(addr) == "Pastelowa 8/3, 60-198, Poznań"


def test_format_address_bez_mieszkania():
    addr = {"street": "Lipowa", "houseNum": "5", "postCode": "00-001", "city": "Warszawa"}
    assert format_address(addr) == "Lipowa 5, 00-001, Warszawa"


def test_format_address_tylko_miasto():
    assert format_address({"city": "Gdańsk", "postCode": "80-001"}) == "80-001, Gdańsk"


def test_format_address_z_działką():
    addr = {"postCode": "62-001", "city": "Unisław", "parcelNum": "15/3"}
    result = format_address(addr)
    assert result is not None
    assert "Działka 15/3" in result


def test_format_address_none():
    assert format_address(None) is None


def test_format_address_pusty_słownik():
    assert format_address({}) is None


def test_format_address_z_dzielnicą():
    addr = {
        "street": "Długa",
        "houseNum": "1",
        "district": "Śródmieście",
        "postCode": "00-001",
        "city": "Warszawa",
    }
    result = format_address(addr)
    assert result is not None
    assert "Śródmieście" in result


# ---------------------------------------------------------------------------
# get_active_meter
# ---------------------------------------------------------------------------


def test_get_active_meter_zwraca_licznik_bez_daty_demontażu():
    data = {
        "meters": [
            {"serialNumber": "STARY", "disassemblyDate": 1700000000000},
            {"serialNumber": "NOWY"},
        ]
    }
    result = get_active_meter(data)
    assert result is not None
    assert result["serialNumber"] == "NOWY"


def test_get_active_meter_brak_liczników():
    assert get_active_meter({"meters": []}) is None


def test_get_active_meter_wszystkie_zdemontowane():
    data = {"meters": [{"serialNumber": "X", "disassemblyDate": 1700000000000}]}
    assert get_active_meter(data) is None


def test_get_active_meter_brak_klucza_meters():
    assert get_active_meter({}) is None


def test_get_active_meter_jeden_aktywny():
    data = {"meters": [{"serialNumber": "JEDEN"}]}
    result = get_active_meter(data)
    assert result is not None
    assert result["serialNumber"] == "JEDEN"


# ---------------------------------------------------------------------------
# EneaApiClient — pomocnik do tworzenia mocków sesji aiohttp
# ---------------------------------------------------------------------------


def make_session(status: int = 200, json_data=None) -> MagicMock:
    """Tworzy mockowaną sesję aiohttp."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data or {})
    response.release = MagicMock()

    session = MagicMock()
    session.closed = False
    # session.post(...) i session.get(...) są coroutines (AsyncMock)
    session.post = AsyncMock(return_value=response)
    session.get = AsyncMock(return_value=response)
    return session


# ---------------------------------------------------------------------------
# EneaApiClient.authenticate
# ---------------------------------------------------------------------------


async def test_authenticate_sukces():
    session = make_session(status=200)
    client = EneaApiClient(session, "user@example.com", "hasło")
    await client.authenticate()
    assert client._authenticated is True


async def test_authenticate_nieprawidłowe_dane_logowania():
    session = make_session(status=401)
    client = EneaApiClient(session, "user@example.com", "złe_hasło")
    with pytest.raises(EneaAuthError):
        await client.authenticate()


async def test_authenticate_nieoczekiwany_status():
    session = make_session(status=500)
    client = EneaApiClient(session, "user@example.com", "hasło")
    with pytest.raises(EneaApiError):
        await client.authenticate()


async def test_authenticate_błąd_ssl():
    session = MagicMock()
    session.closed = False
    _os_err = MagicMock()
    _os_err.errno = 1
    _os_err.strerror = "ssl error"
    session.post = AsyncMock(
        side_effect=aiohttp.ClientSSLError(MagicMock(), _os_err)
    )
    client = EneaApiClient(session, "user@example.com", "hasło")
    with pytest.raises(EneaApiError, match="SSL error"):
        await client.authenticate()


async def test_authenticate_błąd_połączenia():
    session = MagicMock()
    session.closed = False
    session.post = AsyncMock(side_effect=aiohttp.ClientConnectionError("timeout"))
    client = EneaApiClient(session, "user@example.com", "hasło")
    with pytest.raises(EneaApiError, match="Cannot connect"):
        await client.authenticate()


# ---------------------------------------------------------------------------
# EneaApiClient._request — ponawianie przy wygaśnięciu sesji
# ---------------------------------------------------------------------------


async def test_request_ponawia_logowanie_przy_401():
    """Przy odpowiedzi 401 klient powinien ponownie się zalogować i powtórzyć żądanie."""
    resp_401 = MagicMock()
    resp_401.status = 401
    resp_401.release = MagicMock()

    resp_200 = MagicMock()
    resp_200.status = 200
    resp_200.json = AsyncMock(return_value={"ok": True})
    resp_200.release = MagicMock()

    session = MagicMock()
    session.closed = False
    # Pierwsze GET → 401, drugie GET → 200
    session.get = AsyncMock(side_effect=[resp_401, resp_200])
    # POST (re-auth) → 200
    session.post = AsyncMock(return_value=MagicMock(status=200, release=MagicMock()))

    client = EneaApiClient(session, "user@example.com", "hasło")
    client._authenticated = True

    result = await client._request("https://example.com/api", "test")
    assert result == {"ok": True}
    assert session.post.called  # re-auth się odbył


async def test_request_rzuca_wyjątek_gdy_re_auth_nie_pomógł():
    """Jeśli ponowne logowanie nie pomaga, _request powinien rzucić EneaApiError."""
    resp_401 = MagicMock()
    resp_401.status = 401
    resp_401.release = MagicMock()

    session = MagicMock()
    session.closed = False
    session.get = AsyncMock(return_value=resp_401)
    session.post = AsyncMock(return_value=MagicMock(status=200, release=MagicMock()))

    client = EneaApiClient(session, "user@example.com", "hasło")
    client._authenticated = True

    with pytest.raises(EneaApiError):
        await client._request("https://example.com/api", "test")


# ---------------------------------------------------------------------------
# EneaApiClient.get_meters — cache
# ---------------------------------------------------------------------------


async def test_get_meters_cachuje_wyniki():
    """Drugie wywołanie w oknie TTL nie powinno wykonywać żądania HTTP."""
    meters = [{"id": 1, "code": "PPE001"}]
    session = make_session(status=200, json_data=meters)
    client = EneaApiClient(session, "user@example.com", "hasło")
    client._authenticated = True

    first = await client.get_meters()
    second = await client.get_meters()

    assert first == second
    # GET wywoływany tylko raz
    assert session.get.call_count == 1


async def test_update_credentials_czyści_cache():
    """update_credentials z nowym hasłem powinien zresetować cache i flagę auth."""
    session = make_session(status=200, json_data=[])
    client = EneaApiClient(session, "user@example.com", "stare_hasło")
    client._authenticated = True
    client._meters_cache = [{"id": 1}]

    client.update_credentials("nowe_hasło")

    assert client._authenticated is False
    assert client._meters_cache is None
