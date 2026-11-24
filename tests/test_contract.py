"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from custom_components.ukrposhta.const import ParcelStatus
from custom_components.ukrposhta.parcels import (
    normalize_parcel,
    parse_iso,
    planned_for_status,
    planned_window,
)

from .payloads import (
    ACTIVE_CODE,
    active_sample,
    delivered_sample,
    event,
    pickup_sample,
    returned_events,
    sample,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

KYIV = ZoneInfo("Europe/Kyiv")


def _collected() -> dict:
    return sample(
        [
            event("20700", "2026-04-28T15:52:17", "Processed"),
            event("21700", "2026-04-29T08:46:00", "Arrived at the office"),
            event("41000", "2026-04-29T16:00:00", "Delivered to Recipient"),
        ],
        ACTIVE_CODE,
    )


def _returning() -> dict:
    return sample(returned_events()[:-1], ACTIVE_CODE)


def _returned() -> dict:
    return sample(returned_events(), ACTIVE_CODE)


def _problem() -> dict:
    return sample([event("31100", "2026-04-29T09:00:00", "Problem")], ACTIVE_CODE)


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "at_pickup_point": pickup_sample,
    "collected": _collected,
    "returning": _returning,
    "returned": _returned,
    "problem": _problem,
    "bare": lambda: {"trackingNumber": "EXAMPLE000001"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_rule(parcel):
    back_at_sender = (
        parcel["status"] is ParcelStatus.RETURNING
        and parcel["raw_status"] == "41000 Return: Delivered to Sender"
    )
    assert parcel["delivered"] is (
        parcel["status"] is ParcelStatus.DELIVERED or back_at_sender
    )


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_pickup_is_only_set_after_an_office_arrival():
    assert normalize_parcel(active_sample())["pickup"] is False
    assert normalize_parcel(pickup_sample())["pickup"] is True


def test_a_collected_parcel_stays_pickup():
    parcel = normalize_parcel(_collected())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T16:00:00+03:00"
    assert parcel["pickup"] is True


def test_a_parcel_that_never_reached_an_office_is_not_pickup():
    parcel = normalize_parcel(_direct_delivery())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is False


def _direct_delivery() -> dict:
    return sample(
        [
            event("20700", "2026-04-28T15:52:17", "Processed"),
            event("41000", "2026-04-29T16:00:00", "Delivered"),
        ],
        ACTIVE_CODE,
    )


def test_a_return_keeps_pickup_after_an_office_arrival():
    parcel = normalize_parcel(_returning())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["pickup"] is True


def test_a_moment_ends_at_the_end_of_that_day():
    planned_from, planned_to = planned_window("2099-04-29T13:00:00Z", None, zone=KYIV)
    end = datetime.fromisoformat(planned_to)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == parse_iso(planned_from).astimezone(KYIV).date()


def test_a_date_covers_the_whole_day():
    planned_from, planned_to = planned_window("2099-04-29", None, zone=KYIV)
    start = datetime.fromisoformat(planned_from)
    end = datetime.fromisoformat(planned_to)
    assert start.timetz().replace(tzinfo=None) == time.min
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
    assert (
        start.utcoffset()
        == end.utcoffset()
        == datetime(2099, 4, 29, tzinfo=KYIV).utcoffset()
    )


def test_the_window_is_emptied_for_a_finished_parcel():
    window = planned_window("2099-04-29", None, zone=UTC)
    assert planned_for_status(ParcelStatus.OUT_FOR_DELIVERY, *window) == window
    for status in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert planned_for_status(status, *window) == (None, None)


def test_window_shapes():
    assert planned_window(None, None, zone=KYIV) == (None, None)
    assert planned_window(None, "2099-04-29T10:00:00+03:00", zone=KYIV) == (
        "2099-04-29T10:00:00+03:00",
        "2099-04-29T23:59:59+03:00",
    )
    assert planned_window(
        "2099-04-29T10:00:00+03:00", "2099-04-29T12:00:00+03:00", zone=KYIV
    ) == ("2099-04-29T10:00:00+03:00", "2099-04-29T12:00:00+03:00")
    # reversed values are swapped, equal values are one moment
    assert planned_window(
        "2099-04-29T12:00:00+03:00", "2099-04-29T10:00:00+03:00", zone=KYIV
    ) == ("2099-04-29T10:00:00+03:00", "2099-04-29T12:00:00+03:00")
    assert planned_window(
        "2099-04-29T10:00:00+03:00", "2099-04-29T10:00:00+03:00", zone=KYIV
    ) == ("2099-04-29T10:00:00+03:00", "2099-04-29T23:59:59+03:00")
    assert planned_window("2099-04-29", "2099-04-30", zone=KYIV) == (
        "2099-04-29T00:00:00+03:00",
        "2099-04-30T23:59:59+03:00",
    )
    assert planned_window("2099-04-29T23:59:59+03:00", None, zone=KYIV) == (
        "2099-04-29T23:59:59+03:00",
        None,
    )
    assert planned_window("nonsense", None, zone=KYIV) == (None, None)
    assert planned_window(
        datetime(2099, 4, 29, 8), datetime(2099, 4, 29).date(), zone=KYIV
    ) == ("2099-04-29T08:00:00+03:00", "2099-04-29T23:59:59+03:00")
    assert planned_window(42, None, zone=KYIV) == (None, None)
