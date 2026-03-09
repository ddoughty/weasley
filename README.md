# Weasley 🕰️

> *"It's a clock, but instead of hands, there are little figures of family members..."*
> — Harry Potter and the Chamber of Secrets

Weasley polls Apple's Find My via iCloud web and pushes family member locations
to a [TRMNL](https://usetrmnl.com) e-ink display.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure runtime settings

On first run, a template `config.json` will be created with non-secret settings:

```bash
python main.py once
```

Fill in `config.json`:

```json
{
  "client_build_number": "2604Build20",
  "client_mastering_number": "2604Build20",
  "session_dir": "./session",
  "amazon_places_region": "us-east-1",
  "amazon_places_endpoint": "",
  "places_db_path": "./session/places.db",
  "places_cache_precision": 4,
  "poll_interval": 300,
  "family_members": {
    "Molly's iPhone": "Molly",
    "Arthur's iPhone": "Arthur"
  }
}
```

`family_members` maps iCloud device names to display names. Leave it empty
`{}` to include all devices.

### 3. Configure secrets in `.env`

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required keys:

```dotenv
WEASLEY_APPLE_ID=you@example.com
WEASLEY_TRMNL_API_KEY=your-trmnl-api-key
WEASLEY_TRMNL_PLUGIN_UUID=your-plugin-uuid
WEASLEY_AMAZON_PLACES_API_KEY=your-amazon-places-api-key
```

`WEASLEY_CLIENT_ID` is auto-generated if missing. `WEASLEY_DSID` is captured
after successful authentication.

### 4.5 Reverse geocoding

Weasley resolves lat/lon to a user-visible location label in this order:

1. Manual labels in a local SQLite DB (`places_db_path`, default `./session/places.db`)
2. Cached API lookups in the same DB
3. Amazon Places reverse geocoding API

Use manual labels (with radius tolerance) via CLI:

```bash
python main.py place-add --name "Home" --lat 39.77365 --lon -75.59332 --radius 250
python main.py place-list
python main.py place-remove --name "Home"
# or
python main.py place-remove --id 1
```

### 5. Authenticate

```bash
python main.py auth
```

A browser window will open. Log in to iCloud (including YubiKey if prompted).
When you see the iCloud home screen, press Enter in the terminal.
Weasley will then open iCloud Find; if Apple asks for your password again,
complete that step and press Enter again when Find is fully loaded.

The session is saved to `./session/` — this only needs to be repeated when
the session expires (roughly monthly based on observed cookie lifetimes).

### 6. Run

Single fetch:
```bash
python main.py once
```

Continuous polling:
```bash
python main.py daemon
```

## Architecture

```
main.py       — entry point, CLI, run modes
config.py     — config.json + .env loading/saving
auth.py       — Playwright session management, iCloud auth flow
scraper.py    — validate → initClient → refreshClient API calls
trmnl.py      — TRMNL webhook push
geocoder.py   — manual labels + cache + Amazon reverse geocoding
session/      — gitignored, persistent browser profile + saved cookies
config.json   — gitignored local runtime settings
.env          — gitignored credentials and IDs
```

## Session lifetime

Apple's iCloud session cookies appear to live roughly one month. When the
session expires, Weasley will detect the 450 response and log a warning.
Re-run `python main.py auth` to refresh.

## TRMNL plugin

Create a custom TRMNL plugin and build a Liquid template using the
`merge_variables` structure from `trmnl.py`. A sample template is TODO.

## Notes

- Weasley reads device locations from the iCloud Find My web interface.
  Family members must be sharing their location with your Apple ID.
- Reverse geocoding is supported via manual labels + local cache +
  Amazon Places API.
- The `session/` directory contains sensitive auth data. Don't commit it.
