"""
SQS publisher for the Weasley cloud pipeline.

Publishes one RawLocationEvent per family member per poll cycle.
Gracefully degrades if boto3 is not installed or SQS is unreachable.
"""

import json
import logging
from typing import Optional

from config import Config

log = logging.getLogger("weasley.publisher")

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError

    _HAS_BOTO3 = True
except ImportError:
    _HAS_BOTO3 = False


def _make_sqs_client(region: str = "us-east-1"):
    """Create an SQS client. Separated for testability."""
    return boto3.client("sqs", region_name=region)


def publish_locations(
    config: Config,
    locations: list[dict],
    sqs_client=None,
) -> int:
    """Publish each location as a RawLocationEvent to SQS.

    Returns the number of messages successfully sent.
    """
    if not config.sqs_queue_url:
        log.debug("SQS queue URL not configured, skipping publish.")
        return 0

    if not _HAS_BOTO3:
        log.warning("boto3 is not installed — skipping SQS publish.")
        return 0

    # Import here so the module loads even without cloud/ on sys.path at import time
    import sys
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "cloud"))
    from shared.models import RawLocationEvent

    if sqs_client is None:
        sqs_client = _make_sqs_client()

    sent = 0
    for member in locations:
        event = RawLocationEvent(
            person=member.get("name", ""),
            device_name=member.get("device_name", ""),
            lat=member.get("lat", 0.0),
            lon=member.get("lon", 0.0),
            timestamp=member.get("timestamp", 0),
            accuracy=member.get("accuracy"),
            battery_level=member.get("battery_level"),
            battery_status=member.get("battery_status"),
        )

        try:
            sqs_client.send_message(
                QueueUrl=config.sqs_queue_url,
                MessageBody=json.dumps(event.to_dict()),
            )
            sent += 1
            log.debug("Published SQS event for %s", event.person)
        except Exception as exc:
            log.warning("Failed to publish SQS event for %s: %s", event.person, exc)

    if sent:
        log.info("Published %d/%d location events to SQS.", sent, len(locations))
    return sent
