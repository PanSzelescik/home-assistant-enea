"""The Portal Odbiorcy Enea's messages turned into persistent notifications.

The fixture account has one message, read in the portal long ago — the
"Prace serwisowe" notice.  The tests add unread ones around it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
    async_dismiss,
)
from homeassistant.config_entries import ConfigEntryState

from custom_components.enea.connector import EneaApiError
from custom_components.enea.const import URL_NOTIFICATIONS_PAGE
from custom_components.enea.notifications import notification_message, should_show
from conftest import load_portal_fixture
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_METER_ID,
    FAKE_PPE,
    NOW,
    SAVED_ON,
    async_start_hass,
    clock,
    database,
    hass,
    make_entry,
    portal,
)

SECOND_ID = 12346
SECOND_PPE = "590310600000005678"


def _ms(moment: datetime) -> int:
    """Return a moment as the portal's millisecond timestamp."""
    return int(moment.timestamp() * 1000)


def _message(
    message_id: int,
    received: datetime,
    *,
    read: bool = False,
    expiry: datetime | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Return a message shaped like the fixture's, received at the given moment."""
    return {
        **load_portal_fixture("notifications")[0],
        "id": message_id,
        "date": _ms(received),
        "isRead": read,
        "expiryDate": _ms(expiry) if expiry is not None else None,
        **fields,
    }


def _shown(hass) -> dict[str, Any]:
    """Return the persistent notifications by id."""
    return dict(_async_get_or_create_notifications(hass))


def _message_requests(portal) -> list[tuple[Any, ...]]:
    """Return the message requests made so far: meter id, first and last day."""
    return [request[1:] for request in portal.requests if request[0] == "notifications"]


async def _set_up(hass):
    """Add the fixture meter's entry and wait for its setup."""
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry


# --- which messages, and how they read ---------------------------------------


def test_a_message_read_in_the_portal_is_not_shown():
    """The fixture's notice was read in the portal: nothing to tell."""
    [message] = load_portal_fixture("notifications")

    assert should_show(message, NOW) is False


def test_an_unread_message_counts_until_its_expiry():
    """The expiry date decides, however long ago the message came."""
    received = NOW - timedelta(days=90)

    assert should_show(_message(1, received, expiry=NOW + timedelta(hours=1)), NOW) is True
    assert should_show(_message(1, received, expiry=NOW - timedelta(minutes=1)), NOW) is False


def test_an_unread_message_without_expiry_counts_for_a_month():
    """Unread messages from long ago are history, not news."""
    assert should_show(_message(1, NOW - timedelta(days=29)), NOW) is True
    assert should_show(_message(1, NOW - timedelta(days=31)), NOW) is False


def test_the_notification_text_has_the_message_its_category_and_date():
    """The words are the portal's; the date is local, as the portal shows it."""
    [message] = load_portal_fixture("notifications")

    text = notification_message(message)

    assert text == (
        "Szanowni Państwo, informujemy, że dziś w godzinach 16:30-18:00 będą prowadzone prace "
        "serwisowe. W tym czasie dostęp do Portalu Odbiorcy będzie niemożliwy. Przepraszamy za "
        "utrudnienia i dziękujemy za wyrozumiałość.\n\n"
        "*Powiadomienie o niedostepności Portalu Odbiorcy - prace serwisowe · 2026-06-16 12:13*"
    )


def test_the_notification_names_the_meter_and_points_to_an_attachment():
    """A message about one meter says which; its attachment is in the portal."""
    message = _message(
        1, NOW, ppeCodes=FAKE_PPE, hasFile=True, fileName="harmonogram.pdf"
    )

    text = notification_message(message)

    assert f" · PPE {FAKE_PPE}*" in text
    assert text.endswith(f"📎 [harmonogram.pdf]({URL_NOTIFICATIONS_PAGE})")


# --- in a running Home Assistant -------------------------------------------------


async def test_first_check_fetches_every_message_and_shows_the_current_ones(hass, portal):
    """The whole history is asked for once; only unread, current messages are shown."""
    portal.notifications += [
        _message(3007, NOW - timedelta(hours=2), title="Przerwa w dostawie energii"),
        _message(3008, NOW - timedelta(days=400), title="Stara wiadomość"),
        _message(3009, NOW - timedelta(hours=1), read=True, title="Przeczytana"),
    ]

    await _set_up(hass)

    assert _message_requests(portal) == [(FAKE_METER_ID, None, None)]
    shown = _shown(hass)
    assert list(shown) == ["enea_notification_3007"]
    assert shown["enea_notification_3007"]["title"] == "Portal Odbiorcy Enea: Przerwa w dostawie energii"


async def test_later_checks_ask_for_the_days_since_the_last(hass, clock, portal):
    """After the first check only the last day checked and the days after it are asked for."""
    entry = await _set_up(hass)
    coordinator = entry.runtime_data.coordinator
    portal.requests.clear()

    clock.now = NOW + timedelta(days=2)
    portal.notifications.append(_message(3010, clock.now - timedelta(hours=1), title="Nowa"))
    await coordinator.async_refresh()

    assert _message_requests(portal) == [(FAKE_METER_ID, SAVED_ON, SAVED_ON + timedelta(days=2))]
    assert list(_shown(hass)) == ["enea_notification_3010"]

    # The same message comes again with the next check of that day; it is shown once.
    async_dismiss(hass, "enea_notification_3010")
    await coordinator.async_refresh()
    assert _shown(hass) == {}


async def test_a_dismissed_notification_stays_dismissed_after_a_restart(tmp_path, clock, portal, database):
    """The messages seen and the day checked survive a restart."""
    portal.notifications.append(_message(3011, NOW - timedelta(hours=1)))
    first = await async_start_hass(str(tmp_path))
    await first.config_entries.async_add(make_entry())
    await first.async_block_till_done()
    assert list(_shown(first)) == ["enea_notification_3011"]
    async_dismiss(first, "enea_notification_3011")
    await first.async_stop()
    portal.requests.clear()

    second = await async_start_hass(str(tmp_path))
    [entry] = second.config_entries.async_entries("enea")
    assert await second.config_entries.async_setup(entry.entry_id)
    await second.async_block_till_done()

    assert _message_requests(portal) == [(FAKE_METER_ID, SAVED_ON, SAVED_ON)]
    assert _shown(second) == {}
    await second.async_stop()


async def test_failing_messages_do_not_fail_the_refresh(hass, portal):
    """The dashboard still comes through; the next check starts over from the full history."""
    portal.fail_notifications_with = EneaApiError("Unexpected response from notifications endpoint: 500")
    portal.notifications.append(_message(3012, NOW - timedelta(hours=1)))

    entry = await _set_up(hass)

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["tariffGroupName"] == "G12"
    state = coordinator.diagnostics_state()["notifications"]
    assert state == {
        "checked_until": None,
        "seen": 0,
        "error": "EneaApiError: Unexpected response from notifications endpoint: 500",
    }

    portal.fail_notifications_with = None
    portal.requests.clear()
    await coordinator.async_refresh()

    assert _message_requests(portal) == [(FAKE_METER_ID, None, None)]
    assert list(_shown(hass)) == ["enea_notification_3012"]
    assert coordinator.diagnostics_state()["notifications"]["error"] is None


async def test_a_message_to_every_customer_is_one_notification_for_two_meters(hass, portal):
    """Both meters of an account get it; Home Assistant shows it once."""
    portal.notifications.append(_message(3013, NOW - timedelta(hours=1)))
    portal.dashboards[SECOND_ID] = {**portal.dashboard, "id": SECOND_ID, "code": SECOND_PPE, "name": SECOND_PPE}

    for entry in (make_entry(), make_entry(meter_name=SECOND_PPE, meter_id=SECOND_ID)):
        await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()

    assert {request[0] for request in _message_requests(portal)} == {FAKE_METER_ID, SECOND_ID}
    assert list(_shown(hass)) == ["enea_notification_3013"]


async def test_removing_the_entry_removes_what_it_stored(hass):
    """The messages seen go with the meter's entry."""
    entry = await _set_up(hass)
    stored = Path(hass.config.path(".storage", f"enea.notifications.{entry.entry_id}"))
    assert stored.is_file()

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not stored.exists()
