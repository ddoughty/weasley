"""Tests for daemon failure tracking and abort behavior."""

from unittest.mock import MagicMock, patch

import pytest

from config import Config
from main import run_daemon


@pytest.fixture
def config():
    cfg = Config()
    cfg.poll_interval = 1
    cfg.max_consecutive_failures = 3
    cfg.pushover_user_key = "test-user"
    cfg.pushover_app_token = "test-token"
    return cfg


class TestRunDaemon:
    @patch("main.time.sleep", side_effect=InterruptedError)
    @patch("main.run_once", return_value=True)
    def test_success_resets_counter(self, mock_run_once, mock_sleep, config):
        """Successful run should not trigger abort."""
        with pytest.raises(InterruptedError):
            run_daemon(config)

        mock_run_once.assert_called_once()

    @patch("main.time.sleep")
    @patch("main.run_once")
    @patch("notifier.send_pushover", return_value=True)
    def test_aborts_after_max_failures(
        self, mock_pushover, mock_run_once, mock_sleep, config
    ):
        """Daemon should exit after max_consecutive_failures."""
        mock_run_once.return_value = False

        with pytest.raises(SystemExit) as exc_info:
            run_daemon(config)

        assert exc_info.value.code == 1
        assert mock_run_once.call_count == 3
        mock_pushover.assert_called_once()
        assert "Auth Failure" in mock_pushover.call_args[0][1]

    @patch("main.time.sleep")
    @patch("main.run_once")
    @patch("notifier.send_pushover", return_value=True)
    def test_success_resets_failure_count(
        self, mock_pushover, mock_run_once, mock_sleep, config
    ):
        """A success in the middle resets the consecutive failure counter."""
        # Fail twice, succeed, fail twice, succeed, then KeyboardInterrupt
        call_count = 0
        results = [False, False, True, False, False, True]

        def side_effect_run_once(_config):
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx < len(results):
                return results[idx]
            raise KeyboardInterrupt

        mock_run_once.side_effect = side_effect_run_once

        with pytest.raises(KeyboardInterrupt):
            run_daemon(config)

        # Should never have hit the abort threshold
        mock_pushover.assert_not_called()

    @patch("main.time.sleep")
    @patch("main.run_once", side_effect=RuntimeError("boom"))
    @patch("notifier.send_pushover", return_value=True)
    def test_exceptions_count_as_failures(
        self, mock_pushover, mock_run_once, mock_sleep, config
    ):
        """Unhandled exceptions in run_once should count as failures."""
        with pytest.raises(SystemExit) as exc_info:
            run_daemon(config)

        assert exc_info.value.code == 1
        assert mock_run_once.call_count == 3
        mock_pushover.assert_called_once()
