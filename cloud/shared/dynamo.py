"""
DynamoDB access helpers for the Weasley cloud pipeline.

Tables:
  - weasley-locations: current location per person (partition key: person)
  - weasley-places: manual place labels with radius (partition key: place_id)
  - weasley-geocode-cache: cached Amazon Places results (partition key: lat_lon_key)
"""

from __future__ import annotations

import math
import os
import time
import uuid
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

LOCATIONS_TABLE = os.environ.get("LOCATIONS_TABLE", "weasley-locations")
PLACES_TABLE = os.environ.get("PLACES_TABLE", "weasley-places")
GEOCODE_CACHE_TABLE = os.environ.get("GEOCODE_CACHE_TABLE", "weasley-geocode-cache")

_dynamodb = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _float_to_decimal(val):
    """Convert floats to Decimal for DynamoDB compatibility."""
    if isinstance(val, float):
        return Decimal(str(val))
    if isinstance(val, dict):
        return {k: _float_to_decimal(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_float_to_decimal(v) for v in val]
    return val


def _decimal_to_float(val):
    """Convert Decimal back to float when reading from DynamoDB."""
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, dict):
        return {k: _decimal_to_float(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_decimal_to_float(v) for v in val]
    return val


# ---------------------------------------------------------------------------
# Locations table
# ---------------------------------------------------------------------------


def get_location(person: str) -> Optional[dict]:
    """Get the current location for a person. Returns None if not found."""
    table = _get_dynamodb().Table(LOCATIONS_TABLE)
    resp = table.get_item(Key={"person": person})
    item = resp.get("Item")
    return _decimal_to_float(item) if item else None


def get_all_locations() -> list[dict]:
    """Get current locations for all tracked people."""
    table = _get_dynamodb().Table(LOCATIONS_TABLE)
    resp = table.scan()
    return [_decimal_to_float(item) for item in resp.get("Items", [])]


def put_location(person: str, data: dict) -> None:
    """Write or update a person's current location."""
    table = _get_dynamodb().Table(LOCATIONS_TABLE)
    item = _float_to_decimal({"person": person, **data})
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Places table
# ---------------------------------------------------------------------------


def get_all_places() -> list[dict]:
    """Get all manual place labels."""
    table = _get_dynamodb().Table(PLACES_TABLE)
    resp = table.scan()
    return [_decimal_to_float(item) for item in resp.get("Items", [])]


def get_place(place_id: str) -> Optional[dict]:
    """Get a single place by ID."""
    table = _get_dynamodb().Table(PLACES_TABLE)
    resp = table.get_item(Key={"place_id": place_id})
    item = resp.get("Item")
    return _decimal_to_float(item) if item else None


def create_place(
    name: str,
    lat: float,
    lon: float,
    radius_m: float = 200.0,
    user: Optional[str] = None,
) -> dict:
    """Create a new manual place label. Returns the created item."""
    table = _get_dynamodb().Table(PLACES_TABLE)
    item = _float_to_decimal(
        {
            "place_id": str(uuid.uuid4()),
            "name": name,
            "lat": lat,
            "lon": lon,
            "radius_m": radius_m,
            "created_at": int(time.time()),
        }
    )
    if user:
        item["user"] = user
    table.put_item(Item=item)
    return _decimal_to_float(item)


def update_place(place_id: str, updates: dict) -> Optional[dict]:
    """Update an existing place. Returns the updated item or None if not found."""
    table = _get_dynamodb().Table(PLACES_TABLE)
    existing = table.get_item(Key={"place_id": place_id}).get("Item")
    if not existing:
        return None
    for key, value in updates.items():
        if key == "place_id":
            continue
        existing[key] = _float_to_decimal(value) if value is not None else value
    # Remove 'user' key entirely if set to None (makes it a global place)
    if "user" in updates and updates["user"] is None:
        existing.pop("user", None)
    table.put_item(Item=existing)
    return _decimal_to_float(existing)


def delete_place(place_id: str) -> bool:
    """Delete a place by ID. Returns True if deleted."""
    table = _get_dynamodb().Table(PLACES_TABLE)
    table.delete_item(Key={"place_id": place_id})
    return True


def find_place_by_name(name: str) -> Optional[dict]:
    """Find a place by name (case-insensitive scan). Returns first match."""
    places = get_all_places()
    name_lower = name.lower()
    for place in places:
        if place.get("name", "").lower() == name_lower:
            return place
    return None


def lookup_manual_place(
    lat: float, lon: float, for_user: Optional[str] = None
) -> Optional[str]:
    """
    Look up a manual place label by coordinates, respecting per-user overrides.

    Three-tier resolution:
      1. Per-user match for this person (closest within radius)
      2. Global match (closest within radius)
      3. Another user's place, auto-prefixed with their name
    """
    places = get_all_places()

    user_best: Optional[str] = None
    user_best_dist = float("inf")
    global_best: Optional[str] = None
    global_best_dist = float("inf")
    other_best: Optional[tuple[str, str]] = None
    other_best_dist = float("inf")

    for place in places:
        distance = haversine_m(lat, lon, place["lat"], place["lon"])
        if distance > place["radius_m"]:
            continue

        place_user = place.get("user")
        if place_user is None:
            if distance < global_best_dist:
                global_best_dist = distance
                global_best = place["name"]
        elif for_user and place_user == for_user:
            if distance < user_best_dist:
                user_best_dist = distance
                user_best = place["name"]
        else:
            if distance < other_best_dist:
                other_best_dist = distance
                other_best = (place_user, place["name"])

    if user_best is not None:
        return user_best
    if global_best is not None:
        return global_best
    if other_best is not None:
        owner, name = other_best
        return f"{owner}'s {name}"
    return None


# ---------------------------------------------------------------------------
# Geocode cache table
# ---------------------------------------------------------------------------

CACHE_TTL_DAYS = 30


def cache_key(lat: float, lon: float, precision: int = 4) -> str:
    """Round coordinates and build a cache key string."""
    precision = max(0, min(8, precision))
    return f"{round(lat, precision)}:{round(lon, precision)}"


def get_geocode_cache(lat_lon_key: str) -> Optional[str]:
    """Look up a cached geocode result. Returns the label or None."""
    table = _get_dynamodb().Table(GEOCODE_CACHE_TABLE)
    resp = table.get_item(Key={"lat_lon_key": lat_lon_key})
    item = resp.get("Item")
    if item:
        return item.get("label")
    return None


def put_geocode_cache(lat_lon_key: str, label: str, source: str = "amazon") -> None:
    """Cache a geocode result with TTL."""
    table = _get_dynamodb().Table(GEOCODE_CACHE_TABLE)
    table.put_item(
        Item={
            "lat_lon_key": lat_lon_key,
            "label": label,
            "source": source,
            "created_at": int(time.time()),
            "ttl": int(time.time()) + (CACHE_TTL_DAYS * 86400),
        }
    )


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------


def refresh_location_labels() -> list[dict]:
    """Re-resolve location labels for all tracked people using current places.

    Returns a list of dicts describing what changed:
      [{"person": "X", "old_label": "A", "new_label": "B"}, ...]
    """
    locations = get_all_locations()
    changes = []
    for loc in locations:
        person = loc.get("person")
        lat = loc.get("lat")
        lon = loc.get("lon")
        if person is None or lat is None or lon is None:
            continue
        old_label = loc.get("location_label", "Unknown")
        new_label = lookup_manual_place(lat, lon, for_user=person)
        if new_label is None:
            # No manual place matches — don't overwrite geocoded labels
            continue
        if new_label != old_label:
            loc["location_label"] = new_label
            put_location(person, {k: v for k, v in loc.items() if k != "person"})
            changes.append(
                {"person": person, "old_label": old_label, "new_label": new_label}
            )
    return changes


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points in meters."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c
