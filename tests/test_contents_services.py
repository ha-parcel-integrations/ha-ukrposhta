"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ukrposhta.const import CONF_PARCELS, CONF_TRACKING_CODE, DOMAIN
from custom_components.ukrposhta.coordinator import UkrposhtaCoordinator
from custom_components.ukrposhta.parcels import normalize_parcel
from custom_components.ukrposhta.services import _contents_store

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_sample, delivered_sample

CLIENT = "custom_components.ukrposhta.api.UkrposhtaApiClient"
COORDINATOR = f"{UkrposhtaCoordinator.__module__}.UkrposhtaCoordinator"
UNTRACKED = "EXAMPLE555555"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _sample_for(code: str) -> dict:
    return delivered_sample(code) if code == DELIVERED_CODE else active_sample(code)


async def _setup(hass, codes: list[str] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes or []]},
    )
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_get_parcel", new=AsyncMock(side_effect=_sample_for)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service: str, data: dict) -> AsyncMock:
    fetch = AsyncMock(side_effect=_sample_for)
    with patch(f"{CLIENT}.async_get_parcel", new=fetch):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
    return fetch


def _contents(entry: MockConfigEntry, code: str) -> str | None:
    coordinator = entry.runtime_data.coordinator
    parcels = {p["barcode"]: p for p in [*coordinator.data, *coordinator.delivered]}
    return parcels[code]["contents"]


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_sets_replaces_and_clears_without_fetching(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    update = {CONF_TRACKING_CODE: ACTIVE_CODE}

    fetch = await _call(
        hass, "update_contents", {**update, "contents": " Running  shoes "}
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"
    fetch.assert_not_called()

    fetch = await _call(hass, "update_contents", {**update, "contents": "Winter socks"})
    assert _contents(entry, ACTIVE_CODE) == "Winter socks"
    fetch.assert_not_called()

    fetch = await _call(hass, "update_contents", {**update, "contents": "  "})
    assert _contents(entry, ACTIVE_CODE) is None
    fetch.assert_not_called()


async def test_update_contents_reaches_a_delivered_parcel(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    coordinator = entry.runtime_data.coordinator
    coordinator.delivered = [normalize_parcel(delivered_sample())]
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: DELIVERED_CODE, "contents": "Running shoes"},
    )
    assert coordinator.delivered[0]["contents"] == "Running shoes"


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: "example-999 999", "contents": "Running shoes"},
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    fetch = await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert entry.runtime_data.coordinator.contents_store.get(UNTRACKED) == (
        "Running shoes"
    )
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]
    fetch.assert_not_called()


async def test_update_contents_reapplies_only_when_something_changed(hass):
    await _setup(hass, [ACTIVE_CODE])
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}
    with (
        patch(f"{COORDINATOR}.async_reapply_contents", new=AsyncMock()) as reapply,
        patch(f"{COORDINATOR}.async_request_refresh", new=AsyncMock()) as refresh,
    ):
        await _call(hass, "update_contents", data)
        assert reapply.await_count == 1
        await _call(hass, "update_contents", data)
        assert reapply.await_count == 1
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: UNTRACKED})
        assert reapply.await_count == 1
    refresh.assert_not_called()


async def test_reapply_contents_notifies_listeners_without_a_refresh(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(ACTIVE_CODE, "Running shoes")
    with (
        patch(f"{COORDINATOR}.async_update_listeners") as notify,
        patch(f"{COORDINATOR}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{COORDINATOR}._async_update_data", new=AsyncMock()) as update,
    ):
        await coordinator.async_reapply_contents()
    notify.assert_called_once_with()
    refresh.assert_not_called()
    update.assert_not_called()
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: ""})


async def test_update_contents_needs_a_loaded_entry(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert _contents(entry, ACTIVE_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_without_fetching(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    track = {CONF_TRACKING_CODE: ACTIVE_CODE}

    fetch = await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"
    fetch.assert_not_called()

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(entry, ACTIVE_CODE) == "Winter socks"

    fetch = await _call(hass, "track_parcel", {**track, "contents": ""})
    assert _contents(entry, ACTIVE_CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]
    fetch.assert_not_called()


async def test_track_parcel_unchanged_contents_does_nothing(hass):
    await _setup(hass, [ACTIVE_CODE])
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}
    with patch(f"{COORDINATOR}.async_reapply_contents", new=AsyncMock()) as reapply:
        await _call(hass, "track_parcel", data)
        await _call(hass, "track_parcel", data)
    assert reapply.await_count == 1


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch(f"{CLIENT}.async_get_parcel", new=AsyncMock(side_effect=_sample_for)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_removing_the_entry_removes_its_store(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass, [ACTIVE_CODE])
    events = []
    for name in ("status_changed", "registered", "delivered", "delivery_time_changed"):
        hass.bus.async_listen(f"{DOMAIN}_parcel_{name}", events.append)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    assert events == []


def test_no_contents_store_is_offered_for_an_entry_that_is_not_loaded():
    entry = MagicMock()
    entry.state = ConfigEntryState.SETUP_RETRY
    assert _contents_store(entry) is None
