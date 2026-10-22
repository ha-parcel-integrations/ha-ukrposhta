"""Tests for the Ukrposhta API client."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.ukrposhta import api
from custom_components.ukrposhta.api import (
    UkrposhtaApiClient,
    UkrposhtaApiError,
    UkrposhtaRejectedError,
)

from .payloads import DELIVERED_CODE, TEST_BEARER, wire_event

CODE = DELIVERED_CODE


def _session_returning(
    status: int, body: object = None, headers: dict | None = None
) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def _client(session: MagicMock) -> UkrposhtaApiClient:
    return UkrposhtaApiClient(session, TEST_BEARER)


async def test_empty_credential_raises_without_a_request(monkeypatch):
    monkeypatch.setattr(api, "CARRIER_BEARER", "")
    session = _session_returning(200, [])
    client = UkrposhtaApiClient(session)
    with pytest.raises(UkrposhtaRejectedError):
        await client.async_get_parcel(CODE)
    session.get.assert_not_called()


async def test_request_shape_and_events_are_kept_untrimmed():
    session = _session_returning(
        200,
        [
            wire_event("10601", "2026-04-27T23:03:58", "Registered", CODE),
            wire_event("41000", "2026-04-29T13:12:42", "Delivered", CODE),
        ],
    )

    parcel = await _client(session).async_get_parcel(CODE)

    args, kwargs = session.get.call_args
    assert args[0] == (
        "https://www.ukrposhta.ua/status-tracking/0.0.1/statuses"
        f"?barcode={CODE}&lang=en"
    )
    assert kwargs["headers"] == {"Authorization": f"Bearer {TEST_BEARER}"}
    assert parcel["trackingNumber"] == CODE
    assert [e["event"] for e in parcel["events"]] == ["10601", "41000"]
    assert parcel["events"][0] == wire_event(
        "10601", "2026-04-27T23:03:58", "Registered", CODE
    )


async def test_tracking_code_is_url_encoded():
    session = _session_returning(200, [])
    await _client(session).async_get_parcel("A B&c=1")
    assert "barcode=A%20B%26c%3D1&lang=en" in session.get.call_args[0][0]


async def test_empty_event_list_is_track_not_found():
    assert await _client(_session_returning(200, [])).async_get_parcel(CODE) is None


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credential_raises_credential_error(status):
    client = _client(_session_returning(status, "<html>denied</html>"))
    with pytest.raises(UkrposhtaRejectedError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == status
    assert TEST_BEARER not in str(err.value)
    assert CODE not in str(err.value)


async def test_429_carries_numeric_retry_after():
    client = _client(_session_returning(429, None, {"Retry-After": "120"}))
    with pytest.raises(UkrposhtaApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


@pytest.mark.parametrize("header", [None, "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_429_without_numeric_retry_after(header):
    headers = {"Retry-After": header} if header else {}
    client = _client(_session_returning(429, None, headers))
    with pytest.raises(UkrposhtaApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after is None


async def test_other_status_raises():
    client = _client(_session_returning(500, {}))
    with pytest.raises(UkrposhtaApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == 500
    assert not isinstance(err.value, UkrposhtaRejectedError)


async def test_non_json_body_raises_without_echoing_it():
    client = _client(_session_returning(200, "<html>challenge</html>"))
    with pytest.raises(UkrposhtaApiError) as err:
        await client.async_get_parcel(CODE)
    assert "challenge" not in str(err.value)


@pytest.mark.parametrize("body", [{"error": "x"}, "text", 5, None])
async def test_non_list_json_raises(body):
    client = _client(_session_returning(200, body if body != "text" else {"a": 1}))
    with pytest.raises(UkrposhtaApiError):
        await client.async_get_parcel(CODE)


async def test_malformed_event_entry_raises():
    client = _client(_session_returning(200, [wire_event("1", "d", "n", CODE), "junk"]))
    with pytest.raises(UkrposhtaApiError):
        await client.async_get_parcel(CODE)


async def test_propagates_network_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await _client(session).async_get_parcel(CODE)
