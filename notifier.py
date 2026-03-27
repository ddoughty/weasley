"""
Pushover push notifications for critical Weasley alerts.

Sends notifications when the daemon needs manual intervention
(e.g. auth session exhausted, repeated failures).
"""

import logging

import requests

from config import Config

log = logging.getLogger("weasley.notifier")

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


def send_pushover(config: Config, title: str, message: str, priority: int = 1) -> bool:
    """Send a Pushover notification. Returns True on success.

    Priority levels:
      -2  no notification
      -1  quiet
       0  normal
       1  high (bypasses quiet hours)
    """
    if not config.pushover_user_key or not config.pushover_app_token:
        log.warning("Pushover not configured — skipping notification.")
        return False

    try:
        resp = requests.post(
            PUSHOVER_API_URL,
            data={
                "token": config.pushover_app_token,
                "user": config.pushover_user_key,
                "title": title,
                "message": message,
                "priority": priority,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("Pushover notification sent: %s", title)
            return True
        else:
            log.warning("Pushover API returned %d: %s", resp.status_code, resp.text)
            return False
    except requests.RequestException as exc:
        log.warning("Failed to send Pushover notification: %s", exc)
        return False
