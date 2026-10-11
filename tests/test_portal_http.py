"""The API client against a local HTTP server that behaves like the Portal Odbiorcy Enea.

The server answers the paths the portal has, from the saved responses, and
keeps a login session in the PER_JSESSIONID cookie, so the client goes over
real HTTP: URLs, the JSON body of the login, the cookie, status codes and
bodies the portal sends when something is wrong.
"""
from __future__ import annotations

import ssl
from datetime import date, timedelta
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.client_reqrep import ConnectionKey
from aiohttp.test_utils import TestServer

from custom_components.enea import connector
from custom_components.enea.connector import EneaApiClient, EneaApiError, EneaAuthError, _fetch
from custom_components.enea.const import DataSource, MeasurementType, Resolution
from conftest import load_portal_fixture

USERNAME = "test@example.invalid"
PASSWORD = "test-password"
METER_ID = 12345
COOKIE = "PER_JSESSIONID"


class PortalServer:
    """The portal's API paths, with a login session kept in a cookie."""

    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.requests: list[str] = []
        self.maintenance = False
        app = web.Application()
        app.router.add_post("/portalOdbiorcy/api/auth/login", self.login)
        app.router.add_get("/portalOdbiorcy/api/user/ppes", self.ppes)
        app.router.add_get("/portalOdbiorcy/api/consumptionDashboard/ppe/{meter_id}", self.dashboard)
        app.router.add_get(
            "/portalOdbiorcy/api/consumption/{meter_id}/{first}/{last}/{mtype}/{resolution}", self.range
        )
        app.router.add_get(
            "/portalOdbiorcy/api/consumption/{meter_id}/{first}/{last}/{mtype}/{resolution}/{source}",
            self.range,
        )
        app.router.add_post("/portalOdbiorcy/api/notification/", self.notifications)
        app.router.add_post("/portalOdbiorcy/api/notification/read/{notification_id}", self.read)
        # JSON bodies of the message requests, in order.
        self.notification_queries: list[Any] = []
        self.server = TestServer(app)

    def expire_sessions(self) -> None:
        """Forget every login, as the portal does after a while."""
        self.sessions.clear()

    async def login(self, request: web.Request) -> web.Response:
        """Check the credentials and start a session."""
        self.requests.append("login")
        body = await request.json()
        if body != {"username": USERNAME, "password": PASSWORD}:
            return web.json_response(
                {"code": "Wprowadzony login lub hasło jest nieprawidłowe. Spróbuj ponownie."},
                status=401,
            )
        session = f"session-{len(self.requests)}"
        self.sessions.add(session)
        response = web.json_response({})
        response.set_cookie(COOKIE, session)
        return response

    def _refuse(self, request: web.Request) -> web.Response | None:
        """Return the portal's answer to a request it will not serve, None to serve it."""
        if self.maintenance:
            return web.Response(text="<html>Przerwa techniczna</html>", content_type="text/html")
        if request.cookies.get(COOKIE) not in self.sessions:
            return web.json_response({"code": "Nie jesteś zalogowany."}, status=401)
        return None

    async def ppes(self, request: web.Request) -> web.Response:
        """Return /user/ppes."""
        self.requests.append("ppes")
        return self._refuse(request) or web.json_response(load_portal_fixture("user_ppes"))

    async def dashboard(self, request: web.Request) -> web.Response:
        """Return the dashboard of the fixture meter, 404 for any other."""
        self.requests.append(f"dashboard/{request.match_info['meter_id']}")
        if refused := self._refuse(request):
            return refused
        if request.match_info["meter_id"] != str(METER_ID):
            return web.json_response({"code": "Brak dostępu"}, status=404)
        return web.json_response(load_portal_fixture("dashboard"))

    async def range(self, request: web.Request) -> web.Response:
        """Return the saved week for its own dates, an empty response for others."""
        info = request.match_info
        self.requests.append(
            "/".join(info[key] for key in ("first", "last", "mtype", "resolution", "source") if key in info)
        )
        if refused := self._refuse(request):
            return refused
        name = f"range_{info['first']}_{info['last']}_{info['mtype']}"
        try:
            return web.json_response(load_portal_fixture(name))
        except FileNotFoundError:
            return web.json_response({"values": [], "valuesToTable": [], "zones": []})

    async def notifications(self, request: web.Request) -> web.Response:
        """Return the fixture account's messages, whatever the period asked for."""
        self.requests.append("notifications")
        if refused := self._refuse(request):
            return refused
        self.notification_queries.append(await request.json())
        return web.json_response(load_portal_fixture("notifications"))

    async def read(self, request: web.Request) -> web.Response:
        """Mark a message read; the answer has no body to speak of."""
        self.requests.append(f"read/{request.match_info['notification_id']}:{await request.text()!r}")
        return self._refuse(request) or web.Response()


@pytest.fixture
async def portal(monkeypatch: pytest.MonkeyPatch):
    """Run the server and point the client's URLs at it."""
    server = PortalServer()
    await server.server.start_server()
    base = str(server.server.make_url("/portalOdbiorcy/api"))
    monkeypatch.setattr(connector, "URL_LOGIN", f"{base}/auth/login")
    monkeypatch.setattr(connector, "URL_PPES", f"{base}/user/ppes")
    monkeypatch.setattr(connector, "URL_PPE_DASHBOARD", f"{base}/consumptionDashboard/ppe/{{meter_id}}")
    monkeypatch.setattr(connector, "URL_NOTIFICATIONS", f"{base}/notification/")
    monkeypatch.setattr(connector, "URL_NOTIFICATION_READ", f"{base}/notification/read/{{notification_id}}")
    monkeypatch.setattr(
        connector, "URL_CONSUMPTION_RANGE",
        f"{base}/consumption/{{meter_id}}/{{start_date}}/{{end_date}}/{{measurement_type}}/{{resolution}}",
    )
    monkeypatch.setattr(
        connector, "URL_CONSUMPTION_RANGE_DATA_SOURCE",
        f"{base}/consumption/{{meter_id}}/{{start_date}}/{{end_date}}/{{measurement_type}}"
        "/{resolution}/{data_source}",
    )
    yield server
    await server.server.close()


@pytest.fixture
async def session():
    """An aiohttp session that keeps cookies of an IP host, as the test server is one."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client_session:
        yield client_session


async def test_login_then_every_endpoint(portal, session):
    """One login serves the meter list, the dashboard and the range requests."""
    client = EneaApiClient(session, USERNAME, PASSWORD)

    meters = await client.get_meters()
    dashboard = await client.get_ppe_dashboard(METER_ID)
    week = await client.get_consumption_data_range(
        METER_ID, date(2026, 3, 27), date(2026, 4, 2), MeasurementType.ENERGY_CONSUMED, Resolution.MIN_60
    )

    assert [meter["id"] for meter in meters] == [METER_ID]
    assert dashboard["tariffGroupName"] == "G12"
    assert len(week["valuesToTable"]) == 7 * 24 - 1
    assert portal.requests == ["login", "ppes", f"dashboard/{METER_ID}", "2026-03-27/2026-04-02/1/2"]


async def test_balanced_data_is_asked_for_in_a_sixth_segment(portal, session):
    """A prosumer's data source goes after the resolution, as the portal's frontend sends it."""
    client = EneaApiClient(session, USERNAME, PASSWORD)

    await client.get_consumption_data_range(
        METER_ID, date(2026, 3, 27), date(2026, 4, 2), MeasurementType.ENERGY_RETURNED,
        Resolution.MIN_60, DataSource.AFTER_BALANCING,
    )

    assert portal.requests[-1] == "2026-03-27/2026-04-02/5/2/2"


async def test_messages_are_asked_for_as_the_portal_frontend_does(portal, session):
    """The meters and every customer (-1); all messages, then a range of days."""
    client = EneaApiClient(session, USERNAME, PASSWORD)

    everything = await client.get_notifications([METER_ID])
    await client.get_notifications([METER_ID, 12346], date(2026, 10, 10), date(2026, 10, 11))

    assert [message["title"] for message in everything] == ["Prace serwisowe"]
    assert portal.notification_queries == [
        {"ppes": [METER_ID, -1], "groups": None, "timeRange": 5, "timeStart": "null", "timeEnd": "null"},
        {"ppes": [METER_ID, 12346, -1], "groups": None, "timeRange": 6,
         "timeStart": "2026-10-10", "timeEnd": "2026-10-11"},
    ]


async def test_a_message_is_marked_read_with_an_empty_post(portal, session):
    """No body goes out and none is expected back, as with the portal's frontend."""
    client = EneaApiClient(session, USERNAME, PASSWORD)

    assert await client.mark_notification_read(3006) is None

    assert portal.requests == ["login", "read/3006:''"]


async def test_expired_session_posts_the_message_query_again(portal, session):
    """A POST is sent again, body and all, after the portal forgot the session."""
    client = EneaApiClient(session, USERNAME, PASSWORD)
    await client.authenticate()
    portal.expire_sessions()

    await client.get_notifications([METER_ID])

    assert portal.requests == ["login", "notifications", "login", "notifications"]
    # The refused request was not read; the one sent again carries the same body.
    assert [query["timeRange"] for query in portal.notification_queries] == [5]


async def test_expired_session_logs_in_again_once(portal, session):
    """The portal forgetting the session costs one more login, not an error."""
    client = EneaApiClient(session, USERNAME, PASSWORD)
    await client.get_ppe_dashboard(METER_ID)
    portal.expire_sessions()

    assert (await client.get_ppe_dashboard(METER_ID))["tariffGroupName"] == "G12"

    assert portal.requests == ["login", f"dashboard/{METER_ID}", f"dashboard/{METER_ID}", "login",
                               f"dashboard/{METER_ID}"]


async def test_wrong_password_is_an_authentication_error(portal, session):
    """The portal's 401 at login means bad credentials; nothing else is asked for."""
    client = EneaApiClient(session, USERNAME, "wrong")

    with pytest.raises(EneaAuthError):
        await client.get_meters()

    assert portal.requests == ["login"]


async def test_new_password_takes_effect_and_drops_the_meter_cache(portal, session):
    """The meter list is cached between coordinators, until the credentials change."""
    client = EneaApiClient(session, USERNAME, PASSWORD)
    await client.get_meters()
    await client.get_meters()
    assert portal.requests.count("ppes") == 1

    client.update_credentials(PASSWORD)
    await client.get_meters()
    assert portal.requests.count("ppes") == 1  # the same password changes nothing

    client.update_credentials("wrong")
    with pytest.raises(EneaAuthError):
        await client.get_meters()
    assert portal.requests[-1] == "login"


async def test_maintenance_page_is_an_api_error_without_the_meter_id(portal, session):
    """An HTML page served with 200 instead of JSON fails cleanly."""
    client = EneaApiClient(session, USERNAME, PASSWORD)
    await client.authenticate()
    portal.maintenance = True

    with pytest.raises(EneaApiError) as exc_info:
        await client.get_ppe_dashboard(METER_ID)

    assert "ContentTypeError" in str(exc_info.value)
    assert str(METER_ID) not in str(exc_info.value)


async def test_unexpected_status_is_an_api_error(portal, session):
    """A dashboard the portal refuses (404) is an API error naming the endpoint."""
    client = EneaApiClient(session, USERNAME, PASSWORD)

    with pytest.raises(EneaApiError, match="dashboard endpoint: 404"):
        await client.get_ppe_dashboard(99999)


async def test_unreachable_portal_is_an_api_error(portal, session):
    """A closed port is a connection error, reported without the URL's meter id."""
    client = EneaApiClient(session, USERNAME, PASSWORD)
    await portal.server.close()

    with pytest.raises(EneaApiError, match="Cannot connect to Portal Odbiorcy Enea"):
        await client.get_meters()


async def test_closed_session_is_reported(session):
    """The shared client is replaced once its session has been closed."""
    client = EneaApiClient(session, USERNAME, PASSWORD)
    assert client.session_closed is False

    await session.close()

    assert client.session_closed is True


async def test_reversed_range_is_refused_before_any_request(portal, session):
    """A range ending before it starts is a programming error, not a request."""
    client = EneaApiClient(session, USERNAME, PASSWORD)

    with pytest.raises(ValueError, match="must be <="):
        await client.get_consumption_data_range(
            METER_ID, date(2026, 4, 2), date(2026, 4, 2) - timedelta(days=1),
            MeasurementType.ENERGY_CONSUMED, Resolution.MIN_60,
        )

    assert portal.requests == []


def _ssl_error(cls: type[Exception]) -> Exception:
    """Return the TLS error aiohttp raises when the portal's certificate fails."""
    key = ConnectionKey("portalodbiorcy.operator.enea.pl", 443, True, True, None, None, None)
    if cls is aiohttp.ClientConnectorCertificateError:
        return cls(key, ssl.SSLCertVerificationError("certificate has expired"))
    return cls(key, ssl.SSLError("handshake failure"))


@pytest.mark.parametrize("cls,message", [
    pytest.param(aiohttp.ClientConnectorCertificateError, "certificate may have expired", id="certificate"),
    pytest.param(aiohttp.ClientConnectorSSLError, "SSL error connecting", id="handshake"),
])
async def test_tls_failures_say_what_went_wrong(cls, message):
    """An expired certificate and a failed handshake are told apart in the error."""

    async def request() -> Any:
        raise _ssl_error(cls)

    with pytest.raises(EneaApiError, match=message):
        async with _fetch(request()):
            pass
