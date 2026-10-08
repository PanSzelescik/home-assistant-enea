"""Logs never carry the PPE number or the portal's meter id in full."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.enea.connector import EneaApiClient, EneaApiError, mask_ppe


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("590310600015949990", "…9990"),
        ("enea:590310600015949990_energia_pobrana_dzien", "enea:…9990_energia_pobrana_dzien"),
        ("Energia pobrana – Dzień", "Energia pobrana – Dzień"),
        ("2026-10-07", "2026-10-07"),
    ],
)
def test_mask_ppe(text: str, expected: str) -> None:
    """PPE numbers shrink to their tail; dates and names stay as they are."""
    assert mask_ppe(text) == expected


def _response(path: str, status: int, body: bytes) -> Any:
    """Return an aiohttp response stand-in."""

    async def read() -> bytes:
        return body

    async def json() -> Any:
        return {}

    return SimpleNamespace(
        url=SimpleNamespace(path=path), status=status, read=read, json=json
    )


async def test_request_log_hides_the_meter_id(caplog: pytest.LogCaptureFixture) -> None:
    """The debug line keeps type and resolution but not the portal's meter id."""
    caplog.set_level(logging.DEBUG, logger="custom_components.enea.connector")
    path = "/portalOdbiorcy/api/consumption/73689/2026-01-01/2026-06-30/1/2"

    await EneaApiClient._parse_response(_response(path, 200, b"{}"), "range", time.monotonic())

    assert "73689" not in caplog.text
    assert "/consumption/…/2026-01-01/2026-06-30/1/2 (range): HTTP 200, 2 B" in caplog.text


async def test_failed_request_is_logged_too(caplog: pytest.LogCaptureFixture) -> None:
    """An unexpected status is logged before the error is raised."""
    caplog.set_level(logging.DEBUG, logger="custom_components.enea.connector")
    path = "/portalOdbiorcy/api/consumptionDashboard/ppe/73689"

    with pytest.raises(EneaApiError):
        await EneaApiClient._parse_response(_response(path, 500, b""), "dashboard", 0.0)

    assert "/consumptionDashboard/ppe/… (dashboard): HTTP 500" in caplog.text
