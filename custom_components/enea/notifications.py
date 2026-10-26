"""Messages of the Portal Odbiorcy Enea shown as Home Assistant notifications.

The portal's "Wiadomości" carry notices such as planned maintenance of the
portal itself.  They are checked per account: one request covers every meter
of the account with the option on, plus the messages to every customer.  The
first check fetches every message there is; later ones only the days since
the last check, which the portal answers much faster.  A message becomes a
persistent notification once, when it is first seen, and only while the
portal would still present it — see should_show.  It stays until dismissed,
a restart included; dismissing it marks the message read in the portal.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
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

    Kept in the account's store:

    * the ids of the messages seen, so a message is notified about once;
    * the last day checked, so no message is missed while Home Assistant was off;
    * the meters checked, so a meter added later gets its whole history checked;
    * the notifications shown and not yet dismissed — Home Assistant keeps its
      notifications in memory only, so these are shown again after a restart;
    * the messages dismissed whose "read" the portal has not taken yet.

    Dismissing a notification in Home Assistant marks its message read in the
    portal, as closing the message's window there does.
    """

    def __init__(self, hass: HomeAssistant, client: EneaApiClient, username: str) -> None:
        """Prepare the store, read on the first update, and watch for dismissals."""
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
        # Notifications on screen, by message id: their title and text.
        self._shown: dict[int, dict[str, str]] = {}
        self._read_to_send: set[int] = set()
        self._last_check: datetime | None = None
        self.checked_until: date | None = None
        # The last check's failure, only logged — kept for the diagnostics report.
        self.error: str | None = None
        self._unsubscribe = persistent_notification.async_register_callback(
            hass, self._async_notifications_changed
        )

    def unsubscribe(self) -> None:
        """Stop watching for dismissals; the account's last meter was unloaded."""
        self._unsubscribe()

    def diagnostics(self) -> dict[str, Any]:
        """Return the state worth a diagnostics report — counts, no message text."""
        return {
            "checked_until": self.checked_until.isoformat() if self.checked_until else None,
            "seen": len(self._seen),
            "shown": len(self._shown),
            "read_to_send": len(self._read_to_send),
            "error": self.error,
        }

    def _meter_ids(self) -> list[int]:
        """Return the account's meters whose entries have the messages on."""
        return sorted(
            entry.data[CONF_METER_ID]
            for entry in self._hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_USERNAME) == self._username
            and entry.disabled_by is None
            and entry.options.get(CONF_NOTIFICATIONS, True)
        )

    def _show(self, message_id: int, title: str, text: str) -> None:
        """Create the notification of a message; the same id replaces, never doubles."""
        persistent_notification.async_create(
            self._hass, text, title=title, notification_id=NOTIFICATION_ID.format(id=message_id)
        )

    @callback
    def _async_notifications_changed(
        self, update_type: persistent_notification.UpdateType, notifications: dict[str, Any]
    ) -> None:
        """Mark the messages of dismissed notifications read in the portal."""
        if update_type is not persistent_notification.UpdateType.REMOVED:
            return
        dismissed = [
            message_id for message_id in self._shown
            if NOTIFICATION_ID.format(id=message_id) in notifications
        ]
        if not dismissed:
            return
        for message_id in dismissed:
            del self._shown[message_id]
            self._read_to_send.add(message_id)
        self._hass.async_create_task(self._async_send_read(), name="enea_notifications_read")

    async def _async_send_read(self) -> None:
        """Send the dismissed messages' "read" to the portal and store what is left."""
        async with self._lock:
            await self._async_mark_read()
            await self._async_save()

    async def _async_mark_read(self) -> None:
        """Tell the portal which dismissed messages were read; a failure waits for the next check."""
        for message_id in sorted(self._read_to_send):
            try:
                await self._client.mark_notification_read(message_id)
            except Exception as err:  # noqa: BLE001 — retried before the next check
                _LOGGER.warning(
                    "Failed to mark a Portal Odbiorcy Enea message read, will retry: %s", err
                )
                return
            self._read_to_send.discard(message_id)

    async def _async_save(self) -> None:
        """Write the account's state to its store."""
        await self._store.async_save(
            {
                "checked_until": self.checked_until.isoformat() if self.checked_until else None,
                "meters": sorted(self._meters),
                "seen": sorted(self._seen),
                "shown": {str(message_id): shown for message_id, shown in self._shown.items()},
                "read_to_send": sorted(self._read_to_send),
            }
        )

    async def _async_load(self) -> None:
        """Read the store and show again the notifications a restart took away."""
        stored = await self._store.async_load() or {}
        self._seen = set(stored.get("seen", ()))
        self._meters = set(stored.get("meters", ()))
        self._read_to_send = set(stored.get("read_to_send", ()))
        self._shown = {int(key): value for key, value in (stored.get("shown") or {}).items()}
        checked = stored.get("checked_until")
        self.checked_until = date.fromisoformat(checked) if checked else None
        for message_id, shown in self._shown.items():
            self._show(message_id, shown["title"], shown["message"])
        self._loaded = True

    async def async_update(self) -> None:
        """Fetch the messages since the last check and notify about the new ones.

        Meters of the account refreshing together share one request: a check
        within NOTIFICATIONS_MIN_INTERVAL of the last one covering the same
        meters is skipped.  A failure is logged and kept, never raised — the
        messages must not fail a refresh.
        """
        async with self._lock:
            if not self._loaded:
                await self._async_load()
            meter_ids = self._meter_ids()
            now = dt_util.now()
            if (
                not meter_ids
                or (self._last_check is not None
                and now - self._last_check < NOTIFICATIONS_MIN_INTERVAL
                and self._meters.issuperset(meter_ids))
            ):
                return
            try:
                await self._async_mark_read()
                await self._async_check(meter_ids, now)
            except Exception as err:  # noqa: BLE001 — messages must not break the refresh
                self.error = mask_ppe(f"{type(err).__name__}: {err}")
                _LOGGER.warning("Failed to check the Portal Odbiorcy Enea messages: %s", err)
            else:
                self.error = None
                self._last_check = now
            await self._async_save()

    async def _async_check(self, meter_ids: list[int], now: datetime) -> None:
        """Fetch the messages of the meters and create the notifications."""
        today = now.date()
        if self.checked_until is None or not self._meters.issuperset(meter_ids):
            # First check, or a meter never checked before: its whole history.
            messages = await self._client.get_notifications(meter_ids)
        else:
            # The last day checked again: messages received later that day.
            messages = await self._client.get_notifications(
                meter_ids, min(self.checked_until, today), today
            )

        for message in messages:
            message_id = message.get("id")
            if message_id is None or message_id in self._seen:
                continue
            self._seen.add(message_id)
            if should_show(message, now):
                title = message.get("title")
                shown = {
                    "title": f"Portal Odbiorcy Enea: {title}" if title else "Portal Odbiorcy Enea",
                    "message": notification_message(message),
                }
                self._shown[message_id] = shown
                self._show(message_id, shown["title"], shown["message"])
        self.checked_until = today
        self._meters = set(meter_ids)
