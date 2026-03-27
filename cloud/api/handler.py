"""
Weasley REST API Lambda — serves current locations and places CRUD
via API Gateway HTTP API.

Routes:
  GET    /locations           — all person locations (JSON)
  GET    /places              — all manual places (JSON)
  POST   /places              — create a place
  DELETE /places/{place_id}   — delete a place
  GET    /dashboard           — human-readable HTML location view

All requests require x-api-key header or ?key= query param matching the
API_KEY env var.
"""

import json
import logging
import os
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from shared.dynamo import (
    create_place,
    delete_place,
    get_all_locations,
    get_all_places,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

API_KEY = os.environ.get("API_KEY", "")
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "America/New_York")


def lambda_handler(event, context):
    """API Gateway HTTP API v2 handler."""
    # Authenticate — accept header or query parameter
    headers = event.get("headers", {})
    query_params = event.get("queryStringParameters") or {}
    provided_key = headers.get("x-api-key", "") or query_params.get("key", "")
    if not API_KEY or provided_key != API_KEY:
        return _response(401, {"error": "Unauthorized"})

    route_key = event.get("routeKey", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("requestContext", {}).get("http", {}).get("path", "")

    # Route dispatch
    if method == "GET" and path.rstrip("/") == "/prod/locations":
        return _get_locations()
    elif method == "GET" and path.rstrip("/") == "/prod/dashboard":
        return _get_dashboard()
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


def _get_dashboard():
    """Render an HTML dashboard of all family member locations."""
    locations = get_all_locations()
    tz = ZoneInfo(DISPLAY_TIMEZONE)
    now = datetime.now(tz)

    members_html = ""
    for loc in sorted(locations, key=lambda l: l.get("person", "")):
        name = escape(loc.get("person", "Unknown"))
        label = escape(loc.get("location_label", "Unknown"))
        battery = loc.get("battery_level")
        battery_pct = f"{int(battery * 100)}%" if battery is not None else "?"
        battery_status = escape(loc.get("battery_status", "") or "")
        ts = loc.get("timestamp")
        if ts:
            try:
                dt = datetime.fromtimestamp(ts / 1000, tz=tz)
                last_seen = dt.strftime("%I:%M %p")
                age_minutes = int((now - dt).total_seconds() / 60)
                if age_minutes < 1:
                    age_text = "just now"
                elif age_minutes < 60:
                    age_text = f"{age_minutes}m ago"
                else:
                    hours = age_minutes // 60
                    age_text = f"{hours}h {age_minutes % 60}m ago"
            except Exception:
                last_seen = "Unknown"
                age_text = ""
        else:
            last_seen = "Unknown"
            age_text = ""

        battery_icon = _battery_icon(battery, battery_status)
        freshness_class = _freshness_class(loc.get("timestamp"), now, tz)

        members_html += f"""
        <div class="member-card {freshness_class}">
          <div class="member-name">{name}</div>
          <div class="member-location">{label}</div>
          <div class="member-details">
            <span class="battery">{battery_icon} {battery_pct}</span>
            <span class="last-seen">{last_seen}</span>
            <span class="age">{age_text}</span>
          </div>
        </div>"""

    if not locations:
        members_html = '<div class="empty">No family members tracked yet.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Weasley Clock</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #1a1a2e;
      color: #eee;
      min-height: 100vh;
      padding: 2rem 1rem;
    }}
    .container {{ max-width: 600px; margin: 0 auto; }}
    h1 {{
      text-align: center;
      font-size: 1.8rem;
      margin-bottom: 0.3rem;
      color: #e0c068;
    }}
    .subtitle {{
      text-align: center;
      color: #888;
      font-size: 0.85rem;
      margin-bottom: 2rem;
    }}
    .member-card {{
      background: #16213e;
      border-radius: 12px;
      padding: 1.2rem 1.5rem;
      margin-bottom: 1rem;
      border-left: 4px solid #e0c068;
    }}
    .member-card.stale {{
      border-left-color: #e07068;
      opacity: 0.7;
    }}
    .member-name {{
      font-size: 1.3rem;
      font-weight: 600;
      color: #e0c068;
      margin-bottom: 0.3rem;
    }}
    .member-location {{
      font-size: 1.1rem;
      margin-bottom: 0.6rem;
    }}
    .member-details {{
      display: flex;
      gap: 1.2rem;
      font-size: 0.85rem;
      color: #999;
    }}
    .empty {{
      text-align: center;
      color: #666;
      padding: 3rem;
      font-size: 1.1rem;
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>The Weasley Clock</h1>
    <div class="subtitle">Updated {now.strftime("%I:%M %p, %b %d")}</div>
    {members_html}
  </div>
</body>
</html>"""

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }


def _battery_icon(level, status):
    """Return a text battery indicator."""
    if status and "charging" in status.lower():
        return "&#9889;"  # lightning bolt
    if level is None:
        return "&#128267;"  # battery
    if level > 0.5:
        return "&#128267;"
    if level > 0.2:
        return "&#128268;"
    return "&#129707;"  # low battery


def _freshness_class(ts_ms, now, tz):
    """Return CSS class based on how recent the location update is."""
    if ts_ms is None:
        return "stale"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=tz)
        age_minutes = (now - dt).total_seconds() / 60
        return "stale" if age_minutes > 60 else "fresh"
    except Exception:
        return "stale"


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
