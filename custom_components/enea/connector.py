"""API client for the Portal Odbiorcy Enea."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

import aiohttp

from .const import (
    CONST_URL_CONSUMPTION,
    CONST_URL_LOGIN,
    CONST_URL_PPE_DASHBOARD,
    CONST_URL_PPES,
    METERS_CACHE_TTL,
)

_LOGGER = logging.getLogger(__name__)


class EneaApiError(Exception):
    """General API error."""


class EneaAuthError(EneaApiError):
    """Authentication failure (bad credentials or session expired)."""


class EneaApiClient:
    """Client for the Portal Odbiorcy Enea REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._authenticated = False
        self._auth_lock = asyncio.Lock()
        self._meters_cache: list[dict[str, Any]] | None = None
        self._meters_cache_time: datetime | None = None

    async def _fetch(self, coro) -> aiohttp.ClientResponse:
        """Await a request coroutine, translating connection errors to EneaApiError."""
        try:
            return await coro
        except aiohttp.ClientConnectorSSLError as err:
            raise EneaApiError(
                f"SSL certificate error for Portal Odbiorcy Enea"
                f" (certificate may have expired): {err}"
            ) from err
        except (aiohttp.ClientError, RuntimeError) as err:
            raise EneaApiError(f"Cannot connect to Portal Odbiorcy Enea: {err}") from err

    def update_credentials(self, password: str) -> None:
        """Update password and invalidate the current session (e.g. after reauth)."""
        if self._password != password:
            self._password = password
            self._authenticated = False
            self._meters_cache = None
            self._meters_cache_time = None

    async def authenticate(self) -> None:
        """Log in to the Portal Odbiorcy Enea and store the session cookie."""
        resp = await self._fetch(
            self._session.post(
                CONST_URL_LOGIN,
                json={"username": self._username, "password": self._password},
            )
        )

        if resp.status == 401:
            raise EneaAuthError("Invalid username or password")
        if resp.status != 200:
            raise EneaApiError(f"Unexpected login response: {resp.status}")

        self._authenticated = True
        _LOGGER.debug("Successfully authenticated with Portal Odbiorcy Enea")

    async def _request(self, url: str, label: str) -> Any:
        """Perform an authenticated GET request, retrying once on session expiry."""
        if not self._authenticated:
            await self.authenticate()

        resp = await self._fetch(self._session.get(url))

        if resp.status in (401, 403):
            async with self._auth_lock:
                if not self._authenticated:
                    # Another concurrent request already re-authenticated.
                    pass
                else:
                    _LOGGER.debug("Session expired, re-authenticating")
                    self._authenticated = False
                    self._meters_cache = None
                    self._meters_cache_time = None
                    await self.authenticate()
            resp = await self._fetch(self._session.get(url))

        if resp.status != 200:
            raise EneaApiError(f"Unexpected response from {label} endpoint: {resp.status}")

        try:
            return await resp.json()
        except Exception as err:
            raise EneaApiError(f"Failed to parse {label} response: {err}") from err

    async def get_meters(self) -> list[dict[str, Any]]:
        """Return the list of PPE meters associated with the account.

        Results are cached for METERS_CACHE_TTL to avoid redundant API calls
        when multiple coordinators (one per meter) refresh at the same time.
        """
        now = dt_util.utcnow()
        if (
            self._meters_cache is not None
            and self._meters_cache_time is not None
            and now - self._meters_cache_time < METERS_CACHE_TTL
        ):
            _LOGGER.debug("Returning cached meters list")
            return self._meters_cache

        data: list[dict[str, Any]] = await self._request(CONST_URL_PPES, "ppes")
        self._meters_cache = data
        self._meters_cache_time = now
        return data

    async def get_ppe_dashboard(self, meter_id: int) -> dict[str, Any]:
        """Return full consumption dashboard data for a specific meter."""
        url = CONST_URL_PPE_DASHBOARD.format(meter_id=meter_id)
        return await self._request(url, "dashboard")

    async def get_consumption_data(
        self,
        meter_id: int,
        date: str,
        measurement_type: int,
        resolution: int = 1,
    ) -> dict[str, Any]:
        """Return 15-min or 60-min consumption/power data for a specific date.

        Args:
            meter_id: PPE identifier.
            date: Date string in YYYY-MM-DD format.
            measurement_type: 1=energy consumed, 5=energy returned,
                              4=power consumed, 9=power returned.
            resolution: 1=15-minute (96 entries), 2=60-minute (24 entries).
        """
        url = CONST_URL_CONSUMPTION.format(
            meter_id=meter_id,
            date=date,
            measurement_type=measurement_type,
            resolution=resolution,
        )
        return await self._request(url, "consumption")


def get_active_meter(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the currently installed physical meter (no disassembly date)."""
    return next(
        (m for m in data.get("meters", []) if m.get("disassemblyDate") is None),
        None,
    )


def format_address(addr: dict[str, Any] | None) -> str | None:
    """Format an address dict into a readable string."""
    if not addr:
        return None
    street = addr.get("street")
    house = addr.get("houseNum")
    apartment = addr.get("apartmentNum")
    house_apt = f"{house}/{apartment}" if house and apartment else house
    street_with_number = " ".join(p for p in [street, house_apt] if p) or None

    parcel = addr.get("parcelNum")
    parts = [
        street_with_number,
        addr.get("district"),
        addr.get("postCode"),
        addr.get("city"),
        f"Działka {parcel}" if parcel else None,
    ]
    return ", ".join(p for p in parts if p) or None
