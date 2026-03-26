"""Tests for the location processor Lambda handler."""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# Ensure cloud/ is on the path for shared imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud"))

from shared.models import EnrichedLocationEvent, RawLocationEvent


# We need to mock dynamo before importing the handler
@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:test-topic")
    monkeypatch.setenv("DISTANCE_THRESHOLD_M", "200")
    monkeypatch.setenv("HEARTBEAT_INTERVAL_S", "3600")
    monkeypatch.setenv("AMAZON_PLACES_API_KEY", "test-key")
    monkeypatch.setenv("CACHE_PRECISION", "4")


def _make_sqs_event(raw_events: list[RawLocationEvent]) -> dict:
    """Build an SQS event with the given raw events as records."""
    return {
        "Records": [
            {
                "messageId": f"msg-{i}",
                "body": json.dumps(evt.to_dict()),
            }
            for i, evt in enumerate(raw_events)
        ]
    }


def _raw_event(person="Dennis", lat=42.3601, lon=-71.0589, ts=1700000000000):
    return RawLocationEvent(
        person=person,
        device_name=f"{person}'s iPhone",
        lat=lat,
        lon=lon,
        timestamp=ts,
        battery_level=0.85,
        battery_status="Charging",
    )


class TestDetermineTrigger:
    def test_first_time_person_triggers_movement(self):
        from cloud.location_processor.handler import _determine_trigger

        raw = _raw_event()
        trigger, distance = _determine_trigger(raw, None)
        assert trigger == "movement"
        assert distance is None

    def test_large_movement_triggers(self):
        from cloud.location_processor.handler import _determine_trigger

        raw = _raw_event(lat=42.37, lon=-71.06)  # ~1km away
        previous = {"lat": 42.36, "lon": -71.06, "updated_at": int(time.time())}
        trigger, distance = _determine_trigger(raw, previous)
        assert trigger == "movement"
        assert distance > 200

    def test_small_movement_no_trigger(self):
        from cloud.location_processor.handler import _determine_trigger

        raw = _raw_event(lat=42.3601, lon=-71.0589)
        previous = {
            "lat": 42.3602,
            "lon": -71.0589,
            "updated_at": int(time.time()),
        }
        trigger, distance = _determine_trigger(raw, previous)
        assert trigger is None
        assert distance < 200

    def test_heartbeat_triggers_after_interval(self):
        from cloud.location_processor.handler import _determine_trigger

        raw = _raw_event(lat=42.3601, lon=-71.0589)
        previous = {
            "lat": 42.3602,
            "lon": -71.0589,
            "updated_at": int(time.time()) - 4000,  # Over 3600s ago
        }
        trigger, distance = _determine_trigger(raw, previous)
        assert trigger == "heartbeat"


class TestResolveLabel:
    @patch("cloud.location_processor.handler.lookup_manual_place")
    def test_manual_place_takes_priority(self, mock_lookup):
        from cloud.location_processor.handler import _resolve_label

        mock_lookup.return_value = "Home"
        label = _resolve_label(42.36, -71.06, "Dennis")
        assert label == "Home"
        mock_lookup.assert_called_once_with(42.36, -71.06, for_user="Dennis")

    @patch("cloud.location_processor.handler._reverse_geocode_amazon")
    @patch("cloud.location_processor.handler.get_geocode_cache")
    @patch("cloud.location_processor.handler.lookup_manual_place")
    def test_geocode_cache_hit(self, mock_manual, mock_cache, mock_amazon):
        from cloud.location_processor.handler import _resolve_label

        mock_manual.return_value = None
        mock_cache.return_value = "123 Main St"
        label = _resolve_label(42.36, -71.06, "Dennis")
        assert label == "123 Main St"
        mock_amazon.assert_not_called()

    @patch("cloud.location_processor.handler.put_geocode_cache")
    @patch("cloud.location_processor.handler._reverse_geocode_amazon")
    @patch("cloud.location_processor.handler.get_geocode_cache")
    @patch("cloud.location_processor.handler.lookup_manual_place")
    def test_amazon_api_fallback(self, mock_manual, mock_cache, mock_amazon, mock_put):
        from cloud.location_processor.handler import _resolve_label

        mock_manual.return_value = None
        mock_cache.return_value = None
        mock_amazon.return_value = "456 Oak Ave"
        label = _resolve_label(42.36, -71.06, "Dennis")
        assert label == "456 Oak Ave"
        mock_put.assert_called_once()

    @patch("cloud.location_processor.handler._reverse_geocode_amazon")
    @patch("cloud.location_processor.handler.get_geocode_cache")
    @patch("cloud.location_processor.handler.lookup_manual_place")
    def test_all_sources_miss_returns_unknown(
        self, mock_manual, mock_cache, mock_amazon
    ):
        from cloud.location_processor.handler import _resolve_label

        mock_manual.return_value = None
        mock_cache.return_value = None
        mock_amazon.return_value = None
        label = _resolve_label(42.36, -71.06, "Dennis")
        assert label == "Unknown"


class TestExtractLabel:
    def test_result_items_title(self):
        from cloud.location_processor.handler import _extract_label

        data = {"ResultItems": [{"Title": "123 Main St, Boston, MA"}]}
        assert _extract_label(data) == "123 Main St, Boston, MA"

    def test_results_place_label(self):
        from cloud.location_processor.handler import _extract_label

        data = {"Results": [{"Place": {"Label": "456 Oak Ave"}}]}
        assert _extract_label(data) == "456 Oak Ave"

    def test_empty_results(self):
        from cloud.location_processor.handler import _extract_label

        assert _extract_label({}) is None
        assert _extract_label({"ResultItems": []}) is None


class TestLambdaHandler:
    @patch("cloud.location_processor.handler._publish_enriched")
    @patch("cloud.location_processor.handler._store_location")
    @patch("cloud.location_processor.handler._resolve_label")
    @patch("cloud.location_processor.handler.get_location")
    def test_new_person_triggers_movement(
        self, mock_get_loc, mock_resolve, mock_store, mock_publish
    ):
        from cloud.location_processor.handler import lambda_handler

        mock_get_loc.return_value = None
        mock_resolve.return_value = "Home"

        raw = _raw_event()
        event = _make_sqs_event([raw])
        lambda_handler(event, None)

        mock_store.assert_called_once()
        mock_publish.assert_called_once()
        enriched = mock_publish.call_args[0][0]
        assert enriched.person == "Dennis"
        assert enriched.trigger == "movement"
        assert enriched.location_label == "Home"

    @patch("cloud.location_processor.handler._publish_enriched")
    @patch("cloud.location_processor.handler._store_location")
    @patch("cloud.location_processor.handler._resolve_label")
    @patch("cloud.location_processor.handler.get_location")
    def test_no_movement_skips_publish(
        self, mock_get_loc, mock_resolve, mock_store, mock_publish
    ):
        from cloud.location_processor.handler import lambda_handler

        mock_get_loc.return_value = {
            "lat": 42.3601,
            "lon": -71.0589,
            "updated_at": int(time.time()),
        }
        mock_resolve.return_value = "Home"

        raw = _raw_event()
        event = _make_sqs_event([raw])
        lambda_handler(event, None)

        mock_store.assert_called_once()  # Still stores
        mock_publish.assert_not_called()  # But doesn't publish

    @patch("cloud.location_processor.handler._publish_enriched")
    @patch("cloud.location_processor.handler._store_location")
    @patch("cloud.location_processor.handler._resolve_label")
    @patch("cloud.location_processor.handler.get_location")
    def test_processes_batch(
        self, mock_get_loc, mock_resolve, mock_store, mock_publish
    ):
        from cloud.location_processor.handler import lambda_handler

        mock_get_loc.return_value = None
        mock_resolve.return_value = "Office"

        events = [_raw_event("Dennis"), _raw_event("Steph")]
        sqs_event = _make_sqs_event(events)
        lambda_handler(sqs_event, None)

        assert mock_publish.call_count == 2
        assert mock_store.call_count == 2
