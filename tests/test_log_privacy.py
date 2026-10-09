"""Logs never carry the PPE number or the portal's meter id in full."""
from __future__ import annotations

import logging
import time
import traceback
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.enea.connector import (
    EneaApiClient,
    EneaApiError,
    _fetch,
    hide_meter_id,
    mask_ppe,
)

_RANGE_URL = URL(
    "https://portalodbiorcy.operator.enea.pl/portalOdbiorcy/api"
    "/consumption/12345/2026-01-01/2026-06-30/1/2"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("590310600000001234", "…1234"),
        ("enea:590310600000001234_energia_pobrana_dzien", "enea:…1234_energia_pobrana_dzien"),
        ("Energia pobrana – Dzień", "Energia pobrana – Dzień"),
        ("2026-10-07", "2026-10-07"),
    ],
)
def test_mask_ppe(text: str, expected: str) -> None:
    """PPE numbers shrink to their tail; dates and names stay as they are."""
    assert mask_ppe(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "/portalOdbiorcy/api/consumption/12345/2026-01-01/2026-06-30/1/2",
            "/portalOdbiorcy/api/consumption/…/2026-01-01/2026-06-30/1/2",
        ),
        ("/portalOdbiorcy/api/consumptionDashboard/ppe/12345", "/portalOdbiorcy/api/consumptionDashboard/ppe/…"),
        ("url='https://host/api/consumptionDashboard/ppe/12345'", "url='https://host/api/consumptionDashboard/ppe/…'"),
    ],
)
def test_hide_meter_id(text: str, expected: str) -> None:
    """The meter id goes, wherever the path ends; dates and short segments stay."""
    assert hide_meter_id(text) == expected


def _client_response_error(cls: type[aiohttp.ClientResponseError], message: str) -> Exception:
    """Return an aiohttp error that quotes the range endpoint URL, as aiohttp does."""
    headers = CIMultiDictProxy(CIMultiDict())
    request_info = aiohttp.RequestInfo(_RANGE_URL, "GET", headers, _RANGE_URL)
    return cls(request_info, (), message=message)


def _assert_no_meter_id(err: BaseException) -> None:
    """Neither the error nor its formatted traceback may quote the meter id."""
    assert "12345" in str(err.__context__)  # the original error did quote it
    assert "12345" not in "".join(traceback.format_exception(err))


def _response(path: str, status: int, body: bytes, json_error: Exception | None = None) -> Any:
    """Return an aiohttp response stand-in; json() raises json_error when given."""

    async def read() -> bytes:
        return body

    async def json() -> Any:
        if json_error is not None:
            raise json_error
        return {}

    return SimpleNamespace(
        url=SimpleNamespace(path=path), status=status, read=read, json=json
    )


async def test_request_log_hides_the_meter_id(caplog: pytest.LogCaptureFixture) -> None:
    """The debug line keeps type and resolution but not the portal's meter id."""
    caplog.set_level(logging.DEBUG, logger="custom_components.enea.connector")
    path = "/portalOdbiorcy/api/consumption/12345/2026-01-01/2026-06-30/1/2"

    await EneaApiClient._parse_response(_response(path, 200, b"{}"), "range", time.monotonic())

    assert "12345" not in caplog.text
    assert "/consumption/…/2026-01-01/2026-06-30/1/2 (range): HTTP 200, 2 B" in caplog.text


async def test_failed_request_is_logged_too(caplog: pytest.LogCaptureFixture) -> None:
    """An unexpected status is logged before the error is raised."""
    caplog.set_level(logging.DEBUG, logger="custom_components.enea.connector")
    path = "/portalOdbiorcy/api/consumptionDashboard/ppe/12345"

    with pytest.raises(EneaApiError):
        await EneaApiClient._parse_response(_response(path, 500, b""), "dashboard", 0.0)

    assert "/consumptionDashboard/ppe/… (dashboard): HTTP 500" in caplog.text


async def test_unparsable_response_error_hides_the_meter_id() -> None:
    """A maintenance page served instead of JSON must not leak the URL into the error."""
    json_error = _client_response_error(
        aiohttp.ContentTypeError, "Attempt to decode JSON with unexpected mimetype: text/html"
    )
    response = _response(_RANGE_URL.path, 200, b"<html>", json_error)

    with pytest.raises(EneaApiError) as exc_info:
        await EneaApiClient._parse_response(response, "range", 0.0)

    assert "ContentTypeError" in str(exc_info.value)
    assert "/consumption/…/2026-01-01/2026-06-30/1/2" in str(exc_info.value)
    _assert_no_meter_id(exc_info.value)


async def test_connection_error_hides_the_meter_id() -> None:
    """Client errors quoting the URL, like a redirect loop, lose the meter id too."""

    async def request() -> aiohttp.ClientResponse:
        raise _client_response_error(aiohttp.TooManyRedirects, "Exceeded 10 redirects")

    with pytest.raises(EneaApiError) as exc_info:
        async with _fetch(request()):
            pass

    assert "TooManyRedirects" in str(exc_info.value)
    _assert_no_meter_id(exc_info.value)
