"""Tests for the TRMNL consumer Lambda handler."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud"))


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("TRMNL_API_KEY", "test-api-key")
    monkeypatch.setenv("TRMNL_PLUGIN_UUID", "test-uuid-123")
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    monkeypatch.setenv("LOCATIONS_TABLE", "weasley-locations")


def _sns_event(message: dict) -> dict:
    """Build an SNS event wrapping the given message."""
    return {
        "Records": [
            {
                "Sns": {
                    "Message": json.dumps(message),
                }
            }
        ]
    }


SAMPLE_LOCATIONS = [
    {
        "person": "Dennis",
        "lat": 42.3601,
        "lon": -71.0589,
        "location_label": "Home",
        "timestamp": 1700000000000,
        "battery_level": 0.85,
        "battery_status": "Charging",
    },
    {
        "person": "Steph",
        "lat": 42.3501,
        "lon": -71.0689,
        "location_label": "Office",
        "timestamp": 1700000100000,
        "battery_level": 0.42,
        "battery_status": "Unplugged",
    },
]


class TestBuildPayload:
    def test_builds_members_from_locations(self):
        from trmnl_consumer.handler import _build_payload

        payload = _build_payload(SAMPLE_LOCATIONS)
        members = payload["merge_variables"]["members"]
        assert len(members) == 2
        assert members[0]["name"] == "Dennis"
        assert members[0]["location_label"] == "Home"
        assert members[1]["name"] == "Steph"
        assert members[1]["location_label"] == "Office"

    def test_member_count(self):
        from trmnl_consumer.handler import _build_payload

        payload = _build_payload(SAMPLE_LOCATIONS)
        assert payload["merge_variables"]["member_count"] == 2

    def test_updated_at_present(self):
        from trmnl_consumer.handler import _build_payload

        payload = _build_payload(SAMPLE_LOCATIONS)
        assert "updated_at" in payload["merge_variables"]
        assert len(payload["merge_variables"]["updated_at"]) > 0

    def test_empty_locations(self):
        from trmnl_consumer.handler import _build_payload

        payload = _build_payload([])
        assert payload["merge_variables"]["members"] == []
        assert payload["merge_variables"]["member_count"] == 0


class TestFormatBattery:
    def test_normal_level(self):
        from trmnl_consumer.handler import _format_battery

        assert _format_battery(0.85) == "85%"

    def test_full_battery(self):
        from trmnl_consumer.handler import _format_battery

        assert _format_battery(1.0) == "100%"

    def test_none_battery(self):
        from trmnl_consumer.handler import _format_battery

        assert _format_battery(None) == "?"


class TestFormatTimestamp:
    def test_valid_timestamp(self):
        from trmnl_consumer.handler import _format_timestamp

        result = _format_timestamp(1700000000000, "UTC")
        assert ":" in result  # Should be a time string

    def test_none_timestamp(self):
        from trmnl_consumer.handler import _format_timestamp

        assert _format_timestamp(None) == "Unknown"


class TestPushToTrmnl:
    @patch("trmnl_consumer.handler.http")
    def test_successful_push(self, mock_http):
        from trmnl_consumer.handler import _push_to_trmnl

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_http.request.return_value = mock_resp

        result = _push_to_trmnl({"merge_variables": {}})
        assert result is True
        mock_http.request.assert_called_once()
        call_args = mock_http.request.call_args
        assert call_args[0][0] == "POST"
        assert "test-uuid-123" in call_args[0][1]

    @patch("trmnl_consumer.handler.http")
    def test_failed_push(self, mock_http):
        from trmnl_consumer.handler import _push_to_trmnl

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.data = b"Internal Server Error"
        mock_http.request.return_value = mock_resp

        result = _push_to_trmnl({"merge_variables": {}})
        assert result is False

    def test_missing_config_returns_false(self, monkeypatch):
        from trmnl_consumer.handler import _push_to_trmnl

        monkeypatch.setattr("trmnl_consumer.handler.TRMNL_API_KEY", "")
        result = _push_to_trmnl({"merge_variables": {}})
        assert result is False


class TestLambdaHandler:
    @patch("trmnl_consumer.handler._push_to_trmnl")
    @patch("trmnl_consumer.handler.get_all_locations")
    def test_pushes_all_locations(self, mock_get_all, mock_push):
        from trmnl_consumer.handler import lambda_handler

        mock_get_all.return_value = SAMPLE_LOCATIONS
        mock_push.return_value = True

        event = _sns_event({"person": "Dennis", "trigger": "movement"})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_push.assert_called_once()
        payload = mock_push.call_args[0][0]
        assert len(payload["merge_variables"]["members"]) == 2

    @patch("trmnl_consumer.handler._push_to_trmnl")
    @patch("trmnl_consumer.handler.get_all_locations")
    def test_no_locations_skips_push(self, mock_get_all, mock_push):
        from trmnl_consumer.handler import lambda_handler

        mock_get_all.return_value = []

        event = _sns_event({"person": "Dennis", "trigger": "movement"})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_push.assert_not_called()

    @patch("trmnl_consumer.handler._push_to_trmnl")
    @patch("trmnl_consumer.handler.get_all_locations")
    def test_push_failure_returns_502(self, mock_get_all, mock_push):
        from trmnl_consumer.handler import lambda_handler

        mock_get_all.return_value = SAMPLE_LOCATIONS
        mock_push.return_value = False

        event = _sns_event({"person": "Dennis", "trigger": "movement"})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 502
