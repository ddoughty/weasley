"""
Weasley configuration.

Non-sensitive runtime settings live in config.json.
Credentials/session identifiers are loaded from environment variables
(optionally from a local .env file).
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import ClassVar

log = logging.getLogger("weasley.config")


def _strip_optional_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def _load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return

    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = _strip_optional_quotes(value)
            if key and key not in os.environ:
                os.environ[key] = value


def _quote_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f"\"{escaped}\""


def _is_uuid(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return str(parsed) == value.lower()


def _upsert_dotenv(path: str, key: str, value: str) -> None:
    new_line = f"{key}={_quote_env_value(value)}\n"
    lines: list[str] = []
    found = False

    if os.path.exists(path):
        with open(path) as f:
            lines = f.readlines()

    updated: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            candidate = stripped[7:].strip()
        else:
            candidate = stripped

        if candidate and not candidate.startswith("#") and "=" in candidate:
            existing_key = candidate.split("=", 1)[0].strip()
            if existing_key == key:
                updated.append(new_line)
                found = True
                continue

        updated.append(line if line.endswith("\n") else f"{line}\n")

    if not found:
        if updated and updated[-1].strip():
            updated.append("\n")
        updated.append(new_line)

    env_dir = os.path.dirname(path)
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)

    with open(path, "w") as f:
        f.writelines(updated)


@dataclass
class Config:
    # iCloud credentials / session params (recommended via .env)
    apple_id: str = ""
    dsid: str = ""                  # numeric Apple ID, from validate response
    client_id: str = ""             # GUID sent to FMIP endpoints

    # Build numbers — update if Apple changes these
    client_build_number: str = "2604Build20"
    client_mastering_number: str = "2604Build20"

    # Playwright session storage
    session_dir: str = "./session"  # persistent browser profile lives here

    # TRMNL
    trmnl_api_key: str = ""
    trmnl_plugin_uuid: str = ""     # your custom plugin UUID

    # Reverse geocoding
    amazon_places_api_key: str = ""
    amazon_places_region: str = "us-east-1"
    amazon_places_endpoint: str = ""
    places_db_path: str = "./session/places.db"
    places_cache_precision: int = 4

    # Polling
    poll_interval: int = 300        # seconds between refreshes (5 min default)

    # Family members to track: maps iCloud device name -> display name
    # e.g. {"Dennis's iPhone": "Dennis"}
    # If empty, all devices will be included.
    family_members: dict[str, str] = field(default_factory=dict)

    _env_path: str = field(default=".env", init=False, repr=False)

    SECRET_ENV_VARS: ClassVar[dict[str, str]] = {
        "apple_id": "WEASLEY_APPLE_ID",
        "dsid": "WEASLEY_DSID",
        "client_id": "WEASLEY_CLIENT_ID",
        "trmnl_api_key": "WEASLEY_TRMNL_API_KEY",
        "trmnl_plugin_uuid": "WEASLEY_TRMNL_PLUGIN_UUID",
        "amazon_places_api_key": "WEASLEY_AMAZON_PLACES_API_KEY",
    }

    @classmethod
    def load(cls, path: str, env_path: str = ".env") -> "Config":
        _load_dotenv(env_path)

        if not os.path.exists(path):
            # Write a template config and exit helpfully
            template = cls()
            template._env_path = env_path
            with open(path, "w") as f:
                json.dump(template.to_dict(include_secrets=False), f, indent=2)
            print(f"No config found. A template has been written to {path}.")
            print("Credentials should be stored in .env (see .env.example).")
            print("Please fill it in and run again.")
            raise SystemExit(1)

        with open(path) as f:
            data = json.load(f)

        config = cls()
        config._env_path = env_path
        for k, v in data.items():
            if hasattr(config, k):
                setattr(config, k, v)

        config._apply_env_overrides()
        config._ensure_client_id()
        return config

    def save(self, path: str, include_secrets: bool = False):
        with open(path, "w") as f:
            json.dump(self.to_dict(include_secrets=include_secrets), f, indent=2)

    def to_dict(self, include_secrets: bool = False) -> dict:
        data = {
            "client_build_number": self.client_build_number,
            "client_mastering_number": self.client_mastering_number,
            "session_dir": self.session_dir,
            "poll_interval": self.poll_interval,
            "amazon_places_region": self.amazon_places_region,
            "amazon_places_endpoint": self.amazon_places_endpoint,
            "places_db_path": self.places_db_path,
            "places_cache_precision": self.places_cache_precision,
            "family_members": self.family_members,
        }
        if include_secrets:
            data.update({
                "apple_id": self.apple_id,
                "dsid": self.dsid,
                "client_id": self.client_id,
                "trmnl_api_key": self.trmnl_api_key,
                "trmnl_plugin_uuid": self.trmnl_plugin_uuid,
                "amazon_places_api_key": self.amazon_places_api_key,
            })
        return data

    def set_secret(self, field_name: str, value: str):
        if field_name not in self.SECRET_ENV_VARS:
            raise ValueError(f"Unknown secret field: {field_name}")

        value = str(value)
        setattr(self, field_name, value)

        env_key = self.SECRET_ENV_VARS[field_name]
        os.environ[env_key] = value
        _upsert_dotenv(self._env_path, env_key, value)

    def _apply_env_overrides(self):
        for field_name, env_key in self.SECRET_ENV_VARS.items():
            env_value = os.getenv(env_key)
            if env_value:
                setattr(self, field_name, env_value)

    def _ensure_client_id(self):
        if self.client_id and _is_uuid(self.client_id):
            return
        if self.client_id:
            log.warning(
                "WEASLEY_CLIENT_ID is not a UUID; generating a new UUID client_id."
            )
        self.set_secret("client_id", str(uuid.uuid4()))

    @property
    def fmip_params(self) -> dict:
        params = {
            "clientBuildNumber": self.client_build_number,
            "clientMasteringNumber": self.client_mastering_number,
            "clientId": self.client_id,
        }
        if self.dsid:
            params["dsid"] = self.dsid
        return params
