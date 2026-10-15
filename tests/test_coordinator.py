"""Tests for the Ukrposhta coordinator: fetching, caching and events.

The parcel mapping itself is covered by ``test_parcels.py``.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ukrposhta.api import UkrposhtaApiError, UkrposhtaRejectedError
from custom_components.ukrposhta.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.ukrposhta.coordinator import UkrposhtaCoordinator

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
    event,
    in_transit_events,
    sample,
)

OTHER_CODE = "EXAMPLE888888"


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


def _registered(code: str = ACTIVE_CODE) -> dict:
    return sample(in_transit_events(code)[:1], code)


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        active_sample() if code == ACTIVE_CODE else delivered_sample()
    )
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1  # one active
    assert data[0]["barcode"] == ACTIVE_CODE
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_not_found_shows_pending_placeholder(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = None  # not found
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == OTHER_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_update_keeps_cached_payload_on_error(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UkrposhtaCoordinator(hass, client, entry)
    await coordinator._async_update_data()  # populates the cache

    client.async_get_parcel.side_effect = UkrposhtaApiError("HTTP 500")
    await coordinator._async_update_data()  # error -> cached raw reused
    assert len(coordinator.delivered) == 1


async def test_update_raises_when_every_parcel_fails(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = UkrposhtaApiError("HTTP 500")
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_reraises_unexpected_exceptions(hass):
    """Only API and network errors are tolerated; a bug must not be swallowed."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = ValueError("boom")
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    with pytest.raises(ValueError):
        await coordinator._async_update_data()


async def test_update_skips_items_missing_a_tracking_code(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ""}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1  # empty item never fetched


async def test_update_backfills_missing_tracking_number(hass):
    """An edge payload without a tracking number keeps the requested code."""
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    raw = {"events": [event("20700", "2026-04-28T15:52:17", "Processed")]}
    client = AsyncMock()
    client.async_get_parcel.return_value = raw
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()
    assert data[0]["barcode"] == OTHER_CODE


async def test_update_prunes_cache_for_untracked_parcels(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UkrposhtaCoordinator(hass, client, entry)
    coordinator._raw_cache["GONE"] = {"trackingNumber": "GONE"}

    await coordinator._async_update_data()

    assert "GONE" not in coordinator._raw_cache
    assert DELIVERED_CODE in coordinator._raw_cache


async def test_delivered_code_skipped_from_fetch(hass):
    """A delivered code stops being fetched from the next cycle on."""
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = (
        lambda code: active_sample(code) if code == ACTIVE_CODE else delivered_sample()
    )
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.call_count == 2
    assert coordinator.delivered_codes == {DELIVERED_CODE}

    client.async_get_parcel.reset_mock()
    data = await coordinator._async_update_data()

    # Only the still-active code is fetched — the delivered one is skipped.
    client.async_get_parcel.assert_called_once_with(ACTIVE_CODE)
    # The delivered parcel's sensor still keeps its data, from the cache.
    assert any(parcel["barcode"] == DELIVERED_CODE for parcel in coordinator.delivered)
    assert data[0]["barcode"] == ACTIVE_CODE


async def test_delivered_code_forgotten_when_untracked(hass):
    """Untracking a delivered code drops it from the skip set too."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert coordinator.delivered_codes == {DELIVERED_CODE}

    hass.config_entries.async_update_entry(entry, options={CONF_PARCELS: []})
    await coordinator._async_update_data()
    assert coordinator.delivered_codes == set()


async def test_update_fetches_parcels_one_at_a_time_with_pacing(hass, monkeypatch):
    import asyncio

    monkeypatch.setattr(
        "custom_components.ukrposhta.coordinator.REQUEST_PACING_SECONDS", 3
    )
    sleeps = []

    async def _record(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record)
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: OTHER_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: active_sample(code)
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert [c.args[0] for c in client.async_get_parcel.call_args_list] == [
        ACTIVE_CODE,
        OTHER_CODE,
    ]
    assert sleeps == [3]


async def test_cache_only_poll_does_not_stamp_last_success(hass):
    """A poll served entirely from cache must not look like a success."""
    # Must still be active (not delivered) — a delivered code is skipped from
    # the fetch entirely from the next cycle on, which is covered separately
    # by test_delivered_code_skipped_from_fetch.
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    coordinator = UkrposhtaCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    assert stamp is not None

    client.async_get_parcel.side_effect = UkrposhtaApiError("HTTP 500")
    await coordinator._async_update_data()  # served from cache
    assert coordinator.last_success_time == stamp


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    """Otherwise every restart floods the user with "registered" events."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = AsyncMock()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _registered()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_fires_status_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _registered()
    await coordinator._async_update_data()  # first refresh: suppressed

    client.async_get_parcel.return_value = active_sample()  # in transit
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.REGISTERED
    assert events[0].data["new_status"] == ParcelStatus.IN_TRANSIT


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    """The hop to delivered fires exactly one, dedicated event."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = delivered_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == ACTIVE_CODE
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when first tracked fires nothing at all."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        active_sample(code) if code == ACTIVE_CODE else delivered_sample(code)
    )
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    await coordinator._async_update_data()  # first refresh seeds the state

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: DELIVERED_CODE},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: active_sample(code)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == OTHER_CODE


def _with_window(parcel_from, parcel_to):
    from custom_components.ukrposhta.parcels import normalize_parcel as real

    def _normalize(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["planned_from"] = parcel_from
        parcel["planned_to"] = parcel_to
        return parcel

    return _normalize


async def test_fires_delivery_time_changed_event(hass, monkeypatch):
    """Ukrposhta reports no window today; the shared event path still works."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )
    target = "custom_components.ukrposhta.coordinator.normalize_parcel"
    monkeypatch.setattr(target, _with_window("2026-04-29T13:00:00Z", None))
    await coordinator._async_update_data()  # first refresh: suppressed
    monkeypatch.setattr(target, _with_window("2026-04-29T16:00:00Z", None))
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_planned_from"] == "2026-04-29T16:00:00Z"


async def test_losing_the_eta_is_silent(hass, monkeypatch):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )
    target = "custom_components.ukrposhta.coordinator.normalize_parcel"
    monkeypatch.setattr(target, _with_window("2026-04-29T13:00:00Z", None))
    await coordinator._async_update_data()
    monkeypatch.setattr(target, _with_window(None, None))
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


# ---------------------------------------------------------------------------
# credential and failure handling
# ---------------------------------------------------------------------------


async def test_credential_refusal_warns_once_and_fails_the_update(hass, caplog):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: OTHER_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = UkrposhtaRejectedError(
        "HTTP 403", status_code=403
    )
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    for _ in range(2):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert client.async_get_parcel.await_count == 2  # stops after the first refusal
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "issues/new?template=unrecognised_status.yml" in warnings[0].getMessage()
    assert ACTIVE_CODE not in caplog.text
    assert "Bearer" not in caplog.text


async def test_credential_warning_returns_after_a_success(hass, caplog):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    client.async_get_parcel.side_effect = UkrposhtaRejectedError("HTTP 401")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    client.async_get_parcel.side_effect = None
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()
    client.async_get_parcel.side_effect = UkrposhtaRejectedError("HTTP 401")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2


async def test_unconfigured_credential_fails_the_update_without_reauth(
    hass, caplog, monkeypatch
):
    """An unset constant must degrade like a rejected request."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.ukrposhta import api
    from custom_components.ukrposhta.api import UkrposhtaApiClient

    monkeypatch.setattr(api, "CARRIER_BEARER", "")
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    session = MagicMock()
    coordinator = UkrposhtaCoordinator(hass, UkrposhtaApiClient(session), entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    session.get.assert_not_called()
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert list(hass.config_entries.flow.async_progress()) == []


async def test_rate_limit_stops_the_batch_and_keeps_cached_data(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: OTHER_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: active_sample(code)
    coordinator = UkrposhtaCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    client.async_get_parcel.reset_mock()
    client.async_get_parcel.side_effect = UkrposhtaApiError(
        "HTTP 429", status_code=429, retry_after=60
    )
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert client.async_get_parcel.await_count == 1


async def test_failure_warning_omits_the_code_and_network_error_text(hass, caplog):
    import aiohttp

    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UkrposhtaCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    client.async_get_parcel.side_effect = aiohttp.ClientError(
        f"failed for https://example.invalid/?barcode={ACTIVE_CODE}"
    )
    await coordinator._async_update_data()  # cache keeps the parcel

    assert "ClientError" in caplog.text
    assert ACTIVE_CODE not in caplog.text


async def test_credential_warning_returns_after_a_no_events_result(hass, caplog):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    client.async_get_parcel.side_effect = UkrposhtaRejectedError("HTTP 401")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    client.async_get_parcel.side_effect = None
    client.async_get_parcel.return_value = None
    await coordinator._async_update_data()
    client.async_get_parcel.side_effect = UkrposhtaRejectedError("HTTP 401")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2
