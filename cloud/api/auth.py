"""Authentication helpers for the Weasley admin API."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie

SESSION_COOKIE_NAME = "weasley_session"
SESSION_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class AuthContext:
    """Identity information established for one request."""

    mode: str
    csrf_token: str | None = None


def authenticate(
    event: dict,
    api_key: str,
    signing_key: str,
    *,
    now: int | None = None,
) -> AuthContext | None:
    """Authenticate an API key header or a signed browser session."""
    headers = _normalized_headers(event)
    provided_key = headers.get("x-api-key", "")
    if api_key and provided_key and secrets.compare_digest(provided_key, api_key):
        return AuthContext(mode="api-key")

    token = _session_cookie(event, headers)
    payload = validate_session(token, signing_key, now=now)
    if payload is None:
        return None
    return AuthContext(mode="session", csrf_token=payload["csrf"])


def create_session(
    signing_key: str,
    *,
    now: int | None = None,
    ttl_seconds: int = SESSION_TTL_SECONDS,
) -> tuple[str, str]:
    """Create a signed session token and its CSRF token."""
    if not signing_key:
        raise ValueError("A session signing key is required")

    issued_at = int(time.time() if now is None else now)
    csrf_token = secrets.token_urlsafe(24)
    payload = {
        "v": 1,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "csrf": csrf_token,
    }
    encoded_payload = _encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        signing_key.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded_payload}.{_encode(signature)}", csrf_token


def validate_session(
    token: str,
    signing_key: str,
    *,
    now: int | None = None,
) -> dict | None:
    """Validate and decode a session token, returning None when invalid."""
    if not token or not signing_key:
        return None

    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        supplied_signature = _decode(encoded_signature)
        expected_signature = hmac.new(
            signing_key.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_decode(encoded_payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None

    current_time = int(time.time() if now is None else now)
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or not isinstance(payload.get("iat"), int)
        or not isinstance(payload.get("exp"), int)
        or not isinstance(payload.get("csrf"), str)
        or not payload["csrf"]
        or payload["iat"] > current_time + 60
        or payload["exp"] <= current_time
    ):
        return None
    return payload


def csrf_is_valid(
    event: dict,
    auth: AuthContext,
    *,
    form_token: str = "",
) -> bool:
    """Require a matching CSRF token for cookie-authenticated mutations."""
    if auth.mode == "api-key":
        return True
    supplied = _normalized_headers(event).get("x-csrf-token", "") or form_token
    return bool(
        auth.csrf_token
        and supplied
        and secrets.compare_digest(supplied, auth.csrf_token)
    )


def session_cookie(token: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Build the hardened browser session cookie."""
    return (
        f"{SESSION_COOKIE_NAME}={token}; Max-Age={ttl_seconds}; Path=/; "
        "Secure; HttpOnly; SameSite=Strict"
    )


def clear_session_cookie() -> str:
    """Build an expired browser session cookie."""
    return (
        f"{SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; "
        "Secure; HttpOnly; SameSite=Strict"
    )


def _normalized_headers(event: dict) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in (event.get("headers") or {}).items()
        if value is not None
    }


def _session_cookie(event: dict, headers: dict[str, str]) -> str:
    cookie_values = list(event.get("cookies") or [])
    if headers.get("cookie"):
        cookie_values.append(headers["cookie"])
    if not cookie_values:
        return ""

    cookies = SimpleCookie()
    try:
        cookies.load("; ".join(cookie_values))
    except CookieError:
        return ""
    morsel = cookies.get(SESSION_COOKIE_NAME)
    return morsel.value if morsel else ""


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
