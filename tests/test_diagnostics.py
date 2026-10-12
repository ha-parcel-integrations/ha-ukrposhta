"""Tests for Ukrposhta diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.ukrposhta.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "EXAMPLE123456"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "EXAMPLE123456",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "in_transit",
            "raw_status": "20700 Processed",
            "delivered_at": "2026-04-29T13:12:42",
            "history": [{"timestamp": "2026-04-27T23:03:58", "status": "registered"}],
            "raw": {"events": [{"name": "Jane Doe", "index": "00000"}]},
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"] == "**REDACTED**"
    assert result["incoming"][0]["raw_status"] == "**REDACTED**"
    assert result["incoming"][0]["delivered_at"] == "**REDACTED**"
    assert result["incoming"][0]["history"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "in_transit"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }


async def test_diagnostics_redacts_wire_fields_at_any_depth(hass):
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = [
        {
            "nested": [
                {
                    "name": "x",
                    "index": "00000",
                    "country": "x",
                    "eventReason": "x",
                    "eventReason_id": 1,
                    "indexOrder": 1,
                    "mailType": "x",
                    "value": 5,
                    "step": 1,
                    "date": "x",
                }
            ]
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert set(result["incoming"][0]["nested"][0].values()) == {"**REDACTED**"}
