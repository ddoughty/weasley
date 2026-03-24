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

### 3. Configure secrets

You can store secrets in a plain `.env` file or use 1Password.

#### Option A: Plain `.env` (default)

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

#### Option B: 1Password

Store secrets in 1Password and let the CLI inject them at runtime.

1. Install the 1Password CLI:
   ```bash
   brew install 1password-cli
   ```

2. Create a vault item called **Weasley** (in your Personal vault) with these
   fields: `apple_id`, `trmnl_api_key`, `trmnl_plugin_uuid`,
   `amazon_places_api_key`.

3. The repo includes `.env.op` with `op://` secret references. Run with:
   ```bash
   op run --env-file=.env.op -- python main.py daemon
   ```

Dynamic secrets (`WEASLEY_DSID`, `WEASLEY_CLIENT_ID`) are auto-generated and
persisted to `.env` locally — they don't need to be in 1Password. If `.env`
doesn't exist yet, Weasley will create it on first run.

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

#### Per-user place overrides

Places can be scoped to a specific family member with `--user`. This lets the
same coordinates show different names depending on who is there.

```bash
# Global place — everyone sees "Jeremy's House"
python main.py place-add --name "Jeremy's House" --lat 42.0 --lon -71.0 --radius 200

# Jeremy sees "Home" instead (per-user override takes priority over global)
python main.py place-add --name Home --lat 42.0 --lon -71.0 --radius 200 --user Jeremy

# Jeremy-only place with no global equivalent — others see "Jeremy's Work"
python main.py place-add --name Work --lat 42.1 --lon -71.1 --radius 200 --user Jeremy
```

Resolution order when displaying a location for a given person:

1. **Per-user match** for that person — name as-is (e.g. Jeremy sees "Home")
2. **Global match** — name as-is (e.g. Dennis sees "Jeremy's House")
3. **Another user's place** — auto-prefixed (e.g. Dennis sees "Jeremy's Work")
4. Cached API result → Amazon Places API → raw coordinates

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
# or with 1Password:
op run --env-file=.env.op -- python main.py once
```

Continuous polling:
```bash
python main.py daemon
# or with 1Password:
op run --env-file=.env.op -- python main.py daemon
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
.env          — gitignored credentials and dynamic IDs
.env.op       — 1Password secret references (committed, safe — just pointers)
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
