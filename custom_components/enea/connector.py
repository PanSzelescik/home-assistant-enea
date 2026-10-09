"""API client for the Portal Odbiorcy Enea."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from homeassistant.util import dt as dt_util

import aiohttp

from .const import (
    BILLING_PERIOD_MIN_SEGMENT,
    MEASUREMENT_ID_CONSUMPTION,
    URL_CONSUMPTION_RANGE,
    URL_CONSUMPTION_RANGE_DATA_SOURCE,
    URL_LOGIN,
    URL_PPE_DASHBOARD,
    URL_PPES,
    METERS_CACHE_TTL,
    PHASES_BY_METER_MODEL,
    PHASES_SOURCE_CAPACITY,
    PHASES_SOURCE_METER_MODEL,
    PHASES_THREE,
    PHASES_THREE_MIN_CAPACITY_KW,
    TARIFF_GROUP_ALIASES,
    DataSource,
    MeasurementType,
    Resolution,
)

_LOGGER = logging.getLogger(__name__)


class EneaApiError(Exception):
    """General API error."""


class EneaAuthError(EneaApiError):
    """Authentication failure (bad credentials or session expired)."""


@asynccontextmanager
async def _fetch(
    coro: Awaitable[aiohttp.ClientResponse],
) -> AsyncGenerator[aiohttp.ClientResponse, None]:
    """Async context manager that issues a request and translates connection errors.

    Automatically releases the response on exit, so callers never need to
    call resp.release() manually.
    """
    try:
        resp = await coro
    except aiohttp.ClientConnectorCertificateError as err:
        raise EneaApiError(
            f"SSL certificate error for Portal Odbiorcy Enea"
            f" (certificate may have expired): {err}"
        ) from err
    except aiohttp.ClientSSLError as err:
        raise EneaApiError(
            f"SSL error connecting to Portal Odbiorcy Enea: {err}"
        ) from err
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as err:
        # Some client errors (TooManyRedirects, InvalidURL) quote the URL with the
        # meter id; chaining them would bring it back in every logged traceback.
        raise EneaApiError(
            f"Cannot connect to Portal Odbiorcy Enea: {type(err).__name__}: "
            f"{hide_meter_id(str(err))}"
        ) from None
    try:
        yield resp
    finally:
        resp.release()


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
        self._auth_gen = 0
        self._auth_lock = asyncio.Lock()
        self._meters_cache: list[dict[str, Any]] | None = None
        self._meters_cache_time: datetime | None = None

    @property
    def session_closed(self) -> bool:
        """Return True if the underlying aiohttp session has been closed."""
        return self._session.closed

    @staticmethod
    async def _parse_response(
        resp: aiohttp.ClientResponse, label: str, started: float
    ) -> Any:
        """Check response status and return the parsed JSON body.

        Logs every request at debug level — path, status, body size and time
        since `started` (time.monotonic() before the request was sent).  The
        portal's meter id in the path is replaced with "…" (logs end up in public
        GitHub issues); measurement type and resolution stay readable.
        """
        body = await resp.read()
        _LOGGER.debug(
            "GET %s (%s): HTTP %d, %d B in %.2f s",
            hide_meter_id(resp.url.path),
            label,
            resp.status,
            len(body),
            time.monotonic() - started,
        )
        if resp.status != 200:
            raise EneaApiError(f"Unexpected response from {label} endpoint: {resp.status}")
        try:
            return await resp.json()
        except Exception as err:
            # aiohttp's ContentTypeError quotes the full URL, meter id included;
            # chaining it would bring the URL back in every logged traceback.
            raise EneaApiError(
                f"Failed to parse {label} response: {type(err).__name__}: "
                f"{hide_meter_id(str(err))}"
            ) from None

    def update_credentials(self, password: str) -> None:
        """Update password and invalidate the current session (e.g. after reauth)."""
        if self._password != password:
            self._password = password
            self._authenticated = False
            self._meters_cache = None
            self._meters_cache_time = None

    async def authenticate(self) -> None:
        """Log in to the Portal Odbiorcy Enea and store the session cookie."""
        async with _fetch(
            self._session.post(
                URL_LOGIN,
                json={"username": self._username, "password": self._password},
            )
        ) as resp:
            if resp.status == 401:
                raise EneaAuthError("Invalid username or password")
            if resp.status != 200:
                raise EneaApiError(f"Unexpected login response: {resp.status}")
        self._authenticated = True
        self._auth_gen += 1
        _LOGGER.debug("Successfully authenticated with Portal Odbiorcy Enea")

    async def _request(self, url: str, label: str) -> Any:
        """Perform an authenticated GET request, retrying once on session expiry."""
        if not self._authenticated:
            await self.authenticate()

        auth_gen = self._auth_gen
        started = time.monotonic()
        async with _fetch(self._session.get(url)) as resp:
            if resp.status not in (401, 403):
                return await self._parse_response(resp, label, started)

        async with self._auth_lock:
            if self._auth_gen == auth_gen:
                # Generation unchanged — we are the first to handle this expiry.
                _LOGGER.debug("Session expired, re-authenticating")
                self._authenticated = False
                self._meters_cache = None
                self._meters_cache_time = None
                await self.authenticate()

        started = time.monotonic()
        async with _fetch(self._session.get(url)) as resp:
            return await self._parse_response(resp, label, started)

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

        data: list[dict[str, Any]] = await self._request(URL_PPES, "ppes")
        self._meters_cache = data
        self._meters_cache_time = now
        return data

    async def get_ppe_dashboard(self, meter_id: int) -> dict[str, Any]:
        """Return full consumption dashboard data for a specific meter."""
        url = URL_PPE_DASHBOARD.format(meter_id=meter_id)
        return await self._request(url, "dashboard")

    async def get_consumption_data_range(
        self,
        meter_id: int,
        start_date: date,
        end_date: date,
        measurement_type: MeasurementType,
        resolution: Resolution,
        data_source: DataSource | None = None,
    ) -> dict[str, Any]:
        """Return consumption/power data for a date range at the given resolution.

        The response has the same structure as the single-day endpoint but
        contains resolution-dependent timeId slots per day, repeating for each
        day in the range.  For very large ranges, data may appear in
        'valuesToTable' instead of 'values' (same structure, different key).

        Args:
            meter_id: PPE identifier.
            start_date: Start date (inclusive).
            end_date: End date (inclusive); must be >= start_date.
            measurement_type: MeasurementType (1=energy consumed, 5=energy returned,
                              4=power consumed, 9=power returned).
            resolution: Resolution (1=15-min/96 entries per day,
                        2=60-min/24 entries per day).
            data_source: DataSource of a prosumer's meter (2=after balancing,
                         60-min resolution or coarser only); None sends none,
                         as Portal Odbiorcy Enea does for other meters.
        """
        if start_date > end_date:
            raise ValueError(
                f"start_date ({start_date}) must be <= end_date ({end_date})"
            )
        template = (
            URL_CONSUMPTION_RANGE if data_source is None else URL_CONSUMPTION_RANGE_DATA_SOURCE
        )
        url = template.format(
            meter_id=meter_id,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            measurement_type=measurement_type,
            resolution=resolution,
            data_source=data_source,
        )
        return await self._request(url, "consumption_range")


def get_active_meter(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the currently installed physical meter (no disassembly date)."""
    return next(
        (m for m in data.get("meters", []) if m.get("disassemblyDate") is None),
        None,
    )


def tariff_group_name(name: str | None) -> str:
    """Return the tariff group's name as the invoice and enea_prices give it.

    The Portal Odbiorcy Enea shortens some names (G12sezON comes as "G12sez");
    TARIFF_GROUP_ALIASES maps them back.  Other names pass through stripped,
    and a missing one becomes "".
    """
    group = (name or "").strip()
    return TARIFF_GROUP_ALIASES.get(group.casefold(), group)


def zone_name(label: str) -> str:
    """Return a ppeZones label without its OBIS code.

    "Dzień 1.8.1" → "Dzień", "Strefa zalecanego poboru 1.8.2" → "Strefa
    zalecanego poboru".  A label with no code is returned whole.
    """
    return re.sub(r"\s+\d+(?:\.\d+)+$", "", label.strip())


def agreement_tariffs(data: dict[str, Any]) -> list[tuple[date, date | None, str]]:
    """Return the tariff group of each agreement in the dashboard, oldest first.

    Each entry is (first day, day after the last one or None while in force,
    tariff group name).  An agreement's "from" and "to" are local midnights in
    milliseconds, the next agreement starting where the previous one ends.
    Agreements without a start or a group name are left out.
    """
    spans: list[tuple[date, date | None, str]] = []
    for agreement in data.get("agreements") or []:
        start, end = agreement.get("from"), agreement.get("to")
        group = tariff_group_name(agreement.get("tariffGroupName"))
        if start is None or not group:
            continue
        spans.append(
            (
                dt_util.as_local(dt_util.utc_from_timestamp(start / 1000)).date(),
                dt_util.as_local(dt_util.utc_from_timestamp(end / 1000)).date()
                if end is not None
                else None,
                group,
            )
        )
    return sorted(spans, key=lambda span: span[0])


def billing_period_starts(data: dict[str, Any]) -> list[date]:
    """Return billing period start dates found in the dashboard's billingWeekData.

    Segments spanning a whole billing period are interleaved with daily ones;
    each such segment starts on the first day of a period on the invoice.  The
    very first segment is skipped — it starts where the portal's data window
    begins, not on a reading date.  All measurements share the same segments,
    so the consumption one is enough.
    """
    measurement = next(
        (
            m
            for m in data.get("billingWeekData") or []
            if m.get("measurementId") == MEASUREMENT_ID_CONSUMPTION
        ),
        None,
    )
    if measurement is None:
        return []
    starts: list[date] = []
    for i, segment in enumerate(measurement.get("values") or []):
        time_from, time_to = segment.get("timeFrom"), segment.get("timeTo")
        if i == 0 or time_from is None or time_to is None:
            continue
        start = dt_util.utc_from_timestamp(time_from / 1000)
        if dt_util.utc_from_timestamp(time_to / 1000) - start >= BILLING_PERIOD_MIN_SEGMENT:
            starts.append(dt_util.as_local(start).date())
    return starts


def infer_phases(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return the inferred installation phases and what they were inferred from.

    The Portal Odbiorcy Enea does not report the number of phases.  The active
    meter model decides when it is a known one; otherwise a contractual capacity
    beyond what a single-phase connection carries implies three phases.  Any
    other case stays unknown — (None, None).
    """
    model = ((get_active_meter(data) or {}).get("typeName") or "").strip().upper()
    if (phases := PHASES_BY_METER_MODEL.get(model)) is not None:
        return phases, PHASES_SOURCE_METER_MODEL
    capacity = data.get("agreementPower")
    if capacity is not None and capacity >= PHASES_THREE_MIN_CAPACITY_KW:
        return PHASES_THREE, PHASES_SOURCE_CAPACITY
    return None, None


def mask_ppe(text: str) -> str:
    """Shorten every PPE number in text to its last four digits, e.g. "…1234".

    Logs are pasted into public GitHub issues; the tail still tells meters of
    one account apart.  Covers bare meter codes and statistic ids alike.
    """
    return re.sub(r"\d{10,}", lambda m: f"…{m.group()[-4:]}", text)


def hide_meter_id(text: str) -> str:
    """Replace the portal's meter id in a URL path, or text quoting one, with "…".

    The id is a URL segment of at least four digits.  Dates in the path are
    left alone: their year is followed by "-", not by the end of the segment.
    """
    return re.sub(r"(?<=/)\d{4,}(?![\w-])", "…", text)


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
