"""
Weasley scraper — hits the iCloud FMIP endpoints to fetch family locations.

Flow: validate (already done by auth) → initClient → refreshClient
"""

import logging
import re
from difflib import SequenceMatcher
from typing import Optional

import requests

from auth import WeasleyAuth, _browser_headers, _cookies_to_jar
from config import Config

log = logging.getLogger("weasley.scraper")


class WeasleyScraper:
    def __init__(self, config: Config, auth: WeasleyAuth):
        self.config = config
        self.auth = auth

    def fetch_locations(self) -> Optional[list[dict]]:
        """
        Returns a list of dicts like:
          [{"name": "Molly", "location": "Home", "lat": ..., "lon": ..., "battery": ...}]
        Returns None on failure.
        """
        fmip_base = self.auth.fmip_base_url
        fmf_base = self.auth.fmf_base_url or fmip_base
        params = self.config.fmip_params
        fmip_payload = self._fmip_client_context_payload()

        for attempt in (1, 2):
            session = self._make_session()

            warm_state = self._warm_find_page(session)
            if warm_state == "retry":
                if attempt == 1:
                    log.warning(
                        "Find page warmup indicates re-auth challenge. Refreshing session and retrying once."
                    )
                    if self.auth.refresh_session(reprime_fmip=True):
                        fmip_base = self.auth.fmip_base_url
                        fmf_base = self.auth.fmf_base_url or fmip_base
                        params = self.config.fmip_params
                        continue
                log.error("Find page warmup requires re-authentication.")
                return None

            friend_state, friend_locations = self._fetch_friend_locations(
                session, fmf_base, params
            )
            if friend_state == "retry":
                if attempt == 1:
                    log.warning(
                        "FMF request returned 450. Refreshing session and retrying once."
                    )
                    if self.auth.refresh_session(reprime_fmip=True):
                        fmip_base = self.auth.fmip_base_url
                        fmf_base = self.auth.fmf_base_url or fmip_base
                        params = self.config.fmip_params
                        continue
                log.error("FMF request returned 450 — session needs re-authentication.")
                return None

            if friend_state == "ok" and friend_locations:
                log.info("Using people locations from FMF service.")
                return friend_locations
            if friend_state == "ok":
                log.info("FMF payload contained no usable people locations; falling back to FMIP.")

            # Step 1: initClient — establishes FMIP session, sets session cookie
            log.info("Calling initClient...")
            init_resp = self._call_fmip_endpoint(
                session,
                f"{fmip_base}/fmipservice/client/web/initClient",
                params,
                fmip_payload,
            )
            log.info(f"initClient: {init_resp.status_code}")

            if init_resp.status_code == 450:
                if attempt == 1:
                    log.warning(
                        "initClient returned 450. Refreshing session via validate and retrying once."
                    )
                    if self.auth.refresh_session(reprime_fmip=True):
                        fmip_base = self.auth.fmip_base_url
                        fmf_base = self.auth.fmf_base_url or fmip_base
                        params = self.config.fmip_params
                        continue
                log.error("initClient returned 450 — session needs re-authentication.")
                return None

            if init_resp.status_code != 200:
                log.error(f"initClient failed: {init_resp.status_code} {init_resp.text}")
                return None

            # Step 2: refreshClient — returns full device/people blob
            log.info("Calling refreshClient...")
            refresh_resp = self._call_fmip_endpoint(
                session,
                f"{fmip_base}/fmipservice/client/web/refreshClient",
                params,
                fmip_payload,
            )
            log.info(f"refreshClient: {refresh_resp.status_code}")

            if refresh_resp.status_code == 450:
                if attempt == 1:
                    log.warning(
                        "refreshClient returned 450. Refreshing session via validate and retrying once."
                    )
                    if self.auth.refresh_session(reprime_fmip=True):
                        fmip_base = self.auth.fmip_base_url
                        fmf_base = self.auth.fmf_base_url or fmip_base
                        params = self.config.fmip_params
                        continue
                log.error("refreshClient returned 450 — session needs re-authentication.")
                return None

            if refresh_resp.status_code != 200:
                log.error(f"refreshClient failed: {refresh_resp.status_code} {refresh_resp.text}")
                return None

            payload = refresh_resp.json()
            top_count = (
                len(payload.get("content", []))
                if isinstance(payload.get("content"), list)
                else 0
            )
            server_ctx = payload.get("serverContext", {})
            server_count = (
                len(server_ctx.get("content", []))
                if isinstance(server_ctx, dict)
                and isinstance(server_ctx.get("content"), list)
                else 0
            )
            log.info(
                "refreshClient payload sizes: content=%d serverContext.content=%d",
                top_count,
                server_count,
            )
            refresh_friend_locations = self._parse_friend_locations(payload)
            if refresh_friend_locations:
                log.info("Using people locations found in refreshClient payload.")
                return refresh_friend_locations
            return self._parse_locations(payload)

        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_session(self) -> requests.Session:
        """Build a requests Session seeded with current auth cookies."""
        jar = _cookies_to_jar(self.auth.get_cookies_for_requests())
        session = requests.Session()
        session.cookies = jar
        session.headers.update(_browser_headers())
        return session

    def _fmip_client_context_payload(self) -> dict:
        return {
            "clientContext": {
                "fmly": True,
                "shouldLocate": True,
                "mapsActive": False,
                "contextApp": "com.apple.mobileme.fmip1",
            }
        }

    def _call_fmip_endpoint(
        self, session: requests.Session, url: str, params: dict, payload: dict
    ) -> requests.Response:
        resp = session.post(url, params=params, json=payload, timeout=15)
        if resp.status_code in (404, 405):
            resp = session.get(url, params=params, timeout=15)
        return resp

    def _fetch_friend_locations(
        self, session: requests.Session, base: str, params: dict
    ) -> tuple[str, Optional[list[dict]]]:
        """
        Query FMF (Find Friends) service for people locations.
        Returns (state, locations):
          state in {"ok", "retry", "error"}.
        """
        url = f"{base}/fmipservice/client/fmfWeb/initClient"
        payload = {
            "clientContext": {
                "appVersion": "1.0",
                "contextApp": "com.apple.mobileme.fmf1",
                "mapkitAvailable": True,
            }
        }

        try:
            log.info("Calling fmfWeb initClient...")
            resp = session.post(url, params=params, json=payload, timeout=15)
            if resp.status_code in (404, 405):
                # API shape can vary by backend; try GET fallback.
                resp = session.get(url, params=params, timeout=15)
            log.info(f"fmfWeb initClient: {resp.status_code}")
        except Exception as e:
            log.warning(f"fmfWeb request failed: {e}")
            return "error", None

        if resp.status_code == 450:
            return "retry", None
        if resp.status_code != 200:
            body = (resp.text or "").replace("\n", " ")
            if len(body) > 200:
                body = body[:200] + "..."
            log.warning(f"fmfWeb initClient failed: {resp.status_code} {body}")
            return "error", None

        try:
            data = resp.json()
        except Exception as e:
            log.warning(f"fmfWeb JSON parse failed: {e}")
            return "error", None

        return "ok", self._parse_friend_locations(data)

    def _warm_find_page(self, session: requests.Session) -> str:
        """
        Hit iCloud Find page before API calls so server can mint/update cookies.
        Returns one of {"ok", "retry", "error"}.
        """
        try:
            resp = session.get("https://www.icloud.com/find/", timeout=20, allow_redirects=True)
        except Exception as e:
            log.warning(f"Find page warmup request failed: {e}")
            return "error"

        log.info("Find page warmup: %s %s", resp.status_code, resp.url)
        final_url = (resp.url or "").lower()
        if "signin" in final_url or "appleauth" in final_url:
            return "retry"
        return "ok"

    def _parse_locations(self, data: dict) -> list[dict]:
        """
        Parse the refreshClient JSON blob into a clean list of family members.

        The blob contains a 'content' list of device objects. Each has:
          - name: device name (e.g. "Dennis's iPhone")
          - location: dict with latitude, longitude, etc.
          - batteryLevel, batteryStatus
          - deviceDisplayName

        TODO: the exact structure needs to be confirmed against a real response.
        This is a best-guess skeleton based on known iCloud FMIP response shapes.
        """
        results = []
        server_context_devices = _extract_server_context_entries(data)
        _log_server_context_devices(server_context_devices)
        devices = _extract_device_entries(data)
        configured_members = self.config.family_members
        matched_config_keys: set[str] = set()
        discovered_names: set[str] = set()

        normalized_config = {
            _normalize_name(key): key for key in configured_members.keys()
        } if configured_members else {}

        for device in devices:
            candidate_names = _candidate_names(device)
            discovered_names.update(candidate_names)
            device_name = candidate_names[0] if candidate_names else "Unknown"

            # If family_members mapping is configured, filter + rename with
            # exact match first, then normalized fallback.
            if configured_members:
                matched_key = None
                for candidate in candidate_names:
                    if candidate in configured_members:
                        matched_key = candidate
                        break
                if not matched_key:
                    for candidate in candidate_names:
                        normalized_candidate = _normalize_name(candidate)
                        if normalized_candidate in normalized_config:
                            matched_key = normalized_config[normalized_candidate]
                            break
                if not matched_key:
                    continue
                matched_config_keys.add(matched_key)
                display_name = configured_members[matched_key]
            else:
                display_name = device_name

            location = device.get("location")
            if not location:
                log.info(f"No location for {device_name}, skipping.")
                continue

            results.append({
                "name": display_name,
                "device_name": device_name,
                "lat": location.get("latitude"),
                "lon": location.get("longitude"),
                "accuracy": location.get("horizontalAccuracy"),
                "timestamp": location.get("timeStamp"),
                "battery_level": device.get("batteryLevel"),
                "battery_status": device.get("batteryStatus"),
                "location_raw": location,
            })

        if configured_members:
            unmatched = sorted(set(configured_members.keys()) - matched_config_keys)
            if unmatched:
                available = sorted(discovered_names) if discovered_names else []
                log_fn = log.warning if not results else log.info
                log_fn(
                    "Configured family_members entries not found in iCloud payload: "
                    f"{unmatched}. Available device names: {available}"
                )
                for wanted in unmatched:
                    suggestions = _top_name_suggestions(wanted, available)
                    if suggestions:
                        log_fn(
                            "Suggested matches for %r: %s",
                            wanted,
                            suggestions,
                        )

        return results

    def _parse_friend_locations(self, data: dict) -> list[dict]:
        """
        Parse FMF payload into a common location structure.
        """
        results = []
        configured_members = self.config.family_members
        matched_config_keys: set[str] = set()
        discovered_names: set[str] = set()

        normalized_config = {
            _normalize_name(key): key for key in configured_members.keys()
        } if configured_members else {}

        details = data.get("contactDetails", [])
        details_by_id: dict[str, dict] = {}
        if isinstance(details, list):
            for contact in details:
                if not isinstance(contact, dict):
                    continue
                for key in ("id", "identifier", "contactId", "dsid"):
                    value = contact.get(key)
                    if value is not None:
                        details_by_id[str(value)] = contact

        locations = data.get("locations", [])
        if not isinstance(locations, list):
            return []

        for loc in locations:
            if not isinstance(loc, dict):
                continue

            contact = {}
            for key in ("id", "identifier", "contactId", "dsid"):
                value = loc.get(key)
                if value is not None and str(value) in details_by_id:
                    contact = details_by_id[str(value)]
                    break

            candidate_names = _candidate_person_names(loc, contact)
            discovered_names.update(candidate_names)
            person_name = candidate_names[0] if candidate_names else "Unknown"

            if configured_members:
                matched_key = None
                for candidate in candidate_names:
                    if candidate in configured_members:
                        matched_key = candidate
                        break
                if not matched_key:
                    for candidate in candidate_names:
                        normalized_candidate = _normalize_name(candidate)
                        if normalized_candidate in normalized_config:
                            matched_key = normalized_config[normalized_candidate]
                            break
                if not matched_key:
                    continue
                matched_config_keys.add(matched_key)
                display_name = configured_members[matched_key]
            else:
                display_name = person_name

            location_blob = loc.get("location") if isinstance(loc.get("location"), dict) else {}
            lat = loc.get("latitude", location_blob.get("latitude"))
            lon = loc.get("longitude", location_blob.get("longitude"))
            if lat is None or lon is None:
                continue

            results.append({
                "name": display_name,
                "device_name": person_name,
                "lat": lat,
                "lon": lon,
                "accuracy": loc.get("horizontalAccuracy", location_blob.get("horizontalAccuracy")),
                "timestamp": (
                    loc.get("locationTimestamp")
                    or loc.get("timeStamp")
                    or loc.get("timestamp")
                    or location_blob.get("timeStamp")
                ),
                "battery_level": None,
                "battery_status": None,
                "location_raw": loc,
            })

        if configured_members:
            unmatched = sorted(set(configured_members.keys()) - matched_config_keys)
            if unmatched:
                available = sorted(discovered_names) if discovered_names else []
                if available:
                    log_fn = log.warning if not results else log.info
                    log_fn(
                        "Configured family_members entries not found in FMF payload: "
                        f"{unmatched}. Available people names: {available}"
                    )
                    for wanted in unmatched:
                        suggestions = _top_name_suggestions(wanted, available)
                        if suggestions:
                            log_fn(
                                "Suggested FMF matches for %r: %s",
                                wanted,
                                suggestions,
                            )
                else:
                    log.debug("FMF payload has no people names for configured-member matching.")

        return results


def _candidate_names(device: dict) -> list[str]:
    names = [
        device.get("name"),
        device.get("deviceDisplayName"),
        device.get("rawDeviceModel"),
        device.get("modelDisplayName"),
    ]

    deduped = []
    seen = set()
    for name in names:
        if not isinstance(name, str):
            continue
        clean = name.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _extract_device_entries(data: dict) -> list[dict]:
    """
    Collect device entries from known refreshClient payload shapes.
    Some accounts put shared devices under serverContext.content.
    """
    pools: list[list] = []
    top_content = data.get("content")
    if isinstance(top_content, list):
        pools.append(top_content)

    server_context_devices = _extract_server_context_entries(data)
    if server_context_devices:
        pools.append(server_context_devices)

    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for pool in pools:
        for entry in pool:
            if not isinstance(entry, dict):
                continue
            marker = _device_identity(entry)
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(entry)

    return merged


def _device_identity(device: dict) -> tuple[str, str]:
    for key in ("id", "baUUID", "prsId", "deviceDiscoveryId"):
        value = device.get(key)
        if value:
            return key, str(value)
    name = str(device.get("name", ""))
    model = str(device.get("rawDeviceModel", ""))
    return "name_model", f"{name}|{model}"


def _extract_server_context_entries(data: dict) -> list[dict]:
    server_context = data.get("serverContext", {})
    if not isinstance(server_context, dict):
        return []

    entries: list[dict] = []
    for key in ("content", "devices"):
        candidate = server_context.get(key)
        if not isinstance(candidate, list):
            continue
        for item in candidate:
            if isinstance(item, dict):
                entries.append(item)

    return entries


def _log_server_context_devices(devices: list[dict]):
    if not devices:
        log.debug("serverContext.content/devices entries: 0")
        return

    log.info("serverContext.content/devices entries: %d", len(devices))
    for idx, device in enumerate(devices, start=1):
        location = device.get("location")
        has_location = isinstance(location, dict) and bool(location)
        lat = location.get("latitude") if has_location else None
        lon = location.get("longitude") if has_location else None
        log.info(
            "serverContext[%s]: name=%r display=%r class=%r fmlyShare=%r "
            "locationCapable=%r has_location=%s lat=%r lon=%r",
            idx,
            device.get("name"),
            device.get("deviceDisplayName"),
            device.get("deviceClass"),
            device.get("fmlyShare"),
            device.get("locationCapable"),
            has_location,
            lat,
            lon,
        )


def _candidate_person_names(location: dict, contact: dict) -> list[str]:
    names = [
        contact.get("name"),
        contact.get("fullName"),
        " ".join(
            part for part in [contact.get("firstName"), contact.get("lastName")] if part
        ),
        location.get("name"),
        location.get("fullName"),
        location.get("displayName"),
    ]
    return [name for name in _dedupe_names(names)]


def _dedupe_names(names: list) -> list[str]:
    deduped = []
    seen = set()
    for name in names:
        if not isinstance(name, str):
            continue
        clean = name.strip()
        if not clean:
            continue
        marker = _normalize_name(clean)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(clean)
    return deduped


def _normalize_name(value: str) -> str:
    normalized = value.strip().replace("’", "'").casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _top_name_suggestions(wanted: str, available: list[str], limit: int = 5) -> list[str]:
    if not wanted or not available:
        return []

    wanted_norm = _normalize_name(wanted)
    scored = []
    for candidate in available:
        candidate_norm = _normalize_name(candidate)
        score = SequenceMatcher(None, wanted_norm, candidate_norm).ratio()
        scored.append((score, candidate))

    scored.sort(key=lambda row: row[0], reverse=True)
    best = [name for score, name in scored if score >= 0.35][:limit]
    return best
