"""
Location Processor Lambda — consumes raw location events from SQS,
resolves location labels, detects movement vs heartbeat, stores state
in DynamoDB, and publishes enriched events to SNS.
"""

import json
import logging
import os
import time

import boto3
import requests

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.dynamo import (
    cache_key,
    get_geocode_cache,
    get_location,
    haversine_m,
    lookup_manual_place,
    put_geocode_cache,
    put_location,
)
from shared.models import EnrichedLocationEvent, RawLocationEvent

log = logging.getLogger()
log.setLevel(logging.INFO)

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
DISTANCE_THRESHOLD_M = float(os.environ.get("DISTANCE_THRESHOLD_M", "200"))
HEARTBEAT_INTERVAL_S = int(os.environ.get("HEARTBEAT_INTERVAL_S", "3600"))
AMAZON_PLACES_API_KEY = os.environ.get("AMAZON_PLACES_API_KEY", "")
AMAZON_PLACES_REGION = os.environ.get("AMAZON_PLACES_REGION", "us-east-1")
CACHE_PRECISION = int(os.environ.get("CACHE_PRECISION", "4"))

_sns = None


def _get_sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def lambda_handler(event, context):
    """Process a batch of SQS records containing RawLocationEvents."""
    records = event.get("Records", [])
    log.info("Processing %d SQS records", len(records))

    for record in records:
        try:
            body = json.loads(record["body"])
            raw = RawLocationEvent.from_dict(body)
            _process_one(raw)
        except Exception:
            log.exception("Failed to process record: %s", record.get("messageId"))
            raise  # Let SQS retry via visibility timeout


def _process_one(raw: RawLocationEvent) -> None:
    """Process a single raw location event."""
    previous = get_location(raw.person)

    # Resolve location label
    label = _resolve_label(raw.lat, raw.lon, raw.person)

    # Determine trigger type
    previous_label = previous.get("location_label") if previous else None
    trigger, distance = _determine_trigger(raw, previous)

    if trigger is None:
        log.info(
            "Skipping %s — no movement (%.0fm) and heartbeat not due",
            raw.person,
            distance or 0,
        )
        # Still update the stored location so timestamp stays fresh
        _store_location(raw, label)
        return

    # Build enriched event
    enriched = EnrichedLocationEvent(
        person=raw.person,
        lat=raw.lat,
        lon=raw.lon,
        location_label=label,
        previous_label=previous_label,
        trigger=trigger,
        timestamp=raw.timestamp,
        accuracy=raw.accuracy,
        battery_level=raw.battery_level,
        battery_status=raw.battery_status,
        distance_moved_m=round(distance, 1) if distance else None,
    )

    # Store updated location
    _store_location(raw, label)

    # Publish to SNS
    _publish_enriched(enriched)

    log.info(
        "Processed %s: trigger=%s label=%r distance=%.0fm",
        raw.person,
        trigger,
        label,
        distance or 0,
    )


def _resolve_label(lat: float, lon: float, person: str) -> str:
    """Resolve a location label: manual places -> geocode cache -> Amazon Places API."""
    # 1. Check manual places
    manual = lookup_manual_place(lat, lon, for_user=person)
    if manual:
        return manual

    # 2. Check geocode cache
    key = cache_key(lat, lon, precision=CACHE_PRECISION)
    cached = get_geocode_cache(key)
    if cached:
        return cached

    # 3. Call Amazon Places API
    label = _reverse_geocode_amazon(lat, lon)
    if label:
        put_geocode_cache(key, label, source="amazon")
        return label

    return "Unknown"


def _reverse_geocode_amazon(lat: float, lon: float) -> str | None:
    """Call Amazon Location Service reverse geocode API."""
    if not AMAZON_PLACES_API_KEY:
        return None

    endpoint = (
        f"https://places.geo.{AMAZON_PLACES_REGION}.amazonaws.com/v2/reverse-geocode"
    )
    body = {
        "QueryPosition": [lon, lat],
        "MaxResults": 1,
        "IntendedUse": "Storage",
    }

    try:
        resp = requests.post(
            endpoint,
            params={"key": AMAZON_PLACES_API_KEY},
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(
                "Amazon Places reverse geocode returned %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return None

        data = resp.json()
        return _extract_label(data)
    except Exception:
        log.exception("Amazon Places reverse geocode failed")
        return None


def _extract_label(data: dict) -> str | None:
    """Extract a human-readable label from Amazon Places API response."""
    for list_key in ("ResultItems", "Results", "results"):
        items = data.get(list_key)
        if isinstance(items, list) and items:
            entry = items[0]
            for path in [
                ("Title",),
                ("title",),
                ("Address", "Label"),
                ("Place", "Label"),
                ("Place", "Address", "Label"),
                ("formattedAddress",),
            ]:
                val = entry
                for key in path:
                    if isinstance(val, dict):
                        val = val.get(key)
                    else:
                        val = None
                        break
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return None


def _determine_trigger(
    raw: RawLocationEvent, previous: dict | None
) -> tuple[str | None, float | None]:
    """Determine if this event should trigger a notification.

    Returns (trigger_type, distance_meters) or (None, distance) if no trigger.
    """
    if previous is None:
        # First time seeing this person — always trigger
        return EnrichedLocationEvent.TRIGGER_MOVEMENT, None

    prev_lat = previous.get("lat")
    prev_lon = previous.get("lon")
    if prev_lat is None or prev_lon is None:
        return EnrichedLocationEvent.TRIGGER_MOVEMENT, None

    distance = haversine_m(raw.lat, raw.lon, prev_lat, prev_lon)

    if distance >= DISTANCE_THRESHOLD_M:
        return EnrichedLocationEvent.TRIGGER_MOVEMENT, distance

    # Check heartbeat interval
    prev_timestamp = previous.get("updated_at", 0)
    now = int(time.time())
    if (now - prev_timestamp) >= HEARTBEAT_INTERVAL_S:
        return EnrichedLocationEvent.TRIGGER_HEARTBEAT, distance

    return None, distance


def _store_location(raw: RawLocationEvent, label: str) -> None:
    """Write the current location to DynamoDB."""
    put_location(
        raw.person,
        {
            "lat": raw.lat,
            "lon": raw.lon,
            "location_label": label,
            "timestamp": raw.timestamp,
            "battery_level": raw.battery_level,
            "battery_status": raw.battery_status,
            "device_name": raw.device_name,
            "updated_at": int(time.time()),
        },
    )


def _publish_enriched(enriched: EnrichedLocationEvent) -> None:
    """Publish an enriched location event to SNS."""
    if not SNS_TOPIC_ARN:
        log.warning("SNS_TOPIC_ARN not set, skipping publish")
        return

    _get_sns().publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=json.dumps(enriched.to_dict()),
        Subject=f"Location update: {enriched.person}",
    )
