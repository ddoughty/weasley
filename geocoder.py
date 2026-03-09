"""
Reverse geocoding with:
1) local manual labels (radius-based),
2) local cache for API results,
3) Amazon Places reverse geocoding fallback.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from config import Config

log = logging.getLogger("weasley.geocoder")


class ReverseGeocoder:
    def __init__(self, config: Config):
        self.config = config
        self.db_path = os.path.abspath(config.places_db_path)
        self._ensure_db()

    def resolve_label(self, lat: Optional[float], lon: Optional[float]) -> str:
        if lat is None or lon is None:
            return "Unknown"

        manual = self._lookup_manual(lat, lon)
        if manual:
            return manual

        cached = self._lookup_cache(lat, lon)
        if cached:
            return cached

        remote = self._lookup_amazon(lat, lon)
        if remote:
            self._store_cache(lat, lon, remote, source="amazon")
            return remote

        return f"{lat:.4f}, {lon:.4f}"

    def add_manual_place(self, name: str, lat: float, lon: float, radius_m: float) -> int:
        if not name.strip():
            raise ValueError("name must not be empty")
        if radius_m <= 0:
            raise ValueError("radius_m must be > 0")

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO manual_places (name, lat, lon, radius_m)
                VALUES (?, ?, ?, ?)
                """,
                (name.strip(), float(lat), float(lon), float(radius_m)),
            )
            return int(cur.lastrowid)

    def list_manual_places(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, lat, lon, radius_m, created_at
                FROM manual_places
                ORDER BY name, id
                """
            ).fetchall()

        return [
            {
                "id": row["id"],
                "name": row["name"],
                "lat": row["lat"],
                "lon": row["lon"],
                "radius_m": row["radius_m"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def remove_manual_place(self, place_id: Optional[int] = None, name: Optional[str] = None) -> int:
        if place_id is None and not name:
            raise ValueError("provide place_id or name")

        with self._connect() as conn:
            if place_id is not None:
                cur = conn.execute("DELETE FROM manual_places WHERE id = ?", (int(place_id),))
            else:
                cur = conn.execute("DELETE FROM manual_places WHERE name = ?", (name.strip(),))
            return int(cur.rowcount)

    def _ensure_db(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS manual_places (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    radius_m REAL NOT NULL DEFAULT 150.0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    lat_q REAL NOT NULL,
                    lon_q REAL NOT NULL,
                    label TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (lat_q, lon_q)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_manual_places_lat_lon
                ON manual_places (lat, lon)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _lookup_manual(self, lat: float, lon: float) -> Optional[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, lat, lon, radius_m FROM manual_places"
            ).fetchall()

        best_name = None
        best_distance = float("inf")
        for row in rows:
            distance = _haversine_m(lat, lon, row["lat"], row["lon"])
            if distance <= row["radius_m"] and distance < best_distance:
                best_distance = distance
                best_name = row["name"]
        return best_name

    def _lookup_cache(self, lat: float, lon: float) -> Optional[str]:
        lat_q, lon_q = self._cache_key(lat, lon)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT label FROM geocode_cache WHERE lat_q = ? AND lon_q = ?",
                (lat_q, lon_q),
            ).fetchone()
        if row:
            return row["label"]
        return None

    def _store_cache(self, lat: float, lon: float, label: str, source: str):
        lat_q, lon_q = self._cache_key(lat, lon)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO geocode_cache (lat_q, lon_q, label, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lat_q, lon_q)
                DO UPDATE SET
                    label = excluded.label,
                    source = excluded.source,
                    created_at = CURRENT_TIMESTAMP
                """,
                (lat_q, lon_q, label, source),
            )

    def _cache_key(self, lat: float, lon: float) -> tuple[float, float]:
        precision = self.config.places_cache_precision
        if precision < 0:
            precision = 0
        if precision > 8:
            precision = 8
        return round(lat, precision), round(lon, precision)

    def _lookup_amazon(self, lat: float, lon: float) -> Optional[str]:
        if not self.config.amazon_places_api_key:
            return None

        params = {"key": self.config.amazon_places_api_key}
        body = {
            "QueryPosition": [lon, lat],
            "MaxResults": 1,
            # We persist API results in local cache.
            "IntendedUse": "Storage",
        }
        endpoints = _reverse_geocode_endpoints(
            self.config.amazon_places_endpoint, self.config.amazon_places_region
        )
        max_attempts = 3
        base_backoff_s = 0.6
        last_error = None

        for endpoint in endpoints:
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = requests.post(
                        endpoint,
                        params=params,
                        json=body,
                        headers={"Content-Type": "application/json"},
                        timeout=15,
                    )
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}"
                    if attempt < max_attempts:
                        time.sleep(base_backoff_s * attempt)
                        continue
                    break

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception as e:
                        log.warning(f"Amazon Places JSON decode failed: {e}")
                        return None

                    label = _extract_amazon_label(data)
                    if label:
                        return label

                    log.warning("Amazon Places response had no usable label.")
                    return None

                error_body = (resp.text or "").replace("\n", " ")
                if len(error_body) > 220:
                    error_body = error_body[:220] + "..."
                last_error = f"{resp.status_code} {error_body}"

                lower_body = error_body.lower()
                # This usually means /reverse-geocode was used instead of /v2/reverse-geocode.
                if (
                    resp.status_code == 403
                    and "unable to determine service/operation name to be authorized" in lower_body
                ):
                    break

                retryable = resp.status_code in (429, 500, 502, 503, 504)
                # AWS key policy changes may propagate slowly; short retries often succeed.
                policy_propagating = (
                    resp.status_code == 403
                    and "explicit deny in an identity-based policy" in lower_body
                )
                if (retryable or policy_propagating) and attempt < max_attempts:
                    time.sleep(base_backoff_s * attempt)
                    continue

                if policy_propagating:
                    log.warning(
                        "Amazon Places reverse geocode failed: explicit deny in policy. "
                        "If permissions were just updated, wait a few minutes and retry. "
                        "Otherwise allow geo-places:ReverseGeocode for this key."
                    )
                break

        if last_error:
            log.warning(f"Amazon Places reverse geocode failed: {last_error}")
        return None


def _extract_amazon_label(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return None

    list_candidates = []
    for key in ("Results", "results", "ResultItems", "resultItems", "Items", "items"):
        value = data.get(key)
        if isinstance(value, list):
            list_candidates.append(value)

    for entries in list_candidates:
        for entry in entries:
            label = _extract_label_from_entry(entry)
            if label:
                return label

    # Some responses may include top-level address fields.
    return _extract_label_from_entry(data)


def _extract_label_from_entry(entry) -> Optional[str]:
    if not isinstance(entry, dict):
        return None

    paths = [
        ("Label",),
        ("label",),
        ("Title",),
        ("title",),
        ("Address", "Label"),
        ("Address", "label"),
        ("Place", "Label"),
        ("Place", "label"),
        ("Place", "Address", "Label"),
        ("formattedAddress",),
        ("FormattedAddress",),
        ("Place", "formattedAddress"),
        ("Description",),
        ("description",),
    ]
    for path in paths:
        value = _deep_get(entry, path)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for addr_key in ("Address", "address", "Place"):
        addr = entry.get(addr_key)
        if isinstance(addr, dict):
            composed = _compose_address(addr)
            if composed:
                return composed
    return None


def _reverse_geocode_endpoints(configured: str, region: str) -> list[str]:
    """
    Return one or more candidate endpoints.
    We prefer v2 first, but tolerate a user-provided /reverse-geocode URL
    by retrying a normalized /v2/reverse-geocode variant.
    """
    default_base = f"https://places.geo.{region}.amazonaws.com"
    raw = (configured or "").strip()

    if not raw:
        return [f"{default_base}/v2/reverse-geocode"]

    if "://" not in raw:
        raw = f"https://{raw}"

    split = urlsplit(raw)
    scheme = split.scheme or "https"
    netloc = split.netloc
    path = split.path or ""

    if not netloc:
        # Handles values like "places.geo.us-east-1.amazonaws.com/v2"
        netloc, _, path = path.partition("/")
        path = f"/{path}" if path else ""

    base = urlunsplit((scheme, netloc, "", "", "")).rstrip("/")
    normalized_path = path.rstrip("/")
    candidates: list[str] = []

    def add(url: str):
        url = url.strip().rstrip("/")
        if url and url not in candidates:
            candidates.append(url)

    if normalized_path in ("", "/"):
        add(f"{base}/v2/reverse-geocode")
    elif normalized_path == "/v2":
        add(f"{base}/v2/reverse-geocode")
    elif normalized_path == "/v2/reverse-geocode":
        add(f"{base}/v2/reverse-geocode")
    elif normalized_path == "/reverse-geocode":
        add(f"{base}/v2/reverse-geocode")
        add(f"{base}/reverse-geocode")
    else:
        # Respect custom paths, but still try the canonical v2 endpoint first.
        add(f"{base}/v2/reverse-geocode")
        add(f"{base}{normalized_path}")

    return candidates


def _deep_get(obj: dict, path: tuple[str, ...]):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _compose_address(addr: dict) -> Optional[str]:
    parts = []
    for key in (
        "AddressNumber",
        "Street",
        "Neighborhood",
        "Municipality",
        "SubRegion",
        "Region",
        "PostalCode",
        "Country",
    ):
        value = addr.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if not parts:
        return None
    return ", ".join(parts)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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
