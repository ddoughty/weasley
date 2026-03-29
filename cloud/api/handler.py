"""
Weasley REST API Lambda — serves current locations and places CRUD
via API Gateway HTTP API.

Routes:
  GET    /locations           — all person locations (JSON)
  GET    /places              — all manual places (JSON)
  POST   /places              — create a place
  PUT    /places/{place_id}   — update a place
  DELETE /places/{place_id}   — delete a place
  GET    /dashboard           — human-readable HTML location view
  GET    /places/manage       — HTML UI for managing place labels

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
    refresh_location_labels,
    update_place,
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
        return _get_dashboard(query_params)
    elif method == "GET" and path.rstrip("/") == "/prod/places/manage":
        return _get_places_manage(query_params)
    elif method == "GET" and path.rstrip("/") == "/prod/places":
        return _get_places()
    elif method == "POST" and path.rstrip("/") == "/prod/places":
        return _create_place(event)
    elif method == "PUT" and path.startswith("/prod/places/"):
        place_id = event.get("pathParameters", {}).get("place_id", "")
        return _update_place(event, place_id)
    elif method == "DELETE" and path.startswith("/prod/places/"):
        place_id = event.get("pathParameters", {}).get("place_id", "")
        return _delete_place(place_id)
    else:
        return _response(404, {"error": "Not found"})


def _get_locations():
    """Return all tracked person locations."""
    locations = get_all_locations()
    return _response(200, locations)


def _get_dashboard(query_params: dict = None):
    """Render an HTML dashboard of all family member locations."""
    query_params = query_params or {}
    api_key = query_params.get("key", "")
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
    <div class="subtitle">Updated {now.strftime("%I:%M %p, %b %d")} · <a href="places/manage?key={escape(api_key)}" style="color:#e0c068">Manage Places</a></div>
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
    changes = refresh_location_labels()
    return _response(201, {"place": place, "label_changes": changes})


def _update_place(event, place_id: str):
    """Update an existing place."""
    if not place_id:
        return _response(400, {"error": "Missing place_id"})

    try:
        body = json.loads(event.get("body", "{}"))
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    allowed = {"name", "lat", "lon", "radius_m", "user"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return _response(400, {"error": "No valid fields to update"})

    if "lat" in updates or "lon" in updates:
        try:
            if "lat" in updates:
                updates["lat"] = float(updates["lat"])
            if "lon" in updates:
                updates["lon"] = float(updates["lon"])
        except (ValueError, TypeError):
            return _response(400, {"error": "lat and lon must be numbers"})

    if "radius_m" in updates:
        try:
            updates["radius_m"] = float(updates["radius_m"])
        except (ValueError, TypeError):
            return _response(400, {"error": "radius_m must be a number"})

    result = update_place(place_id, updates)
    if result is None:
        return _response(404, {"error": "Place not found"})

    changes = refresh_location_labels()
    return _response(200, {"place": result, "label_changes": changes})


def _delete_place(place_id: str):
    """Delete a place by ID."""
    if not place_id:
        return _response(400, {"error": "Missing place_id"})
    delete_place(place_id)
    changes = refresh_location_labels()
    return _response(200, {"deleted": place_id, "label_changes": changes})


def _get_places_manage(query_params: dict):
    """Render an HTML UI for managing place labels."""
    places = get_all_places()
    locations = get_all_locations()
    api_key = query_params.get("key", "")

    # Sort places: global first, then by user, then by name
    places.sort(key=lambda p: (p.get("user") or "", p.get("name", "")))

    # Build places table rows
    places_rows = ""
    for place in places:
        pid = escape(place.get("place_id", ""))
        name = escape(place.get("name", ""))
        lat = place.get("lat", 0)
        lon = place.get("lon", 0)
        radius = place.get("radius_m", 200)
        user = escape(place.get("user", "") or "")
        scope = (
            f"<span class='tag user-tag'>{user}</span>"
            if user
            else "<span class='tag global-tag'>Everyone</span>"
        )
        places_rows += f"""
        <tr data-id="{pid}">
          <td><input type="text" class="field-name" value="{name}"></td>
          <td>{scope}</td>
          <td><input type="text" class="field-user" value="{user}" placeholder="(everyone)"></td>
          <td><input type="number" class="field-lat" value="{lat}" step="0.0001"></td>
          <td><input type="number" class="field-lon" value="{lon}" step="0.0001"></td>
          <td><input type="number" class="field-radius" value="{radius}" step="10" min="10"></td>
          <td class="actions">
            <button class="btn btn-save" onclick="savePlace(this)">Save</button>
            <button class="btn btn-delete" onclick="deletePlace(this)">Delete</button>
          </td>
        </tr>"""

    if not places:
        places_rows = (
            '<tr><td colspan="7" class="empty">No places defined yet.</td></tr>'
        )

    # Build family member location cards (for quick "name this location" flow)
    member_cards = ""
    for loc in sorted(locations, key=lambda l: l.get("person", "")):
        name = escape(loc.get("person", "Unknown"))
        label = escape(loc.get("location_label", "Unknown"))
        lat = loc.get("lat", 0)
        lon = loc.get("lon", 0)
        member_cards += f"""
        <div class="member-chip">
          <strong>{name}</strong> &mdash; {label}
          <button class="btn btn-small" onclick="prefillFromMember({lat}, {lon}, '{name}')">
            Name this location
          </button>
        </div>"""

    if not locations:
        member_cards = '<div class="empty">No family members tracked yet.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Manage Places — Weasley Clock</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #1a1a2e;
      color: #eee;
      min-height: 100vh;
      padding: 2rem 1rem;
    }}
    .container {{ max-width: 900px; margin: 0 auto; }}
    h1 {{ text-align: center; font-size: 1.8rem; margin-bottom: 0.3rem; color: #e0c068; }}
    h2 {{ font-size: 1.2rem; color: #e0c068; margin: 1.5rem 0 0.8rem; }}
    .subtitle {{ text-align: center; color: #888; font-size: 0.85rem; margin-bottom: 2rem; }}
    a {{ color: #e0c068; }}

    /* Table */
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 1rem; }}
    th {{ text-align: left; padding: 0.5rem; color: #999; font-size: 0.8rem; border-bottom: 1px solid #333; }}
    td {{ padding: 0.4rem; vertical-align: middle; }}
    tr:hover {{ background: #16213e; }}
    input[type="text"], input[type="number"] {{
      background: #0f1a30; border: 1px solid #333; color: #eee; padding: 0.3rem 0.5rem;
      border-radius: 4px; width: 100%; font-size: 0.9rem;
    }}
    input:focus {{ border-color: #e0c068; outline: none; }}

    /* Tags */
    .tag {{ display: inline-block; padding: 0.15rem 0.5rem; border-radius: 10px; font-size: 0.75rem; }}
    .global-tag {{ background: #2a4a2a; color: #8fc98f; }}
    .user-tag {{ background: #3a2a4a; color: #c08fd0; }}

    /* Buttons */
    .btn {{
      border: none; border-radius: 6px; padding: 0.35rem 0.8rem; cursor: pointer;
      font-size: 0.8rem; font-weight: 500;
    }}
    .btn-save {{ background: #2a6a3a; color: #fff; }}
    .btn-save:hover {{ background: #3a8a4a; }}
    .btn-delete {{ background: #6a2a2a; color: #fff; }}
    .btn-delete:hover {{ background: #8a3a3a; }}
    .btn-create {{ background: #e0c068; color: #1a1a2e; font-size: 0.9rem; padding: 0.5rem 1.2rem; }}
    .btn-create:hover {{ background: #f0d078; }}
    .btn-small {{ font-size: 0.75rem; background: #333; color: #e0c068; padding: 0.2rem 0.6rem; }}
    .btn-small:hover {{ background: #444; }}
    .actions {{ white-space: nowrap; }}
    .actions .btn {{ margin-right: 0.3rem; }}

    /* Members */
    .member-chip {{
      background: #16213e; border-radius: 8px; padding: 0.7rem 1rem; margin-bottom: 0.5rem;
      display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
      flex-wrap: wrap;
    }}

    /* New place form */
    .new-place-form {{
      background: #16213e; border-radius: 12px; padding: 1.2rem; margin-top: 1rem;
      display: grid; grid-template-columns: 1fr 1fr; gap: 0.7rem;
    }}
    .new-place-form label {{ color: #999; font-size: 0.8rem; display: block; margin-bottom: 0.2rem; }}
    .new-place-form .full-width {{ grid-column: 1 / -1; }}

    /* Toast */
    .toast {{
      position: fixed; bottom: 1.5rem; right: 1.5rem; background: #2a6a3a; color: #fff;
      padding: 0.7rem 1.2rem; border-radius: 8px; font-size: 0.9rem;
      opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 100;
    }}
    .toast.show {{ opacity: 1; }}
    .toast.error {{ background: #6a2a2a; }}

    .empty {{ text-align: center; color: #666; padding: 1.5rem; }}

    @media (max-width: 700px) {{
      table {{ font-size: 0.8rem; }}
      .new-place-form {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Manage Places</h1>
    <div class="subtitle"><a href="dashboard?key={escape(api_key)}">Back to Dashboard</a></div>

    <h2>Current Family Locations</h2>
    {member_cards}

    <h2>Place Labels</h2>
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Scope</th>
          <th>User (blank = everyone)</th>
          <th>Latitude</th>
          <th>Longitude</th>
          <th>Radius (m)</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="places-body">
        {places_rows}
      </tbody>
    </table>

    <h2>Add New Place</h2>
    <div class="new-place-form">
      <div>
        <label>Name</label>
        <input type="text" id="new-name" placeholder="e.g. Home, Office">
      </div>
      <div>
        <label>User (blank = everyone)</label>
        <input type="text" id="new-user" placeholder="(everyone)">
      </div>
      <div>
        <label>Latitude</label>
        <input type="number" id="new-lat" step="0.0001" placeholder="42.3370">
      </div>
      <div>
        <label>Longitude</label>
        <input type="number" id="new-lon" step="0.0001" placeholder="-71.1170">
      </div>
      <div>
        <label>Radius (meters)</label>
        <input type="number" id="new-radius" value="200" step="10" min="10">
      </div>
      <div style="display:flex;align-items:end;">
        <button class="btn btn-create" onclick="createPlace()">Create Place</button>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const API_KEY = "{escape(api_key)}";
    const BASE = window.location.pathname.replace(/\\/places\\/manage\\/?$/, "");

    function headers() {{
      return {{"Content-Type": "application/json", "x-api-key": API_KEY}};
    }}

    function toast(msg, isError) {{
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.className = "toast show" + (isError ? " error" : "");
      setTimeout(() => el.className = "toast", 3000);
    }}

    async function savePlace(btn) {{
      const row = btn.closest("tr");
      const id = row.dataset.id;
      const body = {{
        name: row.querySelector(".field-name").value,
        user: row.querySelector(".field-user").value || null,
        lat: parseFloat(row.querySelector(".field-lat").value),
        lon: parseFloat(row.querySelector(".field-lon").value),
        radius_m: parseFloat(row.querySelector(".field-radius").value),
      }};
      try {{
        const resp = await fetch(BASE + "/places/" + id + "?key=" + API_KEY, {{
          method: "PUT", headers: headers(), body: JSON.stringify(body),
        }});
        const data = await resp.json();
        if (!resp.ok) {{ toast(data.error || "Failed to save", true); return; }}
        const changes = data.label_changes || [];
        let msg = "Saved!";
        if (changes.length > 0) {{
          msg += " Updated labels: " + changes.map(c => c.person + " → " + c.new_label).join(", ");
        }}
        toast(msg);
        // Update the scope tag
        const user = body.user || "";
        const scopeTd = row.children[1];
        scopeTd.innerHTML = user
          ? "<span class='tag user-tag'>" + user + "</span>"
          : "<span class='tag global-tag'>Everyone</span>";
      }} catch (e) {{
        toast("Network error", true);
      }}
    }}

    async function deletePlace(btn) {{
      const row = btn.closest("tr");
      const id = row.dataset.id;
      const name = row.querySelector(".field-name").value;
      if (!confirm("Delete place '" + name + "'?")) return;
      try {{
        const resp = await fetch(BASE + "/places/" + id + "?key=" + API_KEY, {{
          method: "DELETE", headers: headers(),
        }});
        if (!resp.ok) {{ toast("Failed to delete", true); return; }}
        row.remove();
        const data = await resp.json();
        const changes = data.label_changes || [];
        let msg = "Deleted!";
        if (changes.length > 0) {{
          msg += " Updated labels: " + changes.map(c => c.person + " → " + c.new_label).join(", ");
        }}
        toast(msg);
      }} catch (e) {{
        toast("Network error", true);
      }}
    }}

    async function createPlace() {{
      const name = document.getElementById("new-name").value.trim();
      const user = document.getElementById("new-user").value.trim() || null;
      const lat = parseFloat(document.getElementById("new-lat").value);
      const lon = parseFloat(document.getElementById("new-lon").value);
      const radius_m = parseFloat(document.getElementById("new-radius").value);
      if (!name || isNaN(lat) || isNaN(lon)) {{
        toast("Name, latitude, and longitude are required", true);
        return;
      }}
      try {{
        const resp = await fetch(BASE + "/places?key=" + API_KEY, {{
          method: "POST", headers: headers(),
          body: JSON.stringify({{ name, lat, lon, radius_m, user }}),
        }});
        const data = await resp.json();
        if (!resp.ok) {{ toast(data.error || "Failed to create", true); return; }}
        const changes = data.label_changes || [];
        let msg = "Created!";
        if (changes.length > 0) {{
          msg += " Updated labels: " + changes.map(c => c.person + " → " + c.new_label).join(", ");
        }}
        toast(msg);
        // Reload to show the new place
        setTimeout(() => window.location.reload(), 500);
      }} catch (e) {{
        toast("Network error", true);
      }}
    }}

    function prefillFromMember(lat, lon, person) {{
      document.getElementById("new-lat").value = lat;
      document.getElementById("new-lon").value = lon;
      document.getElementById("new-name").focus();
      toast("Coordinates set from " + person + "'s location — enter a name");
    }}
  </script>
</body>
</html>"""

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }


def _response(status_code: int, body) -> dict:
    """Build an API Gateway v2 response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
