"""
Weasley scraper — hits the iCloud FMIP endpoints to fetch family locations.

Flow: validate (already done by auth) → initClient → refreshClient
"""

import logging
import re
from typing import Optional

import requests
from requests.cookies import RequestsCookieJar

from auth import WeasleyAuth, _browser_headers
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
        session = self._make_session()
        base = self.auth.fmip_base_url
        params = self.config.fmip_params

        # Step 1: initClient — establishes FMIP session, sets session cookie
        log.info("Calling initClient...")
        init_resp = session.get(
            f"{base}/fmipservice/client/web/initClient",
            params=params,
            timeout=15,
        )
        log.info(f"initClient: {init_resp.status_code}")

        if init_resp.status_code == 450:
            log.error("initClient returned 450 — session needs re-authentication.")
            return None

        if init_resp.status_code != 200:
            log.error(f"initClient failed: {init_resp.status_code} {init_resp.text}")
            return None

        # Step 2: refreshClient — returns full device/people blob
        log.info("Calling refreshClient...")
        refresh_resp = session.get(
            f"{base}/fmipservice/client/web/refreshClient",
            params=params,
            timeout=15,
        )
        log.info(f"refreshClient: {refresh_resp.status_code}")

        if refresh_resp.status_code == 450:
            log.error("refreshClient returned 450 — session needs re-authentication.")
            return None

        if refresh_resp.status_code != 200:
            log.error(f"refreshClient failed: {refresh_resp.status_code} {refresh_resp.text}")
            return None

        return self._parse_locations(refresh_resp.json())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_session(self) -> requests.Session:
        """Build a requests Session seeded with current auth cookies."""
        jar = RequestsCookieJar()
        for cookie in self.auth.get_cookies_for_requests():
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            jar.set(
                name,
                value,
                domain=cookie.get("domain", ".icloud.com"),
                path=cookie.get("path", "/"),
            )

        session = requests.Session()
        session.cookies = jar
        session.headers.update(_browser_headers())
        return session

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
        devices = data.get("content", [])
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
                log.warning(
                    "Configured family_members entries not found in iCloud payload: "
                    f"{unmatched}. Available device names: {available}"
                )

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


def _normalize_name(value: str) -> str:
    normalized = value.strip().replace("’", "'").casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized
