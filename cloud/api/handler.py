"""
Weasley REST API Lambda — serves current locations and places CRUD
via API Gateway HTTP API.

Routes:
  GET    /                    — redirect to the dashboard
  GET    /login               — admin login form
  POST   /login               — establish a browser session
  POST   /logout              — clear a browser session
  GET    /locations           — all person locations (JSON)
  GET    /places              — all manual places (JSON)
  POST   /places              — create a place
  PUT    /places/{place_id}   — update a place
  DELETE /places/{place_id}   — delete a place
  GET    /dashboard           — human-readable HTML location view
  GET    /manage-places       — HTML UI for managing place labels

API clients authenticate with the x-api-key header. Browser users exchange the
same high-entropy secret for a signed, short-lived session cookie at /login.
"""

import base64
import json
import logging
import os
import secrets
from datetime import datetime
from html import escape
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from api.auth import (
    AuthContext,
    authenticate,
    clear_session_cookie,
    create_session,
    csrf_is_valid,
    session_cookie,
)
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
SESSION_SIGNING_KEY = os.environ.get("SESSION_SIGNING_KEY", "")
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "America/New_York")

HTML_ROUTES = {"GET /", "GET /dashboard", "GET /manage-places"}
MUTATING_ROUTES = {
    "POST /places",
    "PUT /places/{place_id}",
    "DELETE /places/{place_id}",
}


def lambda_handler(event, context):
    """API Gateway HTTP API v2 handler."""
    route_key = _route_key(event)
    auth = authenticate(event, API_KEY, SESSION_SIGNING_KEY)

    if route_key == "GET /login":
        return _redirect(_url(event, "/dashboard")) if auth else _get_login()
    if route_key == "POST /login":
        return _post_login(event)

    if auth is None:
        if route_key in HTML_ROUTES:
            return _redirect(_url(event, "/login"))
        return _response(401, {"error": "Unauthorized"})

    if route_key == "POST /logout":
        form = _parse_form_body(event)
        if not csrf_is_valid(event, auth, form_token=form.get("csrf_token", "")):
            return _response(403, {"error": "Invalid CSRF token"})
        return _redirect(_url(event, "/login"), cookies=[clear_session_cookie()])

    if route_key in MUTATING_ROUTES and not csrf_is_valid(event, auth):
        return _response(403, {"error": "Invalid CSRF token"})

    if route_key == "GET /":
        return _redirect(_url(event, "/dashboard"))
    if route_key == "GET /locations":
        return _get_locations()
    if route_key == "GET /dashboard":
        return _get_dashboard(auth)
    if route_key == "GET /manage-places":
        return _get_places_manage(auth)
    if route_key == "GET /places":
        return _get_places()
    if route_key == "POST /places":
        return _create_place(event)
    if route_key == "PUT /places/{place_id}":
        place_id = event.get("pathParameters", {}).get("place_id", "")
        return _update_place(event, place_id)
    if route_key == "DELETE /places/{place_id}":
        place_id = event.get("pathParameters", {}).get("place_id", "")
        return _delete_place(place_id)
    return _response(404, {"error": "Not found"})


def _route_key(event: dict) -> str:
    """Return a stable route key for default and custom API hostnames."""
    route_key = event.get("routeKey", "")
    if route_key and route_key != "$default":
        return route_key

    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", "")
    path = request.get("path", "").rstrip("/") or "/"
    if path == "/prod" or path.startswith("/prod/"):
        path = path[5:] or "/"
    if event.get("pathParameters", {}).get("place_id") and path.startswith("/places/"):
        path = "/places/{place_id}"
    return f"{method} {path}"


def _url(event: dict, path: str) -> str:
    """Build a URL that works during both staged and custom-domain rollout."""
    request_context = event.get("requestContext", {})
    request_path = request_context.get("http", {}).get("path", "")
    headers = {
        str(key).lower(): str(value)
        for key, value in (event.get("headers") or {}).items()
        if value is not None
    }
    domain_name = str(request_context.get("domainName") or headers.get("host", ""))
    uses_default_endpoint = not domain_name or ".execute-api." in domain_name
    base_path = (
        "/prod"
        if uses_default_endpoint
        and (request_path == "/prod" or request_path.startswith("/prod/"))
        else ""
    )
    return f"{base_path}{path}"


def _get_login(error: str = "") -> dict:
    """Render the browser login form."""
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign in — Weasley Clock</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      padding: 1rem; background: #1a1a2e; color: #eee;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    main {{ width: min(100%, 380px); background: #16213e; padding: 2rem; border-radius: 12px; }}
    h1 {{ margin: 0 0 0.4rem; color: #e0c068; font-size: 1.7rem; }}
    p {{ color: #aaa; margin: 0 0 1.5rem; }}
    label {{ display: block; margin-bottom: 0.4rem; color: #ccc; }}
    input {{
      width: 100%; padding: 0.7rem; border: 1px solid #444; border-radius: 6px;
      background: #0f1a30; color: #fff; font-size: 1rem;
    }}
    button {{
      width: 100%; margin-top: 1rem; padding: 0.7rem; border: 0; border-radius: 6px;
      background: #e0c068; color: #1a1a2e; font-size: 1rem; font-weight: 600;
    }}
    .error {{ margin-bottom: 1rem; color: #ff9b94; }}
  </style>
</head>
<body>
  <main>
    <h1>The Weasley Clock</h1>
    <p>Enter the admin secret to continue.</p>
    {error_html}
    <form method="post" action="login">
      <label for="admin-secret">Admin secret</label>
      <input id="admin-secret" name="admin_secret" type="password" required autofocus autocomplete="current-password">
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>"""
    return _html_response(200, html)


def _post_login(event: dict) -> dict:
    """Exchange the shared admin secret for a signed browser session."""
    if not API_KEY or not SESSION_SIGNING_KEY:
        log.error("Admin login is unavailable because authentication is not configured")
        return _html_response(503, "Authentication is not configured")

    provided_secret = _parse_form_body(event).get("admin_secret", "")
    if not provided_secret or not secrets.compare_digest(provided_secret, API_KEY):
        log.warning("Admin login rejected")
        return _get_login("The admin secret was not accepted.") | {"statusCode": 401}

    token, _ = create_session(SESSION_SIGNING_KEY)
    return _redirect(_url(event, "/dashboard"), cookies=[session_cookie(token)])


def _parse_form_body(event: dict) -> dict[str, str]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {}
    return {key: values[-1] for key, values in parse_qs(body).items() if values}


def _get_locations():
    """Return all tracked person locations."""
    locations = get_all_locations()
    return _response(200, locations)


def _get_dashboard(auth: AuthContext):
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
    .subtitle a {{ color: #e0c068; }}
    .logout {{ display: inline; }}
    .logout button {{
      border: 0; padding: 0; background: none; color: #e0c068;
      text-decoration: underline; cursor: pointer; font: inherit;
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
    <div class="subtitle">
      Updated {now.strftime("%I:%M %p, %b %d")} ·
      <a href="manage-places">Manage Places</a> ·
      <form class="logout" method="post" action="logout">
        <input type="hidden" name="csrf_token" value="{escape(auth.csrf_token or '')}">
        <button type="submit">Sign out</button>
      </form>
    </div>
    {members_html}
  </div>
</body>
</html>"""

    return _html_response(200, html)


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


def _get_places_manage(auth: AuthContext):
    """Render an HTML UI for managing place labels."""
    places = get_all_places()
    locations = get_all_locations()
    nonce = secrets.token_urlsafe(18)
    csrf_token = escape(auth.csrf_token or "")

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
            <button class="btn btn-save" data-action="save">Save</button>
            <button class="btn btn-delete" data-action="delete">Delete</button>
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
        lat = escape(str(loc.get("lat", 0)))
        lon = escape(str(loc.get("lon", 0)))
        member_cards += f"""
        <div class="member-chip">
          <strong>{name}</strong> &mdash; {label}
          <button class="btn btn-small" data-action="prefill" data-lat="{lat}" data-lon="{lon}" data-person="{name}">
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
    <div class="subtitle"><a href="dashboard">Back to Dashboard</a></div>

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
        <button class="btn btn-create" id="create-place">Create Place</button>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script nonce="{nonce}">
    const CSRF_TOKEN = "{csrf_token}";
    const BASE = window.location.pathname.replace(/\\/manage-places\\/?$/, "");

    function headers() {{
      return {{"Content-Type": "application/json", "X-CSRF-Token": CSRF_TOKEN}};
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
        const resp = await fetch(BASE + "/places/" + id, {{
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
        const scopeTag = document.createElement("span");
        scopeTag.className = user ? "tag user-tag" : "tag global-tag";
        scopeTag.textContent = user || "Everyone";
        scopeTd.replaceChildren(scopeTag);
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
        const resp = await fetch(BASE + "/places/" + id, {{
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
        const resp = await fetch(BASE + "/places", {{
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

    document.querySelectorAll('[data-action="save"]').forEach((button) => {{
      button.addEventListener("click", () => savePlace(button));
    }});
    document.querySelectorAll('[data-action="delete"]').forEach((button) => {{
      button.addEventListener("click", () => deletePlace(button));
    }});
    document.querySelectorAll('[data-action="prefill"]').forEach((button) => {{
      button.addEventListener("click", () => prefillFromMember(
        parseFloat(button.dataset.lat),
        parseFloat(button.dataset.lon),
        button.dataset.person,
      ));
    }});
    document.getElementById("create-place").addEventListener("click", createPlace);
  </script>
</body>
</html>"""

    return _html_response(200, html, script_nonce=nonce)


def _security_headers(content_type: str, script_nonce: str = "") -> dict[str, str]:
    script_source = f"'nonce-{script_nonce}'" if script_nonce else "'none'"
    return {
        "Content-Type": content_type,
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src {script_source}; "
            "style-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        ),
        "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
        "Referrer-Policy": "no-referrer",
        "Strict-Transport-Security": "max-age=31536000",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def _html_response(status_code: int, html: str, script_nonce: str = "") -> dict:
    return {
        "statusCode": status_code,
        "headers": _security_headers("text/html; charset=utf-8", script_nonce),
        "body": html,
    }


def _redirect(location: str, cookies: list[str] | None = None) -> dict:
    response = {
        "statusCode": 303,
        "headers": {
            **_security_headers("text/plain; charset=utf-8"),
            "Location": location,
        },
        "body": "",
    }
    if cookies:
        response["cookies"] = cookies
    return response


def _response(status_code: int, body) -> dict:
    """Build an API Gateway v2 response."""
    return {
        "statusCode": status_code,
        "headers": _security_headers("application/json"),
        "body": json.dumps(body),
    }
