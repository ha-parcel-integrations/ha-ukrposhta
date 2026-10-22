"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :data:`_STATUS_MAP` and
:func:`normalize_parcel`. Everything else — the
timestamp parsing, the history builder, the sort contract, the delivered
filter, the one-shot warning for unmapped statuses — is suite-wide machinery
and should be left alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .const import (
    CARRIER_TIMEZONE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Built once at import: constructing a ZoneInfo reads the tz file from disk.
_CARRIER_TZ = ZoneInfo(CARRIER_TIMEZONE)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-ukrposhta/issues/new"
    "?template=unrecognised_status.yml"
)

# Keys are the numeric event codes the endpoint reports (as strings). Codes
# outside this map surface as ``unknown`` plus a one-shot warning.
# Each code's own ``eventName`` is what places it below; the international
# codes come from a 2026-10-17 sweep of nine real parcels, domestic and
# cross-border. Customs steps count as transit, matching the rest of the
# suite: a parcel in customs is moving, not stuck.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "10601": ParcelStatus.REGISTERED,
    "10100": ParcelStatus.REGISTERED,
    "17100": ParcelStatus.REGISTERED,  # Acceptance
    "20700": ParcelStatus.IN_TRANSIT,
    "20800": ParcelStatus.IN_TRANSIT,
    "21500": ParcelStatus.IN_TRANSIT,
    # offices of exchange, outward then inward
    "21600": ParcelStatus.IN_TRANSIT,
    "27200": ParcelStatus.IN_TRANSIT,
    "77600": ParcelStatus.IN_TRANSIT,
    "70800": ParcelStatus.IN_TRANSIT,
    "60700": ParcelStatus.IN_TRANSIT,
    "67300": ParcelStatus.IN_TRANSIT,
    "90800": ParcelStatus.IN_TRANSIT,
    "97700": ParcelStatus.IN_TRANSIT,
    "2201": ParcelStatus.IN_TRANSIT,  # accepted by the destination post
    # air transport of the consignment carrying the item
    "4006": ParcelStatus.IN_TRANSIT,
    "4021": ParcelStatus.IN_TRANSIT,
    "4024": ParcelStatus.IN_TRANSIT,
    "4074": ParcelStatus.IN_TRANSIT,
    # customs: presented, then released
    "80700": ParcelStatus.IN_TRANSIT,
    "82700": ParcelStatus.IN_TRANSIT,
    "89000": ParcelStatus.IN_TRANSIT,
    "80800": ParcelStatus.IN_TRANSIT,
    "82800": ParcelStatus.IN_TRANSIT,
    "89100": ParcelStatus.IN_TRANSIT,
    "27900": ParcelStatus.IN_TRANSIT,  # arrival at the delivery office
    "29500": ParcelStatus.OUT_FOR_DELIVERY,  # out for physical delivery
    "21400": ParcelStatus.AT_PICKUP_POINT,
    "21700": ParcelStatus.AT_PICKUP_POINT,
    "31100": ParcelStatus.PROBLEM,
    "31300": ParcelStatus.PROBLEM,
    "31400": ParcelStatus.PROBLEM,
    "31200": ParcelStatus.RETURNING,
    "41000": ParcelStatus.DELIVERED,
    "48000": ParcelStatus.DELIVERED,
}

# ``24300`` "Held at inward office of exchange" is seen live but deliberately
# left out: it reads as a hold yet sits mid-way through an otherwise normal
# customs run, so whether it ever means a real problem is unknown. It falls
# back to the previous mappable scan and keeps reporting.

# A refused parcel opens a return leg whose scans reuse the forward codes
# (including the delivered ones) and carry this label prefix. Everything from
# the opening scan on is the way back to the sender, never a delivery.
RETURN_OPENING_CODE = "31200"
RETURN_LABEL_PREFIX = "Return: "

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str, reported: ParcelStatus) -> None:
    """Log an unmapped final event code once, naming what was reported instead.

    Only the numeric event code is logged; the event name and every other
    free-text value stay out of the log. A non-numeric code is replaced by a
    placeholder so free text can never reach it.
    """
    shown = code if code.isdigit() else "non-numeric"
    if shown in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(shown)
    _LOGGER.warning(
        "Unrecognised Ukrposhta status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as '%s'",
        NEW_ISSUE_URL,
        shown,
        reported.value,
    )


def _warn_unmapped_event(code: str) -> None:
    """Log an unmapped code buried in the history once.

    The newest event's warning names what the parcel was reported as instead,
    so that one is left to the status derivation; without this one an
    unrecognised code further back would never be reported at all.
    """
    shown = code if code.isdigit() else "non-numeric"
    if shown in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(shown)
    _LOGGER.warning(
        "Unrecognised Ukrposhta status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → kept in the parcel's history "
        "without a status",
        NEW_ISSUE_URL,
        shown,
    )


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map one event code to a canonical status, or ``None``.

    Never warns: the caller owns the one-shot warning.
    """
    if not code:
        return None
    return _STATUS_MAP.get(code)


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _carrier_iso(value: Any) -> str | None:
    """Return an offset-aware ISO string for a carrier event date.

    The wire format has no UTC offset. A naive value is attached to
    ``CARRIER_TIMEZONE`` — an assumption no payload has confirmed — rather
    than read as UTC; a value that already carries an offset keeps it.
    ``None`` when the value is not a parseable ISO string.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CARRIER_TZ)
    return parsed.isoformat()


_unproven_shapes_logged: set[str] = set()


def _warn_unproven_shape(kind: str) -> None:
    """Log an unconfirmed payload shape once, structure only."""
    if kind in _unproven_shapes_logged:
        return
    _unproven_shapes_logged.add(kind)
    _LOGGER.warning(
        "Unexpected Ukrposhta event data (%s) — help us confirm the payload "
        "shape. Open an issue and paste this line: %s",
        kind,
        NEW_ISSUE_URL,
    )


def _event_code(event: dict) -> str | None:
    """Return the event's numeric code as a string, or ``None``."""
    value = event.get("event")
    if value is None:
        return None
    return str(value).strip() or None


def classify_events(events: list | None) -> list[tuple[dict, ParcelStatus | None]]:
    """Pair each event with its canonical status, oldest first.

    Once a return leg has opened (``31200`` or a ``Return: `` label), every
    later event is ``returning`` — including the forward delivered codes the
    return leg reuses. Unmapped codes pair with ``None`` and warn once; the
    newest event is left to the status derivation, whose warning can also name
    what the parcel was reported as.
    """
    classified: list[tuple[dict, ParcelStatus | None]] = []
    in_return = False
    candidates = list(events or [])
    for index, event in enumerate(candidates):
        if not isinstance(event, dict):
            _warn_unproven_shape("event is not an object")
            continue
        code = _event_code(event)
        name = event.get("eventName")
        if code == RETURN_OPENING_CODE or (
            isinstance(name, str) and name.startswith(RETURN_LABEL_PREFIX)
        ):
            in_return = True
        if code is None and not in_return:
            _warn_unproven_shape("event without a code")
        status = ParcelStatus.RETURNING if in_return else map_event_status(code)
        if status is None and code is not None and index < len(candidates) - 1:
            _warn_unmapped_event(code)
        classified.append((event, status))
    return classified


def resolve_status(
    classified: list[tuple[dict, ParcelStatus | None]],
) -> ParcelStatus:
    """Return the parcel status: the final event, else the newest mappable one.

    No events, or a final event without a code, report ``unknown`` silently.
    An unmapped final event walks back for the newest mappable event and warns
    once even when that rescues it. Return-leg events are already
    ``returning``, so the walk can never step back over a ``31200``.
    """
    if not classified:
        return ParcelStatus.UNKNOWN
    final_event, final_status = classified[-1]
    if final_status is not None:
        return final_status
    code = _event_code(final_event)
    if code is None:
        return ParcelStatus.UNKNOWN
    for _, earlier in reversed(classified[:-1]):
        if earlier is not None:
            _warn_unmapped_status(code, earlier)
            return earlier
    _warn_unmapped_status(code, ParcelStatus.UNKNOWN)
    return ParcelStatus.UNKNOWN


def _raw_status_text(event: dict) -> str | None:
    """Return ``"<code> <name>"`` for an event, from whichever parts exist."""
    parts = [
        str(part).strip()
        for part in (event.get("event"), event.get("eventName"))
        if part is not None and str(part).strip()
    ]
    return " ".join(parts) or None


def build_history(
    classified: list[tuple[dict, ParcelStatus | None]],
    *,
    max_events: int = HISTORY_MAX_EVENTS,
) -> list[dict]:
    """Build the canonical ``history`` list from classified events.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. The endpoint answers oldest to newest and
    that order is kept; only the most recent ``max_events`` are returned.
    """
    entries: list[dict] = []
    for event, status in classified:
        raw_date = event.get("date")
        if not isinstance(raw_date, str) or not raw_date.strip():
            _warn_unproven_shape("event without a date")
            continue
        timestamp = _carrier_iso(raw_date)
        if timestamp is None:
            _warn_unproven_shape("unparseable event date")
            continue
        entries.append(
            {
                "timestamp": timestamp,
                "status": status,
                "raw_status": _raw_status_text(event),
            }
        )
    return entries[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=quote(tracking_code, safe=""))


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict.

    ``raw`` is the payload from the API client:
    ``{"trackingNumber": ..., "events": [...]}`` with events oldest first.
    The parcel is described by its final event. Fields the event schema does
    not carry are ``None``; ``raw`` carries the full record untrimmed.
    """
    classified = classify_events(raw.get("events"))
    final_event = classified[-1][0] if classified else {}
    status = resolve_status(classified)
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = None
    if delivered:
        delivered_event = next(
            event
            for event, event_status in reversed(classified)
            if event_status is ParcelStatus.DELIVERED
        )
        delivered_at = _carrier_iso(delivered_event.get("date"))
        if delivered_at is None:
            _warn_unproven_shape("delivered event without a usable date")

    barcode = final_event.get("barcode") or raw.get("trackingNumber")

    return {
        "carrier": "Ukrposhta",
        "barcode": barcode,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": _raw_status_text(final_event),
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": None,
        "pickup_point": None,
        "url": tracking_url(barcode),
        "weight": None,
        "dimensions": None,
        "history": build_history(classified) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
