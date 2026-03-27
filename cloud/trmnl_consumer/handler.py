"""
TRMNL Consumer Lambda — receives batched location events via SQS
and pushes updated family locations to the TRMNL e-ink display.

An SQS buffer queue sits between the SNS topic and this Lambda,
with a 15-second batch window. This collapses multiple per-member
updates from a single polling cycle into one TRMNL push.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import urllib3

from shared.dynamo import get_all_locations

log = logging.getLogger()
log.setLevel(logging.INFO)

TRMNL_API_KEY = os.environ.get("TRMNL_API_KEY", "")
TRMNL_PLUGIN_UUID = os.environ.get("TRMNL_PLUGIN_UUID", "")
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "America/New_York")
TRMNL_WEBHOOK_URL = "https://trmnl.com/api/custom_plugins/{uuid}"

http = urllib3.PoolManager()


def lambda_handler(event, context):
    """SQS trigger: read all locations and push to TRMNL."""
    records = event.get("Records", [])
    log.info(
        "TRMNL consumer invoked with %d batched record(s) — single push", len(records)
    )

    locations = get_all_locations()
    if not locations:
        log.info("No locations in DynamoDB — skipping TRMNL push.")
        return {"statusCode": 200, "body": "no locations"}

    payload = _build_payload(locations)
    success = _push_to_trmnl(payload)

    return {
        "statusCode": 200 if success else 502,
        "body": "pushed" if success else "trmnl push failed",
    }


def _build_payload(locations: list[dict]) -> dict:
    """Build the TRMNL webhook payload matching the desktop trmnl.py format."""
    members = []
    for loc in locations:
        members.append(
            {
                "name": loc.get("person", ""),
                "lat": loc.get("lat"),
                "lon": loc.get("lon"),
                "battery_level": _format_battery(loc.get("battery_level")),
                "battery_status": loc.get("battery_status"),
                "last_seen": _format_timestamp(loc.get("timestamp"), DISPLAY_TIMEZONE),
                "location_label": loc.get("location_label", "Unknown"),
            }
        )

    return {
        "merge_variables": {
            "members": members,
            "updated_at": datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).strftime("%I:%M %p"),
            "member_count": len(members),
        }
    }


def _push_to_trmnl(payload: dict) -> bool:
    """POST the payload to the TRMNL custom plugin webhook."""
    if not TRMNL_API_KEY or not TRMNL_PLUGIN_UUID:
        log.warning("TRMNL not configured (missing API key or plugin UUID).")
        return False

    url = TRMNL_WEBHOOK_URL.format(uuid=TRMNL_PLUGIN_UUID)
    try:
        resp = http.request(
            "POST",
            url,
            body=json.dumps(payload),
            headers={
                "Authorization": f"Bearer {TRMNL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )
        if resp.status in (200, 201):
            log.info("TRMNL push successful.")
            return True
        else:
            log.error("TRMNL push failed: %d %s", resp.status, resp.data.decode())
            return False
    except Exception as e:
        log.error("TRMNL push error: %s", e)
        return False


def _format_battery(level: Optional[float]) -> str:
    """Convert 0-1 float battery level to percentage string."""
    if level is None:
        return "?"
    return f"{int(level * 100)}%"


def _format_timestamp(ts: Optional[int], tz_name: str = "America/New_York") -> str:
    """Convert a millisecond epoch timestamp to a readable time string."""
    if ts is None:
        return "Unknown"
    try:
        dt = datetime.fromtimestamp(ts / 1000, tz=ZoneInfo(tz_name))
        return dt.strftime("%I:%M %p")
    except Exception:
        return "Unknown"
