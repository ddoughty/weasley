"""Tests for the Pushover notifier module."""

from unittest.mock import MagicMock, patch

import pytest

from config import Config
from notifier import send_pushover


@pytest.fixture
def config():
    cfg = Config()
    cfg.pushover_user_key = "test-user-key"
    cfg.pushover_app_token = "test-app-token"
    return cfg


@pytest.fixture
def config_no_pushover():
    return Config()


class TestSendPushover:
    def test_success(self, config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("notifier.requests.post", return_value=mock_resp) as mock_post:
            result = send_pushover(config, "Test Title", "Test message")

        assert result is True
        mock_post.assert_called_once()
        call_data = mock_post.call_args[1]["data"]
        assert call_data["token"] == "test-app-token"
        assert call_data["user"] == "test-user-key"
        assert call_data["title"] == "Test Title"
        assert call_data["message"] == "Test message"
        assert call_data["priority"] == 1

    def test_not_configured(self, config_no_pushover):
        with patch("notifier.requests.post") as mock_post:
            result = send_pushover(config_no_pushover, "Title", "Message")

        assert result is False
        mock_post.assert_not_called()

    def test_api_error(self, config):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "invalid token"
        with patch("notifier.requests.post", return_value=mock_resp):
            result = send_pushover(config, "Title", "Message")

        assert result is False

    def test_request_exception(self, config):
        import requests

        with patch(
            "notifier.requests.post", side_effect=requests.ConnectionError("timeout")
        ):
            result = send_pushover(config, "Title", "Message")

        assert result is False

    def test_custom_priority(self, config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("notifier.requests.post", return_value=mock_resp) as mock_post:
            send_pushover(config, "Title", "Message", priority=0)

        assert mock_post.call_args[1]["data"]["priority"] == 0
