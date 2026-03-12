#!/usr/bin/env python3
"""
Weasley - A magic clock that tells you where your family is.
Polls Apple's Find My via iCloud web and pushes locations to a TRMNL display.
"""

import argparse
import logging
import sys
import random
import time

from config import Config
from geocoder import ReverseGeocoder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("weasley")


def run_once(config: Config) -> bool:
    """Fetch locations and push to TRMNL. Returns True on success."""
    from auth import WeasleyAuth
    from scraper import WeasleyScraper
    from trmnl import WeasleyTRMNL

    auth = WeasleyAuth(config)

    # Ensure we have a valid session, prompting interactively if needed
    if not auth.ensure_session():
        log.error("Could not establish a valid session.")
        return False

    scraper = WeasleyScraper(config, auth)
    locations = scraper.fetch_locations()

    if locations is None:
        log.error("Failed to fetch locations.")
        return False

    geocoder = ReverseGeocoder(config)
    for member in locations:
        member["location_label"] = geocoder.resolve_label(
            member.get("lat"), member.get("lon"), for_user=member.get("name")
        )

    log.info(f"Fetched {len(locations)} family members.")
    for member in locations:
        log.info(
            f"  {member['name']}: {member.get('location_label')} "
            f"({member.get('lat')}, {member.get('lon')})"
        )

    trmnl = WeasleyTRMNL(config)
    trmnl.push(locations)

    return True


def run_daemon(config: Config):
    """Poll on a schedule indefinitely."""
    log.info(f"Starting Weasley daemon, polling every {config.poll_interval}s.")
    while True:
        try:
            run_once(config)
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
        jitter = random.uniform(-1/6, 1/6)
        sleep_time = int(config.poll_interval * (1 + jitter))
        log.info(f"Sleeping {sleep_time}s (base {config.poll_interval}s ± jitter)...")
        time.sleep(sleep_time)


def run_auth(config: Config):
    """Interactive auth setup only — establish and save a browser session."""
    from auth import WeasleyAuth

    log.info("Running interactive authentication setup...")
    auth = WeasleyAuth(config)
    if auth.interactive_login():
        log.info("Session saved. You can now run Weasley in daemon mode.")
    else:
        log.error("Authentication completed but session validation failed.")


def main():
    parser = argparse.ArgumentParser(
        description="Weasley: a magic clock showing where your family is."
    )
    parser.add_argument(
        "command",
        choices=[
            "once",
            "daemon",
            "auth",
            "store-credentials",
            "place-add",
            "place-list",
            "place-remove",
        ],
        help=(
            "once: fetch and push a single update; "
            "daemon: poll continuously; "
            "auth: run interactive browser login to set up session; "
            "store-credentials: save iCloud credentials in macOS Keychain; "
            "place-add/place-list/place-remove: manage manual geocode labels"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file (default: config.json)",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to .env file for secrets (default: .env)",
    )
    parser.add_argument("--name", help="Place name for place-add/place-remove")
    parser.add_argument("--lat", type=float, help="Latitude for place-add")
    parser.add_argument("--lon", type=float, help="Longitude for place-add")
    parser.add_argument(
        "--radius",
        type=float,
        default=150.0,
        help="Radius in meters for place-add (default: 150)",
    )
    parser.add_argument("--id", type=int, help="Row id for place-remove")
    parser.add_argument(
        "--user",
        help="Family member name for per-user place override (place-add)",
    )
    args = parser.parse_args()

    config = Config.load(args.config, args.env)
    geocoder = ReverseGeocoder(config)

    if args.command == "store-credentials":
        from credentials import store_credentials

        success = store_credentials()
        sys.exit(0 if success else 1)
    elif args.command == "auth":
        run_auth(config)
    elif args.command == "once":
        success = run_once(config)
        sys.exit(0 if success else 1)
    elif args.command == "daemon":
        run_daemon(config)
    elif args.command == "place-add":
        if args.name is None or args.lat is None or args.lon is None:
            parser.error("place-add requires --name, --lat, and --lon")
        place_id = geocoder.add_manual_place(
            args.name, args.lat, args.lon, args.radius, user=args.user
        )
        log.info(
            "Added place id=%s name=%r user=%s at lat=%s lon=%s radius=%sm",
            place_id,
            args.name,
            args.user or "(global)",
            args.lat,
            args.lon,
            args.radius,
        )
    elif args.command == "place-list":
        places = geocoder.list_manual_places()
        if not places:
            log.info("No manual places configured.")
        for place in places:
            log.info(
                "id=%s name=%r user=%s lat=%s lon=%s radius=%sm created=%s",
                place["id"],
                place["name"],
                place["user"] or "(global)",
                place["lat"],
                place["lon"],
                place["radius_m"],
                place["created_at"],
            )
    elif args.command == "place-remove":
        if args.id is None and args.name is None:
            parser.error("place-remove requires --id or --name")
        removed = geocoder.remove_manual_place(place_id=args.id, name=args.name)
        log.info("Removed %s manual place row(s).", removed)


if __name__ == "__main__":
    main()
