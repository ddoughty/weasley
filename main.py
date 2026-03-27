#!/usr/bin/env python3
"""
Weasley - A magic clock that tells you where your family is.

Desktop agent: polls Apple's Find My via iCloud web and publishes
raw location events to SQS. All geocoding, display updates, and
place management are handled by the cloud pipeline.
"""

import argparse
import logging
import random
import sys
import time

from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("weasley")


def run_once(config: Config) -> bool:
    """Fetch locations and publish to SQS. Returns True on success."""
    from auth import WeasleyAuth
    from scraper import WeasleyScraper

    auth = WeasleyAuth(config)

    if not auth.ensure_session():
        log.error("Could not establish a valid session.")
        return False

    scraper = WeasleyScraper(config, auth)
    locations = scraper.fetch_locations()

    if locations is None:
        log.error("Failed to fetch locations.")
        return False

    log.info(f"Fetched {len(locations)} family members.")
    for member in locations:
        log.info(f"  {member['name']}: ({member.get('lat')}, {member.get('lon')})")

    from publisher import publish_locations

    publish_locations(config, locations)

    return True


def run_daemon(config: Config):
    """Poll on a schedule indefinitely."""
    log.info(f"Starting Weasley daemon, polling every {config.poll_interval}s.")
    while True:
        try:
            run_once(config)
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
        jitter = random.uniform(-1 / 6, 1 / 6)
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
        choices=["once", "daemon", "auth", "store-credentials"],
        help=(
            "once: fetch and publish a single update; "
            "daemon: poll continuously; "
            "auth: run interactive browser login to set up session; "
            "store-credentials: save iCloud credentials in macOS Keychain"
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
    args = parser.parse_args()

    config = Config.load(args.config, args.env)

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


if __name__ == "__main__":
    main()
