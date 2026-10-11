"""Messages of the Portal Odbiorcy Enea shown as Home Assistant notifications.

The portal's "Wiadomości" carry notices such as planned maintenance of the
portal itself.  The first check fetches every message there is; later ones
only the days since the last check, which the portal answers much faster.
A message becomes a persistent notification once, when it is first seen —
unless it was already read in the portal, has expired, or is old.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .connector import EneaApiClient
from .const import (
    NOTIFICATION_ID,
    NOTIFICATION_MAX_AGE,
    NOTIFICATIONS_STORAGE_KEY,
    NOTIFICATIONS_STORAGE_VERSION,
    URL_NOTIFICATIONS_PAGE,
)


def _local_time(timestamp_ms: int | None) -> datetime | None:
    """Return a timestamp of the portal (ms since the epoch) as local time."""
    if timestamp_ms is None:
        return None
    return dt_util.as_local(dt_util.utc_from_timestamp(timestamp_ms / 1000))


def should_show(message: dict[str, Any], now: datetime) -> bool:
    """Return True when a newly seen message is worth a notification.

    One read in the portal has been seen already.  An expiry date in the past
    means the portal no longer presents it; a message without one counts as
    current for NOTIFICATION_MAX_AGE after it was received — the first check
    fetches the whole history, unread messages from years ago included.
    """
    if message.get("isRead"):
        return False
    expiry = _local_time(message.get("expiryDate"))
    if expiry is not None:
        return expiry > now
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


class EneaNotifications:
    """Turns the meter's new portal messages into persistent notifications.

    The ids of the messages seen and the last day checked are stored per
    meter entry, so a notification the user dismissed does not come back
    after a restart and no message is missed while Home Assistant was off.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Prepare the store; it is read on the first update."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, NOTIFICATIONS_STORAGE_VERSION, f"{NOTIFICATIONS_STORAGE_KEY}.{entry_id}"
        )
        self._loaded = False
        self._seen: set[int] = set()
        self.checked_until: date | None = None

    @property
    def seen(self) -> int:
        """Return how many messages have been seen, for the diagnostics report."""
        return len(self._seen)

    async def async_update(self, client: EneaApiClient, meter_id: int) -> None:
        """Fetch the messages since the last check and notify about the new ones."""
        if not self._loaded:
            stored = await self._store.async_load() or {}
            self._seen = set(stored.get("seen", ()))
            checked = stored.get("checked_until")
            self.checked_until = date.fromisoformat(checked) if checked else None
            self._loaded = True

        now = dt_util.now()
        today = now.date()
        if self.checked_until is None:
            messages = await client.get_notifications(meter_id)
        else:
            # The last day checked again: messages received later that day.
            messages = await client.get_notifications(
                meter_id, min(self.checked_until, today), today
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
        if new or self.checked_until != today:
            self.checked_until = today
            await self._store.async_save(
                {"checked_until": today.isoformat(), "seen": sorted(self._seen)}
            )
