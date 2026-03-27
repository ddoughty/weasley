"""Tests for the SQS publisher module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from publisher import publish_locations


@pytest.fixture
def config():
    """Config with SQS queue URL set."""
    cfg = Config()
    cfg.sqs_queue_url = "https://sqs.us-east-1.amazonaws.com/123456789/test-queue"
    return cfg


@pytest.fixture
def config_no_sqs():
    """Config without SQS queue URL."""
    return Config()


@pytest.fixture
def sample_locations():
    return [
        {
            "name": "Dennis",
            "device_name": "Dennis's iPhone",
            "lat": 42.3601,
            "lon": -71.0589,
            "timestamp": 1700000000000,
            "battery_level": 0.85,
            "battery_status": "Charging",
        },
        {
            "name": "Steph",
            "device_name": "Steph's iPhone",
            "lat": 42.3505,
            "lon": -71.0648,
            "timestamp": 1700000001000,
            "battery_level": 0.42,
            "battery_status": None,
        },
    ]


def test_publish_sends_one_message_per_member(config, sample_locations):
    mock_client = MagicMock()
    sent = publish_locations(config, sample_locations, sqs_client=mock_client)

    assert sent == 2
    assert mock_client.send_message.call_count == 2

    # Verify first message
    first_call = mock_client.send_message.call_args_list[0]
    assert first_call.kwargs["QueueUrl"] == config.sqs_queue_url
    body = json.loads(first_call.kwargs["MessageBody"])
    assert body["person"] == "Dennis"
    assert body["device_name"] == "Dennis's iPhone"
    assert body["lat"] == 42.3601
    assert body["lon"] == -71.0589
    assert body["timestamp"] == 1700000000000
    assert body["battery_level"] == 0.85
    assert body["battery_status"] == "Charging"
    assert "published_at" in body

    # Verify second message
    second_call = mock_client.send_message.call_args_list[1]
    body2 = json.loads(second_call.kwargs["MessageBody"])
    assert body2["person"] == "Steph"


def test_publish_skips_when_no_queue_url(config_no_sqs, sample_locations):
    mock_client = MagicMock()
    sent = publish_locations(config_no_sqs, sample_locations, sqs_client=mock_client)

    assert sent == 0
    mock_client.send_message.assert_not_called()


def test_publish_skips_when_boto3_missing(config, sample_locations):
    with patch("publisher._HAS_BOTO3", False):
        sent = publish_locations(config, sample_locations)
        assert sent == 0


def test_publish_continues_on_partial_failure(config, sample_locations):
    mock_client = MagicMock()
    mock_client.send_message.side_effect = [None, Exception("network error")]

    sent = publish_locations(config, sample_locations, sqs_client=mock_client)

    assert sent == 1
    assert mock_client.send_message.call_count == 2


def test_publish_empty_locations(config):
    mock_client = MagicMock()
    sent = publish_locations(config, [], sqs_client=mock_client)

    assert sent == 0
    mock_client.send_message.assert_not_called()


def test_publish_handles_missing_optional_fields(config):
    locations = [
        {
            "name": "Dennis",
            "device_name": "Dennis's iPhone",
            "lat": 42.36,
            "lon": -71.06,
            "timestamp": 1700000000000,
        }
    ]
    mock_client = MagicMock()
    sent = publish_locations(config, locations, sqs_client=mock_client)

    assert sent == 1
    body = json.loads(mock_client.send_message.call_args.kwargs["MessageBody"])
    assert body["battery_level"] is None
    assert body["battery_status"] is None
    assert body["accuracy"] is None
