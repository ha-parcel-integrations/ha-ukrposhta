"""Synthetic Ukrposhta payloads shared by the test modules.

Synthetic payloads (obviously fake codes, a fixed fictional timeline, generic
event names) plus two scrubbed real parcels, ``scrubbed_parcel_events`` and
``scrubbed_returned_parcel_events``, whose barcode, place names and postal
indexes are placeholders and whose dates, codes and event names are as received. ``*_events`` helpers build the chronological list the
endpoint answers with; ``*_sample`` helpers build what the API client hands
the coordinator (barcode plus the event list).
"""
from __future__ import annotations

from copy import deepcopy

ACTIVE_CODE = "EXAMPLE999999"
DELIVERED_CODE = "EXAMPLE123456"
TEST_BEARER = "test-bearer"


def event(code, date: str | None, name: str | None, barcode: str | None = None) -> dict:
    """One event reduced to the keys the mapping reads."""
    return {"barcode": barcode, "date": date, "event": code, "eventName": name}


def wire_event(code, date: str, name: str, barcode: str) -> dict:
    """One event in the endpoint's own full shape, with person fields blanked."""
    return {
        "barcode": barcode,
        "date": date,
        "name": "",
        "country": "",
        "event": code,
        "eventName": name,
        "eventReason": "",
        "eventReason_id": 0,
        "index": "00000",
        "indexOrder": 0,
        "mailType": "",
        "step": 0,
        "value": None,
    }


def in_transit_events(code: str = ACTIVE_CODE) -> list[dict]:
    return [
        event("10601", "2026-04-27T23:03:58", "Registered", code),
        event("20700", "2026-04-28T15:52:17", "Processed", code),
    ]


def delivered_events(code: str = DELIVERED_CODE) -> list[dict]:
    return [
        *in_transit_events(code),
        event("21700", "2026-04-29T08:46:00", "Arrived at the office", code),
        event("41000", "2026-04-29T13:12:42", "Delivered", code),
    ]


def returned_events(code: str = ACTIVE_CODE) -> list[dict]:
    """A refused parcel whose return leg reuses the forward codes."""
    return [
        *in_transit_events(code),
        event("21700", "2026-04-29T08:46:00", "Arrived at the office", code),
        event("31200", "2026-04-30T09:00:00", "Returned to Sender", code),
        event("20700", "2026-05-01T09:00:00", "Return: Processed", code),
        event("41000", "2026-05-03T12:00:00", "Return: Delivered to Sender", code),
    ]


LC = "Logistics Centre"
ARRIVED_LC = "Arrived to the Logistics Center"
DEPARTED_LC = "Departed from the Logistics Center"


def _wire(code: str, rows: list[tuple]) -> list[dict]:
    """Build wire-shape events from compact rows.

    Row: (date, index, indexOrder, event, eventName, eventReason, reasonId,
    place). ``step`` is the 1-based position.
    """
    return [
        {
            "barcode": code,
            "date": date,
            "name": place,
            "country": "UKRAINE",
            "event": event_code,
            "eventName": name,
            "eventReason": reason,
            "eventReason_id": reason_id,
            "index": index,
            "indexOrder": order,
            "mailType": 4096,
            "step": step,
            "value": None,
        }
        for step, (date, index, order, event_code, name, reason, reason_id, place)
        in enumerate(rows, start=1)
    ]


def scrubbed_parcel_events(code: str = DELIVERED_CODE) -> list[dict]:
    """A real delivered domestic parcel, 13 events, in the exact wire shape.

    Scrubbed: the barcode, place names and postal indexes are placeholders
    (the index is null exactly where the real one was). Dates, codes, event
    names and reasons are as received.
    """
    o, d = "00001", "00002"
    return _wire(code, [
        ("2026-08-05T12:09:03", o, 1, "10601", "Created online. Waiting acceptance", None, 1, "Origin Branch"),
        ("2026-08-06T10:52:00", o, 2, "10100", "Registered", None, 1, "Origin Branch"),
        ("2026-08-06T14:00:22", o, 3, "20800", "Departed from the Branch", "Branch", 60, "Origin Branch"),
        ("2026-08-06T16:21:06", None, 1, "20700", ARRIVED_LC, None, 1, LC),
        ("2026-08-06T17:41:35", None, 2, "20800", DEPARTED_LC, None, 1, LC),
        ("2026-08-07T00:42:20", None, 3, "20700", ARRIVED_LC, None, 1, LC),
        ("2026-08-07T00:57:36", None, 4, "20800", DEPARTED_LC, None, 1, LC),
        ("2026-08-07T02:16:41", None, 5, "20700", ARRIVED_LC, None, 1, LC),
        ("2026-08-11T10:57:23", None, 6, "20800", DEPARTED_LC, None, 1, LC),
        ("2026-08-11T14:22:08", None, 7, "20700", ARRIVED_LC, None, 1, LC),
        ("2026-08-11T16:16:43", None, 8, "20800", DEPARTED_LC, None, 1, LC),
        ("2026-08-11T18:13:19", d, 1, "21700", "Arrived to the Branch", None, 1, "Destination Branch"),
        ("2026-08-12T15:18:16", d, 2, "41000", "Delivered to Recipient", "to Recipient", 2, "Destination Branch"),
    ])


def scrubbed_returned_parcel_events(code: str = ACTIVE_CODE) -> list[dict]:
    """A real refused-and-returned parcel, 14 events, in the exact wire shape.

    Scrubbed the same way as :func:`scrubbed_parcel_events`.
    """
    o, d = "00001", "00002"
    ret = "Return: "
    return _wire(code, [
        ("2026-08-16T18:11:22", o, 1, "10601", "Created online. Waiting acceptance", None, 1, "Origin Branch"),
        ("2026-08-16T18:17:00", o, 2, "10100", "Registered", None, 1, "Origin Branch"),
        ("2026-08-17T12:39:56", o, 3, "20800", "Departed from the Branch", "Branch", 60, "Origin Branch"),
        ("2026-08-19T09:01:55", d, 1, "21700", "Arrived to the Branch", None, 1, "Destination Branch"),
        ("2026-08-20T10:34:09", d, 2, "31200", "Returned to Sender (Due to Recipient's Refusal)", "(Due to Recipient's Refusal)", 7, "Destination Branch"),
        ("2026-08-20T12:04:21", None, 1, "20800", ret + "Departed from the Branch", None, 1, LC),
        ("2026-08-20T18:58:07", None, 2, "20700", ret + ARRIVED_LC, None, 1, LC),
        ("2026-08-21T00:37:50", None, 3, "20800", ret + DEPARTED_LC, None, 1, LC),
        ("2026-08-21T03:05:32", None, 4, "20700", ret + ARRIVED_LC, None, 1, LC),
        ("2026-08-21T03:05:59", None, 5, "20800", ret + DEPARTED_LC, None, 1, LC),
        ("2026-08-21T09:09:40", None, 6, "20700", ret + ARRIVED_LC, None, 1, LC),
        ("2026-08-21T11:27:29", None, 7, "20800", ret + DEPARTED_LC, None, 1, LC),
        ("2026-08-21T13:32:29", o, 4, "21700", ret + "Arrived to the Branch", None, 1, "Origin Branch"),
        ("2026-08-24T16:03:27", o, 5, "41000", ret + "Delivered to Sender", "to Sender", 10, "Origin Branch"),
    ])


def sample(events: list[dict], code: str) -> dict:
    return {"trackingNumber": code, "events": deepcopy(events)}


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    return sample(delivered_events(code), code)


def active_sample(code: str = ACTIVE_CODE) -> dict:
    return sample(in_transit_events(code), code)


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    return sample(delivered_events(code)[:3], code)
