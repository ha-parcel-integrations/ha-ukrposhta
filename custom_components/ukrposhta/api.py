"""Ukrposhta status-tracking API client.

The coordinator relies on this contract:

* ``async_get_parcel`` returns ``{"trackingNumber": code, "events": [...]}``
  (oldest first, events exactly as the carrier sent them) on success,
* returns ``None`` when the carrier has no events for the code (a normal,
  expected state — never an error),
* raises :class:`UkrposhtaRejectedError` when the carrier refuses the
  request outright, and :class:`UkrposhtaApiError`
  for anything else, with ``status_code`` set on a non-2xx response and
  ``retry_after`` set when a 429's ``Retry-After`` could be parsed as seconds,
* lets ``aiohttp.ClientError`` propagate untouched.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import aiohttp

from .const import TRACKING_API_URL

CARRIER_BEARER = "c3e02b53-3b1d-386e-b676-141ffa054c57"


class UkrposhtaApiError(Exception):
    """Raised when an Ukrposhta API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Ukrposhta API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class UkrposhtaRejectedError(UkrposhtaApiError):
    """Raised when the carrier refuses the request."""


def _validate_events(payload: Any) -> list[dict[str, Any]]:
    """Check the body is a list of event objects and return it unchanged."""
    if not isinstance(payload, list):
        raise UkrposhtaApiError("unexpected body (not a JSON list)")
    for item in payload:
        if not isinstance(item, dict):
            raise UkrposhtaApiError("malformed event list (entry is not an object)")
    return payload


class UkrposhtaApiClient:
    """Client for the Ukrposhta status-tracking endpoint."""

    def __init__(
        self, session: aiohttp.ClientSession, bearer: str | None = None
    ) -> None:
        """Initialise the client; the request header defaults to the constant."""
        self._session = session
        self._bearer = CARRIER_BEARER if bearer is None else bearer

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's event list.

        Returns ``None`` when the endpoint reports no events for the code.
        """
        if not self._bearer:
            raise UkrposhtaRejectedError("the request cannot be made")
        url = TRACKING_API_URL.format(tracking_code=quote(tracking_code, safe=""))
        headers = {"Authorization": f"Bearer {self._bearer}"}
        async with self._session.get(url, headers=headers) as response:
            if response.status in (401, 403):
                raise UkrposhtaRejectedError(
                    f"HTTP {response.status}", status_code=response.status
                )
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = (
                        float(retry_after_header) if retry_after_header else None
                    )
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; the caller's own backoff applies
                raise UkrposhtaApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise UkrposhtaApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                # content_type=None: the gateway may serve JSON with a generic
                # content type, which aiohttp would otherwise refuse to parse.
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise UkrposhtaApiError("unparseable body") from err

        events = _validate_events(payload)
        if not events:
            return None
        return {"trackingNumber": tracking_code, "events": events}
