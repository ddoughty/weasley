"""Tests for Playwright browser launch configuration."""

from unittest.mock import MagicMock

from auth import WeasleyAuth
from config import Config


def test_launch_browser_context_uses_installed_chrome():
    config = Config()
    config.session_dir = "/tmp/weasley-test-session"
    auth = WeasleyAuth(config)
    playwright = MagicMock()

    context = auth._launch_browser_context(playwright, headless=True)

    assert context is playwright.chromium.launch_persistent_context.return_value
    playwright.chromium.launch_persistent_context.assert_called_once_with(
        user_data_dir=config.session_dir,
        channel="chrome",
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-site-isolation-trials",
        ],
    )
