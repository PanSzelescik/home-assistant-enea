"""HTTP session expiry retries once and failures release response resources."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from yarl import URL

from custom_components.enea.connector import EneaApiClient, EneaApiError, EneaAuthError
from custom_components.enea.const import URL_LOGIN, URL_PPE_DASHBOARD


def _response(status, data=None):
    """Provide the response methods used by the actual request implementation."""
    return SimpleNamespace(
        status=status, url=URL(URL_PPE_DASHBOARD.format(meter_id=12345)),
        read=AsyncMock(return_value=b"{}"), json=AsyncMock(return_value=data), release=Mock(),
    )


@pytest.mark.parametrize("expired_status", [401, 403])
@pytest.mark.parametrize("retry_status", [200, 401, 503], ids=["success", "still-expired", "server-error"])
async def test_expired_session_reauthenticates_and_retries_only_once(expired_status, retry_status):
    """A failed retry propagates instead of looping, and every response is released."""
    expired, login, retried = _response(expired_status), _response(200), _response(retry_status, {"tariffGroupName": "G12"})
    session = SimpleNamespace(get=AsyncMock(side_effect=[expired, retried]), post=AsyncMock(return_value=login))
    client = EneaApiClient(session, "test@example.invalid", "test-password")
    client._authenticated = True
    client._meters_cache = [{"id": 12345}]

    if retry_status == 200:
        assert await client.get_ppe_dashboard(12345) == {"tariffGroupName": "G12"}
    else:
        with pytest.raises(EneaApiError, match=f"dashboard endpoint: {retry_status}"):
            await client.get_ppe_dashboard(12345)

    assert [call.args for call in session.get.await_args_list] == [(URL_PPE_DASHBOARD.format(meter_id=12345),)] * 2
    session.post.assert_awaited_once_with(URL_LOGIN, json={"username": "test@example.invalid", "password": "test-password"})
    for response in (expired, login, retried):
        response.release.assert_called_once_with()
    assert client._meters_cache is None


@pytest.mark.parametrize("status,error", [(401, EneaAuthError), (500, EneaApiError)], ids=["bad-credentials", "server-error"])
async def test_failed_initial_login_never_fetches_dashboard(status, error):
    """Login failures release the response and do not issue an authenticated request."""
    response = _response(status)
    session = SimpleNamespace(post=AsyncMock(return_value=response), get=AsyncMock())
    client = EneaApiClient(session, "test@example.invalid", "wrong")

    with pytest.raises(error):
        await client.get_ppe_dashboard(12345)

    session.get.assert_not_awaited()
    response.release.assert_called_once_with()
    assert client._authenticated is False


async def test_timeout_is_api_error_without_reauthentication():
    """A network timeout is not session expiry and must not trigger another login."""
    session = SimpleNamespace(get=AsyncMock(side_effect=asyncio.TimeoutError), post=AsyncMock())
    client = EneaApiClient(session, "test@example.invalid", "test")
    client._authenticated = True

    with pytest.raises(EneaApiError, match="TimeoutError"):
        await client.get_ppe_dashboard(12345)

    session.get.assert_awaited_once()
    session.post.assert_not_awaited()


async def test_first_request_authenticates_and_reuses_session():
    """A fresh client logs in before GET and reuses its session for the next request."""
    login = _response(200)
    responses = [_response(200, {"tariffGroupName": tariff}) for tariff in ("G11", "G12")]
    session = SimpleNamespace(post=AsyncMock(return_value=login), get=AsyncMock(side_effect=responses))
    client = EneaApiClient(session, "test@example.invalid", "test-password")

    assert await client.get_ppe_dashboard(12345) == {"tariffGroupName": "G11"}
    assert await client.get_ppe_dashboard(12345) == {"tariffGroupName": "G12"}

    session.post.assert_awaited_once_with(URL_LOGIN, json={"username": "test@example.invalid", "password": "test-password"})
    assert session.get.await_count == 2
    for response in [login, *responses]:
        response.release.assert_called_once_with()
