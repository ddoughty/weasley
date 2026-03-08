#!/usr/bin/env python3
"""
Weasley - A magic clock that tells you where your family is.
Polls Apple's Find My via iCloud web and pushes locations to a TRMNL display.
"""

import argparse
import logging
import sys
import time

from auth import WeasleyAuth
from scraper import WeasleyScraper
from trmnl import WeasleyTRMNL
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("weasley")


def run_once(config: Config) -> bool:
    """Fetch locations and push to TRMNL. Returns True on success."""
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

    log.info(f"Fetched {len(locations)} family members.")
    for member in locations:
        log.info(f"  {member['name']}: {member.get('lat')}, {member.get('lon')}")

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
        log.info(f"Sleeping {config.poll_interval}s...")
        time.sleep(config.poll_interval)


def run_auth(config: Config):
    """Interactive auth setup only — establish and save a browser session."""
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
        choices=["once", "daemon", "auth"],
        help=(
            "once: fetch and push a single update; "
            "daemon: poll continuously; "
            "auth: run interactive browser login to set up session"
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

    if args.command == "auth":
        run_auth(config)
    elif args.command == "once":
        success = run_once(config)
        sys.exit(0 if success else 1)
    elif args.command == "daemon":
        run_daemon(config)


if __name__ == "__main__":
    main()
