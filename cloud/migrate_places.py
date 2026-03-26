"""
One-shot migration script: copies manual places and geocode cache
from the desktop SQLite database to DynamoDB.

Usage:
    python migrate_places.py [--places-db PATH] [--dry-run]

Requires AWS credentials and the DynamoDB tables to exist (deploy the
SAM template first).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# Allow importing shared modules from the cloud package.
sys.path.insert(0, os.path.dirname(__file__))

from shared import dynamo


def migrate_places(db_path: str, dry_run: bool = False) -> int:
    """Migrate manual_places rows from SQLite to DynamoDB. Returns count."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, lat, lon, radius_m, user FROM manual_places"
    ).fetchall()
    conn.close()

    count = 0
    for row in rows:
        place = {
            "name": row["name"],
            "lat": row["lat"],
            "lon": row["lon"],
            "radius_m": row["radius_m"],
        }
        user = row["user"]
        if user:
            place["user"] = user

        if dry_run:
            print(f"  [dry-run] Would create place: {place}")
        else:
            created = dynamo.create_place(**place)
            print(f"  Created place: {created['name']} ({created['place_id']})")
        count += 1

    return count


def migrate_geocode_cache(
    db_path: str, precision: int = 4, dry_run: bool = False
) -> int:
    """Migrate geocode_cache rows from SQLite to DynamoDB. Returns count."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT lat_q, lon_q, label, source FROM geocode_cache"
    ).fetchall()
    conn.close()

    count = 0
    for row in rows:
        lat_lon_key = dynamo.cache_key(
            float(row["lat_q"]), float(row["lon_q"]), precision
        )
        if dry_run:
            print(f"  [dry-run] Would cache: {lat_lon_key} -> {row['label']}")
        else:
            dynamo.put_geocode_cache(
                lat_lon_key, row["label"], row["source"] or "amazon"
            )
            print(f"  Cached: {lat_lon_key} -> {row['label']}")
        count += 1

    return count


def main():
    parser = argparse.ArgumentParser(description="Migrate Weasley places to DynamoDB")
    parser.add_argument(
        "--places-db",
        default="./session/places.db",
        help="Path to the SQLite places database (default: ./session/places.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be migrated without writing to DynamoDB",
    )
    parser.add_argument(
        "--skip-cache",
        action="store_true",
        help="Skip geocode cache migration (it will repopulate organically)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.places_db):
        print(f"Database not found: {args.places_db}")
        sys.exit(1)

    print(f"Migrating from {args.places_db}...")
    if args.dry_run:
        print("[DRY RUN — no writes will be made]\n")

    print("Manual places:")
    places_count = migrate_places(args.places_db, dry_run=args.dry_run)
    print(f"  {places_count} place(s) {'would be ' if args.dry_run else ''}migrated.\n")

    if not args.skip_cache:
        print("Geocode cache:")
        cache_count = migrate_geocode_cache(args.places_db, dry_run=args.dry_run)
        print(
            f"  {cache_count} cache entry/entries {'would be ' if args.dry_run else ''}migrated.\n"
        )
    else:
        print("Geocode cache: skipped (will repopulate organically).\n")

    print("Done.")


if __name__ == "__main__":
    main()
