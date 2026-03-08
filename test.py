#!/usr/bin/env python3
"""
Manual smoke test for Weasley auth + scraper pipeline.

This script reads secrets from .env via Config.load().
"""

import json

from auth import WeasleyAuth
from config import Config
from scraper import WeasleyScraper


def main():
    config = Config.load("config.json")

    auth = WeasleyAuth(config)
    if not auth.ensure_session():
        raise SystemExit("Could not establish a valid iCloud session.")

    scraper = WeasleyScraper(config, auth)
    locations = scraper.fetch_locations()
    if locations is None:
        raise SystemExit("Failed to fetch locations.")

    print(json.dumps(locations, indent=2))


if __name__ == "__main__":
    main()
