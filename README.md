# Weasley

> *"It's a clock, but instead of hands, there are little figures of family members..."*
> — Harry Potter and the Chamber of Secrets

Weasley polls Apple's Find My via iCloud web and shows family member locations
on a [TRMNL](https://usetrmnl.com) e-ink display.

## Architecture

```
Desktop (macOS)                          AWS Cloud
+-----------------+                      +----------------------------------+
| main.py         |    SQS              | Location Processor Lambda        |
|  auth.py        | -- RawLocationEvent -->  resolve labels (places/cache/  |
|  scraper.py     |                      |    Amazon Places API)            |
|  publisher.py   |                      |  detect movement/heartbeat       |
+-----------------+                      |  store in DynamoDB               |
                                         |  publish EnrichedLocationEvent   |
                                         +----------|---------------------+
                                                    | SNS
                                         +----------|---------------------+
                                         | SQS buffer (15s batch window)  |
                                         +----------|---------------------+
                                                    |
                                         +----------|---------------------+
                                         | TRMNL Consumer Lambda          |
                                         |  read ALL locations from Dynamo |
                                         |  push to TRMNL display webhook |
                                         +----------------------------------+
                                         | REST API Lambda (API Gateway)   |
                                         |  GET/POST/DELETE places + locs  |
                                         +----------------------------------+
```

The desktop agent **only** handles iCloud authentication (which requires macOS
Keychain) and location scraping. All geocoding, display updates, and place
management are handled by the cloud pipeline.

## Setup

### 1. Install dependencies

```bash
pipenv install
playwright install chromium
```

### 2. Configure runtime settings

Copy and fill in `config.json`:

```json
{
  "client_build_number": "2604Build20",
  "client_mastering_number": "2604Build20",
  "session_dir": "./session",
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

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required keys:

```dotenv
WEASLEY_APPLE_ID=you@example.com
WEASLEY_SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT/weasley-raw-locations
WEASLEY_API_URL=https://XXXXXX.execute-api.us-east-1.amazonaws.com/prod
WEASLEY_API_KEY=your-api-key
```

`WEASLEY_CLIENT_ID` is auto-generated if missing. `WEASLEY_DSID` is captured
after successful authentication.

#### 1Password integration

Store secrets in 1Password and use `op://` references in `.env.op`:

```bash
op run --env-file=.env.op -- python main.py daemon
```

### 4. Authenticate

```bash
python main.py auth
```

A browser window will open. Log in to iCloud (including YubiKey if prompted).
When you see the iCloud home screen, press Enter in the terminal.
The session is saved to `./session/` and typically lasts about a month.

### 5. Run

Single fetch:
```bash
python main.py once
```

Continuous polling:
```bash
python main.py daemon
```

## Cloud Deployment

The cloud pipeline is defined in `cloud/template.yaml` (AWS SAM).

```bash
cd cloud && ./deploy.sh
```

This deploys:
- **SQS queue** (`weasley-raw-locations`) — receives raw events from desktop
- **Location Processor Lambda** — resolves labels, detects triggers, stores state
- **SNS topic** (`weasley-location-changes`) — fans out enriched events
- **SQS buffer** (`weasley-trmnl-buffer`) — debounces per-member updates (15s window)
- **TRMNL Consumer Lambda** — pushes all locations to TRMNL display
- **REST API Lambda** (API Gateway) — locations and places CRUD
- **DynamoDB tables** — locations, places, geocode-cache

## REST API

All requests require an `x-api-key` header.

### Get all family member locations

```bash
curl -s -H "x-api-key: $WEASLEY_API_KEY" \
  https://XXXXXX.execute-api.us-east-1.amazonaws.com/prod/locations
```

### Get all places

```bash
curl -s -H "x-api-key: $WEASLEY_API_KEY" \
  https://XXXXXX.execute-api.us-east-1.amazonaws.com/prod/places
```

### Create a place

```bash
curl -s -X POST -H "x-api-key: $WEASLEY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Home", "lat": 42.3375, "lon": -71.1171, "radius_m": 250}' \
  https://XXXXXX.execute-api.us-east-1.amazonaws.com/prod/places
```

Per-user place (only applies when resolving this person's location):

```bash
curl -s -X POST -H "x-api-key: $WEASLEY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Apartment", "lat": 42.387, "lon": -71.116, "radius_m": 200, "user": "Jeremy"}' \
  https://XXXXXX.execute-api.us-east-1.amazonaws.com/prod/places
```

### Delete a place

```bash
curl -s -X DELETE -H "x-api-key: $WEASLEY_API_KEY" \
  https://XXXXXX.execute-api.us-east-1.amazonaws.com/prod/places/PLACE_ID
```

## Desktop files

```
main.py         — entry point: auth, scrape, publish to SQS
config.py       — config.json + .env loading
auth.py         — Playwright session management, iCloud auth flow
scraper.py      — iCloud Find My API calls
publisher.py    — SQS event publisher
credentials.py  — macOS Keychain access
session/        — gitignored, persistent browser profile + saved cookies
```

## Session lifetime

Apple's iCloud session cookies live roughly one month. When the session
expires, Weasley detects the 450 response and logs a warning. Re-run
`python main.py auth` to refresh.

## TRMNL plugin

Create a custom TRMNL plugin and build a Liquid template using the
`merge_variables` structure. Each push includes:

```json
{
  "merge_variables": {
    "members": [
      {"name": "Molly", "lat": 42.34, "lon": -71.11, "battery_level": "85%",
       "battery_status": "Charging", "last_seen": "03:45 PM", "location_label": "Home"}
    ],
    "updated_at": "03:45 PM",
    "member_count": 1
  }
}
```

## Notes

- Family members must be sharing their location with your Apple ID.
- The `session/` directory contains sensitive auth data — don't commit it.
- Place resolution order: per-user manual place > global manual place > geocode cache > Amazon Places API.
