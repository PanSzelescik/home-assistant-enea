"""The Portal Odbiorcy Enea's messages turned into persistent notifications.

The fixture account has one message, read in the portal long ago — the
"Prace serwisowe" notice.  The tests add others around it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
    async_dismiss,
)
from homeassistant.config_entries import ConfigEntryState

from custom_components.enea.connector import EneaApiError
from custom_components.enea.const import URL_NOTIFICATIONS_PAGE
from custom_components.enea.notifications import notification_message, should_show, storage_key
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

USERNAME = "test@example.invalid"
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
    """Return the message requests made so far: meter ids, first and last day."""
    return [request[1:] for request in portal.requests if request[0] == "notifications"]


def _stored(hass) -> Path:
    """Return the file the account's messages seen are stored in."""
    return Path(hass.config.path(".storage", storage_key(USERNAME)))


def _add_second_meter(portal) -> None:
    """Add a second meter to the fixture account."""
    portal.dashboards[SECOND_ID] = {
        **portal.dashboard, "id": SECOND_ID, "code": SECOND_PPE, "name": SECOND_PPE
    }


async def _set_up(hass, **options):
    """Add the fixture meter's entry and wait for its setup."""
    entry = make_entry(**options)
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


def test_a_message_presented_after_every_login_counts_even_when_read():
    """The portal shows it after every login, read or not — until it expires."""
    received = NOW - timedelta(days=400)

    assert should_show(_message(1, received, read=True, presentedAfterEveryLogin=True), NOW) is True
    assert should_show(
        _message(1, received, read=True, expiry=NOW - timedelta(days=1), presentedAfterEveryLogin=True),
        NOW,
    ) is False


def test_a_message_presented_after_login_counts_until_read():
    """The portal shows it after the next login while unread, however old."""
    received = NOW - timedelta(days=400)

    assert should_show(_message(1, received, presentedAfterLogin=True), NOW) is True
    assert should_show(_message(1, received, read=True, presentedAfterLogin=True), NOW) is False


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
    message = _message(1, NOW, ppeCodes=FAKE_PPE, hasFile=True, fileName="harmonogram.pdf")

    text = notification_message(message)

    assert f" · PPE {FAKE_PPE}*" in text
    assert text.endswith(f"📎 [harmonogram.pdf]({URL_NOTIFICATIONS_PAGE})")


def test_the_store_is_named_without_the_login():
    """The login is an e-mail address; the store's file name does not give it away."""
    key = storage_key(USERNAME)

    assert key.startswith("enea.notifications.")
    assert "example" not in key
    assert key == storage_key(USERNAME.upper())  # the portal ignores the case of the login


# --- in a running Home Assistant -------------------------------------------------


async def test_first_check_fetches_every_message_and_shows_the_current_ones(hass, portal):
    """The whole history is asked for once; only unread, current messages are shown."""
    portal.notifications += [
        _message(3007, NOW - timedelta(hours=2), title="Przerwa w dostawie energii"),
        _message(3008, NOW - timedelta(days=400), title="Stara wiadomość"),
        _message(3009, NOW - timedelta(hours=1), read=True, title="Przeczytana"),
    ]

    await _set_up(hass)

    assert _message_requests(portal) == [((FAKE_METER_ID,), None, None)]
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

    assert _message_requests(portal) == [((FAKE_METER_ID,), SAVED_ON, SAVED_ON + timedelta(days=2))]
    assert list(_shown(hass)) == ["enea_notification_3010"]

    # The same message comes again with the next check of that day; it is shown once.
    async_dismiss(hass, "enea_notification_3010")
    clock.now += timedelta(hours=1)
    await coordinator.async_refresh()
    assert len(_message_requests(portal)) == 2
    assert _shown(hass) == {}


async def test_a_refresh_soon_after_a_check_does_not_ask_again(hass, clock, portal):
    """An action-triggered refresh minutes after the last check sends no message request."""
    entry = await _set_up(hass)
    portal.requests.clear()

    clock.now = NOW + timedelta(minutes=5)
    await entry.runtime_data.coordinator.async_refresh()

    assert ("dashboard", FAKE_METER_ID) in portal.requests
    assert _message_requests(portal) == []


async def test_a_dismissed_notification_stays_dismissed_after_a_restart(tmp_path, clock, portal, database):
    """The messages seen and the day checked survive a restart; nothing is marked read twice."""
    portal.notifications.append(_message(3011, NOW - timedelta(hours=1)))
    first = await async_start_hass(str(tmp_path))
    await first.config_entries.async_add(make_entry())
    await first.async_block_till_done()
    assert list(_shown(first)) == ["enea_notification_3011"]
    async_dismiss(first, "enea_notification_3011")
    await first.async_block_till_done()
    await first.async_stop()
    portal.requests.clear()

    second = await async_start_hass(str(tmp_path))
    [entry] = second.config_entries.async_entries("enea")
    assert await second.config_entries.async_setup(entry.entry_id)
    await second.async_block_till_done()

    assert _message_requests(portal) == [((FAKE_METER_ID,), SAVED_ON, SAVED_ON)]
    assert _shown(second) == {}
    assert ("read", 3011) not in portal.requests
    await second.async_stop()


async def test_failing_messages_do_not_fail_the_refresh(hass, portal):
    """The dashboard still comes through; the next refresh tries again, from the full history."""
    portal.fail_notifications_with = EneaApiError("Unexpected response from notifications endpoint: 500")
    portal.notifications.append(_message(3012, NOW - timedelta(hours=1)))

    entry = await _set_up(hass)

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["tariffGroupName"] == "G12"
    assert coordinator.diagnostics_state()["notifications"] == {
        "checked_until": None,
        "seen": 0,
        "shown": 0,
        "read_to_send": 0,
        "error": "EneaApiError: Unexpected response from notifications endpoint: 500",
    }

    portal.fail_notifications_with = None
    portal.requests.clear()
    await coordinator.async_refresh()

    assert _message_requests(portal) == [((FAKE_METER_ID,), None, None)]
    assert list(_shown(hass)) == ["enea_notification_3012"]
    assert coordinator.diagnostics_state()["notifications"]["error"] is None


async def test_the_option_turns_the_messages_off(hass, portal):
    """With the option off nothing is asked for and the report says so."""
    portal.notifications.append(_message(3013, NOW - timedelta(hours=1)))

    entry = await _set_up(hass, notifications=False)

    assert _message_requests(portal) == []
    assert _shown(hass) == {}
    assert entry.runtime_data.coordinator.diagnostics_state()["notifications"] is None


async def test_two_meters_of_an_account_share_one_request(tmp_path, clock, portal, database):
    """After a restart the first meter asks for both; the second's refresh asks for nothing."""
    _add_second_meter(portal)
    portal.notifications.append(_message(3014, NOW - timedelta(hours=1), ppeCodes=SECOND_PPE))
    first = await async_start_hass(str(tmp_path))
    for entry in (make_entry(), make_entry(meter_name=SECOND_PPE, meter_id=SECOND_ID)):
        await first.config_entries.async_add(entry)
    await first.async_block_till_done()
    await first.async_stop()
    portal.requests.clear()

    second = await async_start_hass(str(tmp_path))
    # Setting the integration up sets up both entries at once, as a start does.
    entries = second.config_entries.async_entries("enea")
    assert await second.config_entries.async_setup(entries[0].entry_id)
    await second.async_block_till_done()

    assert all(entry.state is ConfigEntryState.LOADED for entry in entries)
    assert _message_requests(portal) == [((FAKE_METER_ID, SECOND_ID), SAVED_ON, SAVED_ON)]
    await second.async_stop()


async def test_a_meter_added_later_gets_its_history_checked(hass, portal):
    """The account's new meter has never been asked about: the whole history again, for both."""
    _add_second_meter(portal)
    general = _message(3015, NOW - timedelta(hours=1))
    portal.notifications.append(general)
    await _set_up(hass)
    assert list(_shown(hass)) == ["enea_notification_3015"]
    async_dismiss(hass, "enea_notification_3015")

    # The portal answers for the second meter too: its own message and the general one.
    portal.notifications.append(_message(3016, NOW - timedelta(hours=2), ppeCodes=SECOND_PPE))
    await hass.config_entries.async_add(make_entry(meter_name=SECOND_PPE, meter_id=SECOND_ID))
    await hass.async_block_till_done()

    assert _message_requests(portal)[-1] == ((FAKE_METER_ID, SECOND_ID), None, None)
    # The general message is not shown again — the account has seen it.
    assert list(_shown(hass)) == ["enea_notification_3016"]


async def test_a_meter_with_the_option_off_is_left_out_of_the_request(hass, portal):
    """Only the meters whose option is on are asked about."""
    _add_second_meter(portal)

    await _set_up(hass, notifications=False)
    await hass.config_entries.async_add(make_entry(meter_name=SECOND_PPE, meter_id=SECOND_ID))
    await hass.async_block_till_done()

    assert _message_requests(portal) == [((SECOND_ID,), None, None)]


async def test_the_store_goes_with_the_last_meter_of_the_account(hass, portal):
    """The messages seen belong to the account; removing one of two meters keeps them."""
    _add_second_meter(portal)
    first = await _set_up(hass)
    second = make_entry(meter_name=SECOND_PPE, meter_id=SECOND_ID)
    await hass.config_entries.async_add(second)
    await hass.async_block_till_done()
    assert _stored(hass).is_file()

    await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()
    assert _stored(hass).is_file()

    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()
    assert not _stored(hass).exists()


# --- dismissing, and what a restart keeps --------------------------------------------


async def test_dismissing_a_notification_marks_its_message_read(hass, portal):
    """Closing it in Home Assistant does what closing the message's window in the portal does."""
    portal.notifications.append(_message(3017, NOW - timedelta(hours=1)))
    await _set_up(hass)

    async_dismiss(hass, "enea_notification_3017")
    await hass.async_block_till_done()

    assert ("read", 3017) in portal.requests
    assert portal.notifications[-1]["isRead"] is True


async def test_dismissing_every_notification_marks_every_message_read(hass, portal):
    """Dismissing all in Home Assistant reaches the portal for each message; others are left alone."""
    portal.notifications += [_message(3018, NOW - timedelta(hours=1)), _message(3019, NOW)]
    await _set_up(hass)
    persistent_notification.async_create(hass, "Coś innego", notification_id="other")

    persistent_notification.async_dismiss_all(hass)
    await hass.async_block_till_done()

    assert sorted(request[1] for request in portal.requests if request[0] == "read") == [3018, 3019]


async def test_a_read_the_portal_refused_is_sent_with_the_next_check(hass, clock, portal):
    """The portal down when the notification is dismissed: the next check sends it."""
    portal.notifications.append(_message(3020, NOW - timedelta(hours=1)))
    entry = await _set_up(hass)
    portal.fail_read_with = EneaApiError("Cannot connect to Portal Odbiorcy Enea")

    async_dismiss(hass, "enea_notification_3020")
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.diagnostics_state()["notifications"]["read_to_send"] == 1
    assert portal.notifications[-1]["isRead"] is False

    portal.fail_read_with = None
    clock.now = NOW + timedelta(hours=4)
    await entry.runtime_data.coordinator.async_refresh()

    assert portal.notifications[-1]["isRead"] is True
    assert entry.runtime_data.coordinator.diagnostics_state()["notifications"]["read_to_send"] == 0


async def test_a_notification_not_dismissed_comes_back_after_a_restart(tmp_path, clock, portal, database):
    """Home Assistant forgets its notifications on a restart; the account's store does not."""
    portal.notifications.append(_message(3021, NOW - timedelta(hours=1), title="Ważne"))
    first = await async_start_hass(str(tmp_path))
    await first.config_entries.async_add(make_entry())
    await first.async_block_till_done()
    shown_before = _shown(first)["enea_notification_3021"]
    await first.async_stop()

    second = await async_start_hass(str(tmp_path))
    [entry] = second.config_entries.async_entries("enea")
    assert await second.config_entries.async_setup(entry.entry_id)
    await second.async_block_till_done()

    shown = _shown(second)
    assert list(shown) == ["enea_notification_3021"]
    assert (shown["enea_notification_3021"]["title"], shown["enea_notification_3021"]["message"]) == (
        shown_before["title"], shown_before["message"]
    )
    assert not any(request[0] == "read" for request in portal.requests)

    async_dismiss(second, "enea_notification_3021")
    await second.async_block_till_done()
    assert ("read", 3021) in portal.requests
    await second.async_stop()
