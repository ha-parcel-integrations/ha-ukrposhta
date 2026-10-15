"""Tests for the pure parcel-mapping helpers (no Home Assistant instance)."""
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ukrposhta.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.ukrposhta.coordinator import UkrposhtaCoordinator
from custom_components.ukrposhta.parcels import (
    _STATUS_MAP,
    apply_delivered_filter,
    build_history,
    classify_events,
    map_event_status,
    normalize_parcel,
    parse_iso,
    resolve_status,
    sort_parcels_by_ts,
    tracking_url,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
    event,
    pickup_sample,
    returned_events,
    sample,
    scrubbed_parcel_events,
    scrubbed_returned_parcel_events,
)

# ---------------------------------------------------------------------------
# status map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("10601", ParcelStatus.REGISTERED),
        ("10100", ParcelStatus.REGISTERED),
        ("20700", ParcelStatus.IN_TRANSIT),
        ("20800", ParcelStatus.IN_TRANSIT),
        ("21500", ParcelStatus.IN_TRANSIT),
        ("21400", ParcelStatus.AT_PICKUP_POINT),
        ("21700", ParcelStatus.AT_PICKUP_POINT),
        ("31100", ParcelStatus.PROBLEM),
        ("31300", ParcelStatus.PROBLEM),
        ("31400", ParcelStatus.PROBLEM),
        ("31200", ParcelStatus.RETURNING),
        ("41000", ParcelStatus.DELIVERED),
        ("48000", ParcelStatus.DELIVERED),
    ],
)
def test_map_event_status_known(code, expected):
    assert map_event_status(code) == expected


def test_status_map_holds_only_the_documented_codes():
    assert len(_STATUS_MAP) == 13


@pytest.mark.parametrize("code", ["29500", "27900", "24300", "2201", "99999"])
def test_unproven_codes_are_unmapped(code):
    assert map_event_status(code) is None


def test_map_event_status_missing_and_unmapped_are_none_and_silent(caplog):
    assert map_event_status(None) is None
    assert map_event_status("") is None
    assert map_event_status("12345") is None
    assert caplog.text == ""


def _statuses(*codes):
    events = [
        event(code, f"2026-04-{day:02d}T10:00:00", "x")
        for day, code in enumerate(codes, start=1)
    ]
    return normalize_parcel(sample(events, "C"))["status"]


def test_unmapped_final_event_falls_back_to_newest_mappable_event(caplog):
    with caplog.at_level(logging.WARNING):
        assert _statuses("10601", "20700", "29500") == ParcelStatus.IN_TRANSIT
        assert _statuses("10601", "20700", "29500") == ParcelStatus.IN_TRANSIT
    # A rescued fallback still warns, once, without the code.
    assert caplog.text.count("Unrecognised Ukrposhta status") == 1
    assert "status=29500 → reported as 'in_transit'" in caplog.text
    assert "issues/new?template=unrecognised_status.yml" in caplog.text


def test_each_unmapped_code_warns_even_when_they_rescue_to_the_same_status(caplog):
    with caplog.at_level(logging.WARNING):
        assert _statuses("20700", "29500") == ParcelStatus.IN_TRANSIT
        assert _statuses("20700", "27900") == ParcelStatus.IN_TRANSIT
        assert _statuses("20700", "27900") == ParcelStatus.IN_TRANSIT
    assert "status=29500" in caplog.text
    assert "status=27900" in caplog.text
    assert caplog.text.count("Unrecognised Ukrposhta status") == 2


def test_the_warning_logs_the_code_but_no_free_text(caplog):
    raw = sample(
        [
            event("20700", "2026-04-24T10:00:00", "Processed", "EXAMPLE555555"),
            event("29500", "2026-04-25T10:00:00", "Out somewhere", "EXAMPLE555555"),
        ],
        "EXAMPLE555555",
    )
    with caplog.at_level(logging.WARNING):
        normalize_parcel(raw)
    assert "status=29500" in caplog.text
    assert "Out somewhere" not in caplog.text
    assert "EXAMPLE555555" not in caplog.text


def test_a_non_numeric_event_code_never_reaches_the_log(caplog):
    with caplog.at_level(logging.WARNING):
        assert _statuses("20700", "Free Text Here") == ParcelStatus.IN_TRANSIT
    assert "Free Text Here" not in caplog.text
    assert "status=non-numeric" in caplog.text


def test_delivered_at_comes_from_the_event_that_carries_delivered():
    events = [
        event("20700", "2026-04-28T10:00:00", "Processed"),
        event("41000", "2026-04-29T13:12:42", "Delivered"),
        event("29500", "2026-04-30T09:00:00", "Unmapped follow-up"),
    ]
    parcel = normalize_parcel(sample(events, "C"))
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42+03:00"


def test_fallback_skips_earlier_unmapped_events():
    assert _statuses("10601", "27900", "29500") == ParcelStatus.REGISTERED


def test_nothing_mappable_is_unknown_and_every_code_is_reported_once(caplog):
    with caplog.at_level(logging.WARNING):
        assert _statuses("29500", "27900") == ParcelStatus.UNKNOWN
        assert _statuses("29500", "27900") == ParcelStatus.UNKNOWN
    # one line per unrecognised code, however often it is seen
    assert caplog.text.count("Unrecognised Ukrposhta status") == 2
    assert caplog.text.count("29500") == 1
    assert caplog.text.count("27900") == 1


def test_a_code_buried_in_the_history_is_reported_too(caplog):
    with caplog.at_level(logging.WARNING):
        assert _statuses("29500", "41000") == ParcelStatus.DELIVERED
    assert "29500" in caplog.text
    assert "without a status" in caplog.text


def test_fallback_never_walks_back_past_the_return_leg():
    """An unmapped scan inside the return leg must not resurrect in_transit."""
    assert _statuses("20700", "31200", "29500") == ParcelStatus.RETURNING


def test_fallback_never_resurrects_a_delivered_state_after_the_return_leg():
    assert _statuses("41000", "31200", "99999") == ParcelStatus.RETURNING


def test_resolve_status_without_events_is_silently_unknown(caplog):
    assert resolve_status([]) == ParcelStatus.UNKNOWN
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_tracking_url_is_encoded():
    assert tracking_url("A B/1") == "https://track.ukrposhta.ua/en/?barcode=A%20B%2F1"
    assert tracking_url(None) is None


# ---------------------------------------------------------------------------
# return leg
# ---------------------------------------------------------------------------


def test_return_leg_never_maps_forward_delivered_code_to_delivered():
    """A return leg reuses 41000; it must not read as delivered to the recipient."""
    parcel = normalize_parcel(sample(returned_events(), ACTIVE_CODE))
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_events_after_the_opening_code_are_returning_without_a_label():
    events = [
        event("21700", "2026-04-29T08:46:00", "Arrived"),
        event("31200", "2026-04-30T09:00:00", "Returned"),
        event("48000", "2026-05-01T09:00:00", "Delivered"),
    ]
    statuses = [status for _, status in classify_events(events)]
    assert statuses == [
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
        ParcelStatus.RETURNING,
    ]


def test_return_label_alone_opens_the_return_leg():
    events = [
        event("20700", "2026-04-29T08:46:00", "Processed"),
        event("41000", "2026-05-03T12:00:00", "Return: Delivered to Sender"),
    ]
    parcel = normalize_parcel(sample(events, ACTIVE_CODE))
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_unmapped_code_inside_the_return_leg_does_not_warn(caplog):
    events = [
        event("31200", "2026-04-30T09:00:00", "Returned"),
        event("29500", "2026-05-01T09:00:00", "Return: Something"),
    ]
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel(sample(events, ACTIVE_CODE))
    assert parcel["status"] == ParcelStatus.RETURNING
    assert "29500" not in caplog.text


def test_return_history_is_returning_throughout_the_leg():
    parcel = normalize_parcel(
        sample(returned_events(), ACTIVE_CODE), include_history=True
    )
    assert [e["status"] for e in parcel["history"]][-3:] == [ParcelStatus.RETURNING] * 3


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_keeps_chronological_order_and_text():
    history = build_history(classify_events(delivered_sample()["events"]))
    assert len(history) == 4
    assert history[0] == {
        "timestamp": "2026-04-27T23:03:58+03:00",
        "status": ParcelStatus.REGISTERED,
        "raw_status": "10601 Registered",
    }
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_the_most_recent_events():
    events = [
        event("20700", f"2026-04-{day:02d}T10:00:00", f"step {day}")
        for day in range(1, 26)
    ]
    history = build_history(classify_events(events))
    assert len(history) == 20
    assert history[-1]["raw_status"] == "20700 step 25"
    assert history[0]["raw_status"] == "20700 step 6"


def test_build_history_skips_events_without_a_date(caplog):
    history = build_history(classify_events([event("20700", None, "x")]))
    assert history == []
    assert "event without a date" in caplog.text


def test_build_history_drops_an_unparseable_date_and_warns_once(caplog):
    events = [event("20700", "not-a-date", "a"), event("20700", "also-bad", "b")]
    assert build_history(classify_events(events)) == []
    assert caplog.text.count("unparseable event date") == 1


def test_build_history_uses_only_the_code_without_a_name():
    history = build_history(classify_events([event("20700", "2026-04-24T10:00:00", "")]))
    assert history[0]["raw_status"] == "20700"


def test_classify_skips_non_object_events(caplog):
    assert classify_events(["junk", None]) == []
    assert "event is not an object" in caplog.text
    assert classify_events(None) == []


def test_event_without_a_code_is_unknown_and_warns(caplog):
    parcel = normalize_parcel(sample([event(None, "2026-04-24T10:00:00", "x")], "C"))
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["raw_status"] == "x"
    assert "event without a code" in caplog.text


def test_integer_event_codes_are_accepted():
    parcel = normalize_parcel(sample([event(41000, "2026-04-24T10:00:00", "x")], "C"))
    assert parcel["status"] == ParcelStatus.DELIVERED


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    delivered = normalize_parcel(delivered_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)
    assert CAPABILITIES == {"url", "history"}
    assert PENDING_CAPABILITIES == {"pickup_point"}
    assert delivered["url"] is not None
    assert with_history["history"] is not None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Ukrposhta"
    assert parcel["barcode"] is None or parcel["barcode"] == DELIVERED_CODE
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "41000 Delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42+03:00"
    assert parcel["url"] == f"https://track.ukrposhta.ua/en/?barcode={DELIVERED_CODE}"
    assert parcel["history"] is None


def test_normalize_unproven_fields_stay_none():
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    for key in (
        "sender",
        "receiver",
        "planned_from",
        "planned_to",
        "pickup",
        "pickup_point",
        "weight",
        "dimensions",
    ):
        assert parcel[key] is None


def test_normalize_active_parcel():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_normalize_uses_the_final_events_barcode_over_the_registered_code():
    raw = sample([event("20700", "2026-04-24T10:00:00", "x", barcode="EXAMPLE777777")], "EXAMPLE1")
    assert normalize_parcel(raw)["barcode"] == "EXAMPLE777777"


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 4
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_pending_placeholder():
    """A tracked code with no events still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": "EXAMPLE000001"})
    assert parcel["barcode"] == "EXAMPLE000001"
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["history"] is None


def test_delivered_event_with_a_bad_date_has_no_delivered_at(caplog):
    parcel = normalize_parcel(sample([event("41000", "garbage", "Delivered")], "C"))
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is None
    assert "delivered event without a usable date" in caplog.text


def test_warnings_carry_no_payload_values(caplog):
    raw = sample(
        [event("29500", "2026-04-25T10:00:00", "Out somewhere", "EXAMPLE555555")],
        "EXAMPLE555555",
    )
    with caplog.at_level(logging.WARNING):
        normalize_parcel(raw)
    assert "EXAMPLE555555" not in caplog.text
    assert "Out somewhere" not in caplog.text


def test_raw_is_the_full_untrimmed_record():
    raw = {
        "trackingNumber": "EXAMPLE1",
        "events": [dict(event("20700", "2026-04-25T10:00:00", "x"), name="", step=1)],
    }
    assert normalize_parcel(raw)["raw"] is raw


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


# ---------------------------------------------------------------------------
# carrier timezone
# ---------------------------------------------------------------------------


def test_naive_dates_are_attached_to_the_carrier_timezone_with_dst():
    summer = normalize_parcel(sample([event("41000", "2026-08-12T15:18:16", "x")], "C"))
    winter = normalize_parcel(sample([event("41000", "2026-01-12T15:18:16", "x")], "C"))
    assert summer["delivered_at"] == "2026-08-12T15:18:16+03:00"
    assert winter["delivered_at"] == "2026-01-12T15:18:16+02:00"


@pytest.mark.parametrize("date", ["2026-08-12T15:18:16Z", "2026-08-12T15:18:16+05:00"])
def test_dates_with_an_offset_keep_it(date):
    parcel = normalize_parcel(sample([event("41000", date, "x")], "C"))
    assert parcel["delivered_at"] == date.replace("Z", "+00:00")


def test_published_delivered_at_ages_out_on_local_time():
    """Read as UTC the same wall clock would outlive its window by three hours."""
    parcel = normalize_parcel(delivered_sample())
    assert parse_iso(parcel["delivered_at"]) == parse_iso("2026-04-29T10:12:42Z")


# ---------------------------------------------------------------------------
# scrubbed real-shape parcel
# ---------------------------------------------------------------------------


def test_scrubbed_parcel_has_all_thirteen_wire_keys_and_wire_order():
    events = scrubbed_parcel_events()
    assert len(events) == 13
    assert all(len(e) == 13 and e["value"] is None for e in events)
    assert all(e["mailType"] == 4096 and e["country"] == "UKRAINE" for e in events)
    assert [e["step"] for e in events] == list(range(1, 14))
    # indexOrder restarts per location; it is not a sequence to sort on.
    assert [e["indexOrder"] for e in events] != sorted(e["indexOrder"] for e in events)


def test_scrubbed_parcel_arc_and_fields():
    raw = {"trackingNumber": DELIVERED_CODE, "events": scrubbed_parcel_events()}
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-08-12T15:18:16+03:00"
    assert parcel["raw_status"] == "41000 Delivered to Recipient"
    statuses = [h["status"] for h in parcel["history"]]
    assert statuses[:3] == [ParcelStatus.REGISTERED] * 2 + [ParcelStatus.IN_TRANSIT]
    assert ParcelStatus.AT_PICKUP_POINT in statuses
    assert len(parcel["history"]) == 13
    stamps = [parse_iso(h["timestamp"]) for h in parcel["history"]]
    assert stamps == sorted(stamps)
    assert "eventReason" not in parcel["raw_status"]
    assert parcel["raw"] is raw


def test_history_keeps_wire_order_not_indexorder():
    raw = {"trackingNumber": DELIVERED_CODE, "events": scrubbed_parcel_events()}
    history = normalize_parcel(raw, include_history=True)["history"]
    assert history[3]["raw_status"] == "20700 Arrived to the Logistics Center"
    assert history[-1]["raw_status"] == "41000 Delivered to Recipient"


def test_blank_or_non_string_dates_are_not_published():
    parcel = normalize_parcel(sample([event("41000", "  ", "x")], "C"))
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is None


def test_scrubbed_parcel_index_is_null_exactly_at_logistics_centres():
    for events in (scrubbed_parcel_events(), scrubbed_returned_parcel_events()):
        for e in events:
            assert (e["index"] is None) == (e["name"] == "Logistics Centre")


def test_scrubbed_parcel_reasons_match_the_real_vocabulary():
    reasons = {
        (e["eventReason"], e["eventReason_id"])
        for e in scrubbed_parcel_events() + scrubbed_returned_parcel_events()
    }
    assert reasons == {
        (None, 1),
        ("Branch", 60),
        ("to Recipient", 2),
        ("(Due to Recipient's Refusal)", 7),
        ("to Sender", 10),
    }


# ---------------------------------------------------------------------------
# real return leg
# ---------------------------------------------------------------------------


def _returned_raw():
    return {"trackingNumber": ACTIVE_CODE, "events": scrubbed_returned_parcel_events()}


def test_real_return_leg_ends_in_a_return_delivered_41000_that_is_not_delivered():
    raw = _returned_raw()
    last = raw["events"][-1]
    assert last["event"] == "41000"
    assert last["eventName"] == "Return: Delivered to Sender"

    parcel = normalize_parcel(raw, include_history=True)

    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["raw_status"] == "41000 Return: Delivered to Sender"


def test_real_return_leg_history_is_forward_then_returning_from_the_refusal():
    history = normalize_parcel(_returned_raw(), include_history=True)["history"]
    statuses = [h["status"] for h in history]
    assert len(history) == 14
    assert statuses[:4] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.AT_PICKUP_POINT,
    ]
    assert statuses[4:] == [ParcelStatus.RETURNING] * 10


def test_real_return_leg_prefix_alone_would_also_keep_it_undelivered():
    """Without the 31200 scan the Return: labels still mark the leg."""
    raw = _returned_raw()
    raw["events"] = [e for e in raw["events"] if e["event"] != "31200"]
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["delivered"] is False


async def test_real_return_leg_never_enters_the_delivered_list(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={"parcels": [{"tracking_code": ACTIVE_CODE}]},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _returned_raw()
    coordinator = UkrposhtaCoordinator(hass, client, entry)

    active = await coordinator._async_update_data()

    assert [p["status"] for p in active] == [ParcelStatus.RETURNING]
    assert coordinator.delivered == []
    assert coordinator.delivered_codes == set()
