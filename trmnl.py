"""
Weasley TRMNL integration — pushes family location data to a TRMNL display.

Uses TRMNL's custom plugin / webhook API.
See: https://docs.usetrmnl.com/go/private-plugins/create-a-plugin
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from config import Config
from geocoder import ReverseGeocoder

log = logging.getLogger("weasley.trmnl")

TRMNL_WEBHOOK_URL = "https://trmnl.com/api/custom_plugins/{uuid}"


class WeasleyTRMNL:
    def __init__(self, config: Config):
        self.config = config
        self.geocoder = ReverseGeocoder(config)

    def push(self, locations: list[dict]) -> bool:
        """
        Push location data to TRMNL. Returns True on success.

        The payload shape here is a starting point — adjust to match
        whatever Liquid template you build for the TRMNL plugin.
        """
        if not self.config.trmnl_api_key or not self.config.trmnl_plugin_uuid:
            log.info("TRMNL not configured (missing api_key or plugin_uuid). Skipping push.")
            return False

        payload = self._build_payload(locations)
        url = TRMNL_WEBHOOK_URL.format(uuid=self.config.trmnl_plugin_uuid)

        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.config.trmnl_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                log.info("TRMNL push successful.")
                return True
            else:
                log.error(f"TRMNL push failed: {resp.status_code} {resp.text}")
                return False

        except Exception as e:
            log.error(f"TRMNL push error: {e}")
            return False

    def _build_payload(self, locations: list[dict]) -> dict:
        """
        Build the TRMNL webhook payload.

        TRMNL custom plugins accept a 'merge_variables' dict that gets
        passed to your Liquid template. Structure this however your
        template needs it.
        """
        members = []
        for loc in locations:
            members.append({
                "name": loc["name"],
                "lat": loc.get("lat"),
                "lon": loc.get("lon"),
                "battery_level": _format_battery(loc.get("battery_level")),
                "battery_status": loc.get("battery_status"),
                "last_seen": _format_timestamp(loc.get("timestamp")),
                "location_label": (
                    loc.get("location_label")
                    or self.geocoder.resolve_label(loc.get("lat"), loc.get("lon"))
                ),
            })

        return {
            "merge_variables": {
                "members": members,
                "updated_at": datetime.now(timezone.utc).strftime("%I:%M %p"),
                "member_count": len(members),
            }
        }


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------

def _format_battery(level: Optional[float]) -> str:
    if level is None:
        return "?"
    return f"{int(level * 100)}%"


def _format_timestamp(ts: Optional[int]) -> str:
    """Convert a millisecond epoch timestamp to a readable time string."""
    if ts is None:
        return "Unknown"
    try:
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        return dt.strftime("%I:%M %p")
    except Exception:
        return "Unknown"
