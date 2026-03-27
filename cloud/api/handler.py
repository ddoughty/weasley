"""
Weasley REST API Lambda — serves current locations and places CRUD
via API Gateway HTTP API.

Routes:
  GET    /locations           — all person locations
  GET    /places              — all manual places
  POST   /places              — create a place
  DELETE /places/{place_id}   — delete a place

All requests require x-api-key header matching the API_KEY env var.
"""

import json
import logging
import os

from shared.dynamo import (
    create_place,
    delete_place,
    get_all_locations,
    get_all_places,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

API_KEY = os.environ.get("API_KEY", "")


def lambda_handler(event, context):
    """API Gateway HTTP API v2 handler."""
    # Authenticate
    headers = event.get("headers", {})
    provided_key = headers.get("x-api-key", "")
    if not API_KEY or provided_key != API_KEY:
        return _response(401, {"error": "Unauthorized"})

    route_key = event.get("routeKey", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("requestContext", {}).get("http", {}).get("path", "")

    # Route dispatch
    if method == "GET" and path.rstrip("/") == "/prod/locations":
        return _get_locations()
    elif method == "GET" and path.rstrip("/") == "/prod/places":
        return _get_places()
    elif method == "POST" and path.rstrip("/") == "/prod/places":
        return _create_place(event)
    elif method == "DELETE" and path.startswith("/prod/places/"):
        place_id = event.get("pathParameters", {}).get("place_id", "")
        return _delete_place(place_id)
    else:
        return _response(404, {"error": "Not found"})


def _get_locations():
    """Return all tracked person locations."""
    locations = get_all_locations()
    return _response(200, locations)


def _get_places():
    """Return all manual places."""
    places = get_all_places()
    return _response(200, places)


def _create_place(event):
    """Create a new manual place from JSON body."""
    try:
        body = json.loads(event.get("body", "{}"))
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    name = body.get("name")
    lat = body.get("lat")
    lon = body.get("lon")
    if not name or lat is None or lon is None:
        return _response(400, {"error": "Missing required fields: name, lat, lon"})

    try:
        lat = float(lat)
        lon = float(lon)
    except (ValueError, TypeError):
        return _response(400, {"error": "lat and lon must be numbers"})

    radius_m = float(body.get("radius_m", 200.0))
    user = body.get("user")

    place = create_place(name=name, lat=lat, lon=lon, radius_m=radius_m, user=user)
    return _response(201, place)


def _delete_place(place_id: str):
    """Delete a place by ID."""
    if not place_id:
        return _response(400, {"error": "Missing place_id"})
    delete_place(place_id)
    return _response(200, {"deleted": place_id})


def _response(status_code: int, body) -> dict:
    """Build an API Gateway v2 response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
