"""Messages of the Portal Odbiorcy Enea shown as Home Assistant notifications.

The portal's "Wiadomości" carry notices such as planned maintenance of the
portal itself.  They are checked per account: one request covers every meter
of the account with the option on, plus the messages to every customer.  The
first check fetches every message there is; later ones only the days since
the last check, which the portal answers much faster.  A message becomes a
persistent notification once, when it is first seen, and only while the
portal would still present it — see should_show.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .connector import EneaApiClient, mask_ppe
from .const import (
    CONF_METER_ID,
    CONF_NOTIFICATIONS,
    DOMAIN,
    NOTIFICATION_ID,
    NOTIFICATION_MAX_AGE,
    NOTIFICATIONS_MIN_INTERVAL,
    NOTIFICATIONS_STORAGE_KEY,
    NOTIFICATIONS_STORAGE_VERSION,
    URL_NOTIFICATIONS_PAGE,
)

_LOGGER = logging.getLogger(__name__)


def _local_time(timestamp_ms: int | None) -> datetime | None:
    """Return a timestamp of the portal (ms since the epoch) as local time."""
    if timestamp_ms is None:
        return None
    return dt_util.as_local(dt_util.utc_from_timestamp(timestamp_ms / 1000))


def should_show(message: dict[str, Any], now: datetime) -> bool:
    """Return True when a newly seen message is worth a notification.

    A message past its expiry date is never shown.  The portal presents one
    flagged presentedAfterEveryLogin in a dialog after every login, read or
    not, so it counts as current.  Otherwise a message read in the portal has
    been seen already; an unread one counts while the portal still has
    something to show: one flagged presentedAfterLogin waits for the next
    login, one with an expiry date in the future is current, and one without
    either counts for NOTIFICATION_MAX_AGE after it was received — the first
    check fetches the whole history, unread messages from years ago included.
    """
    expiry = _local_time(message.get("expiryDate"))
    if expiry is not None and expiry <= now:
        return False
    if message.get("presentedAfterEveryLogin"):
        return True
    if message.get("isRead"):
        return False
    if message.get("presentedAfterLogin") or expiry is not None:
        return True
    received = _local_time(message.get("date"))
    return received is not None and now - received <= NOTIFICATION_MAX_AGE


def notification_message(message: dict[str, Any]) -> str:
    """Return the notification text: the message, then its category and date.

    Only the message's own words are Polish; what is added carries none, so it
    reads the same in every language Home Assistant is set to.  An attachment
    is only downloadable when logged in, so it links to the portal's messages page.
    """
    parts = [(message.get("content") or "").strip()]
    details = [message.get("notificationCategory")]
    received = _local_time(message.get("date"))
    if received is not None:
        details.append(received.strftime("%Y-%m-%d %H:%M"))
    if message.get("ppeCodes"):
        details.append(f"PPE {message['ppeCodes']}")
    parts.append("*" + " · ".join(detail for detail in details if detail) + "*")
    if message.get("hasFile"):
        parts.append(
            f"📎 [{message.get('fileName') or 'Portal Odbiorcy Enea'}]({URL_NOTIFICATIONS_PAGE})"
        )
    return "\n\n".join(part for part in parts if part.strip("*"))


def storage_key(username: str) -> str:
    """Return the key of an account's store; the login itself is an e-mail address."""
    digest = hashlib.sha256(username.casefold().encode()).hexdigest()[:16]
    return f"{NOTIFICATIONS_STORAGE_KEY}.{digest}"


class EneaNotifications:
    """Turns the new portal messages of one account into persistent notifications.

    The ids of the messages seen, the last day checked and the meters checked
    are stored per account, so a notification the user dismissed does not
    come back after a restart, no message is missed while Home Assistant was
    off and a meter added later gets its whole history checked.
    """

    def __init__(self, hass: HomeAssistant, client: EneaApiClient, username: str) -> None:
        """Prepare the store; it is read on the first update."""
        self._hass = hass
        self._client = client
        self._username = username
        self._store: Store[dict[str, Any]] = Store(
            hass, NOTIFICATIONS_STORAGE_VERSION, storage_key(username)
        )
        self._lock = asyncio.Lock()
        self._loaded = False
        self._seen: set[int] = set()
        self._meters: set[int] = set()
        self._last_check: datetime | None = None
        self.checked_until: date | None = None
        # The last check's failure, only logged — kept for the diagnostics report.
        self.error: str | None = None

    @property
    def seen(self) -> int:
        """Return how many messages have been seen, for the diagnostics report."""
        return len(self._seen)

    def _meter_ids(self) -> list[int]:
        """Return the account's meters whose entries have the messages on."""
        return sorted(
            entry.data[CONF_METER_ID]
            for entry in self._hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_USERNAME) == self._username
            and entry.disabled_by is None
            and entry.options.get(CONF_NOTIFICATIONS, True)
        )

    async def async_update(self) -> None:
        """Fetch the messages since the last check and notify about the new ones.

        Meters of the account refreshing together share one request: a check
        within NOTIFICATIONS_MIN_INTERVAL of the last one covering the same
        meters is skipped.  A failure is logged and kept, never raised — the
        messages must not fail a refresh.
        """
        async with self._lock:
            meter_ids = self._meter_ids()
            now = dt_util.now()
            if (
                not meter_ids
                or self._last_check is not None
                and now - self._last_check < NOTIFICATIONS_MIN_INTERVAL
                and self._meters.issuperset(meter_ids)
            ):
                return
            try:
                await self._async_check(meter_ids, now)
            except Exception as err:
                self.error = mask_ppe(f"{type(err).__name__}: {err}")
                _LOGGER.warning("Failed to check the Portal Odbiorcy Enea messages: %s", err)
            else:
                self.error = None
                self._last_check = now

    async def _async_check(self, meter_ids: list[int], now: datetime) -> None:
        """Fetch the messages of the meters and create the notifications."""
        if not self._loaded:
            stored = await self._store.async_load() or {}
            self._seen = set(stored.get("seen", ()))
            self._meters = set(stored.get("meters", ()))
            checked = stored.get("checked_until")
            self.checked_until = date.fromisoformat(checked) if checked else None
            self._loaded = True

        today = now.date()
        if self.checked_until is None or not self._meters.issuperset(meter_ids):
            # First check, or a meter never checked before: its whole history.
            messages = await self._client.get_notifications(meter_ids)
        else:
            # The last day checked again: messages received later that day.
            messages = await self._client.get_notifications(
                meter_ids, min(self.checked_until, today), today
            )

        new = [
            message for message in messages
            if message.get("id") is not None and message["id"] not in self._seen
        ]
        for message in new:
            if should_show(message, now):
                title = message.get("title")
                persistent_notification.async_create(
                    self._hass,
                    notification_message(message),
                    title=f"Portal Odbiorcy Enea: {title}" if title else "Portal Odbiorcy Enea",
                    notification_id=NOTIFICATION_ID.format(id=message["id"]),
                )
        self._seen.update(message["id"] for message in new)
        if new or self.checked_until != today or self._meters != set(meter_ids):
            self.checked_until = today
            self._meters = set(meter_ids)
            await self._store.async_save(
                {
                    "checked_until": today.isoformat(),
                    "meters": sorted(self._meters),
                    "seen": sorted(self._seen),
                }
            )
