"""Authentication and custom-domain routing tests for the admin API."""

import json
import os
import sys
from unittest.mock import patch
from urllib.parse import urlencode

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud"))

API_KEY = "test-secret-key"
SIGNING_KEY = "test-session-signing-key-at-least-32-bytes"


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("LOCATIONS_TABLE", "weasley-locations")
    monkeypatch.setenv("PLACES_TABLE", "weasley-places")
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")


def _event(
    method,
    route,
    *,
    body=None,
    headers=None,
    cookies=None,
    path=None,
    path_params=None,
):
    event = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": path or route}},
        "headers": headers or {},
        "pathParameters": path_params or {},
    }
    if body is not None:
        event["body"] = body
    if cookies is not None:
        event["cookies"] = cookies
    return event


def _signed_session(now=None):
    from api.auth import SESSION_COOKIE_NAME, create_session

    token, csrf_token = create_session(SIGNING_KEY, now=now)
    return f"{SESSION_COOKIE_NAME}={token}", csrf_token


class TestSessionTokens:
    def test_valid_session_authenticates(self):
        from api.auth import authenticate

        cookie, csrf_token = _signed_session(now=1_000)
        auth = authenticate(
            _event("GET", "/dashboard", cookies=[cookie]),
            API_KEY,
            SIGNING_KEY,
            now=1_001,
        )

        assert auth is not None
        assert auth.mode == "session"
        assert auth.csrf_token == csrf_token

    def test_expired_session_is_rejected(self):
        from api.auth import authenticate

        cookie, _ = _signed_session(now=1_000)
        auth = authenticate(
            _event("GET", "/dashboard", cookies=[cookie]),
            API_KEY,
            SIGNING_KEY,
            now=50_000,
        )

        assert auth is None

    def test_tampered_session_is_rejected(self):
        from api.auth import authenticate

        cookie, _ = _signed_session(now=1_000)
        auth = authenticate(
            _event("GET", "/dashboard", cookies=[f"{cookie}x"]),
            API_KEY,
            SIGNING_KEY,
            now=1_001,
        )

        assert auth is None


class TestLoginFlow:
    def test_login_page_is_public(self):
        from api.handler import lambda_handler

        result = lambda_handler(_event("GET", "/login"), None)

        assert result["statusCode"] == 200
        assert "Admin secret" in result["body"]
        assert API_KEY not in result["body"]

    def test_valid_login_sets_hardened_cookie(self):
        from api.handler import lambda_handler

        event = _event(
            "POST",
            "/login",
            body=urlencode({"admin_secret": API_KEY}),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 303
        assert result["headers"]["Location"] == "/dashboard"
        cookie = result["cookies"][0]
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Strict" in cookie
        assert API_KEY not in cookie

    def test_login_preserves_stage_path_during_cutover(self):
        from api.handler import lambda_handler

        event = _event(
            "POST",
            "/login",
            path="/prod/login",
            body=urlencode({"admin_secret": API_KEY}),
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 303
        assert result["headers"]["Location"] == "/prod/dashboard"

    def test_invalid_login_does_not_set_cookie(self):
        from api.handler import lambda_handler

        event = _event(
            "POST",
            "/login",
            body=urlencode({"admin_secret": "wrong"}),
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 401
        assert "cookies" not in result
        assert "wrong" not in result["body"]

    @patch("api.handler.get_all_locations", return_value=[])
    def test_session_opens_dashboard_on_custom_domain_path(self, mock_locations):
        from api.handler import lambda_handler

        cookie, _ = _signed_session()
        result = lambda_handler(
            _event("GET", "/dashboard", path="/dashboard", cookies=[cookie]), None
        )

        assert result["statusCode"] == 200
        assert "The Weasley Clock" in result["body"]
        mock_locations.assert_called_once_with()

    def test_unauthenticated_dashboard_redirects_to_login(self):
        from api.handler import lambda_handler

        result = lambda_handler(_event("GET", "/dashboard"), None)

        assert result["statusCode"] == 303
        assert result["headers"]["Location"] == "/login"

    def test_logout_requires_csrf_and_clears_cookie(self):
        from api.handler import lambda_handler

        cookie, csrf_token = _signed_session()
        missing = lambda_handler(
            _event("POST", "/logout", cookies=[cookie], body=""), None
        )
        valid = lambda_handler(
            _event(
                "POST",
                "/logout",
                cookies=[cookie],
                body=urlencode({"csrf_token": csrf_token}),
            ),
            None,
        )

        assert missing["statusCode"] == 403
        assert valid["statusCode"] == 303
        assert "Max-Age=0" in valid["cookies"][0]


class TestBrowserMutationProtection:
    @patch("api.handler.refresh_location_labels", return_value=[])
    @patch("api.handler.create_place")
    def test_session_mutation_requires_matching_csrf(self, mock_create, mock_refresh):
        from api.handler import lambda_handler

        mock_create.return_value = {
            "place_id": "new-id",
            "name": "Office",
            "lat": 42.35,
            "lon": -71.07,
            "radius_m": 200.0,
        }
        cookie, csrf_token = _signed_session()
        body = json.dumps({"name": "Office", "lat": 42.35, "lon": -71.07})

        missing = lambda_handler(
            _event("POST", "/places", cookies=[cookie], body=body), None
        )
        valid = lambda_handler(
            _event(
                "POST",
                "/places",
                cookies=[cookie],
                body=body,
                headers={"x-csrf-token": csrf_token},
            ),
            None,
        )

        assert missing["statusCode"] == 403
        assert valid["statusCode"] == 201
        mock_create.assert_called_once()
        mock_refresh.assert_called_once()

    @patch("api.handler.get_all_locations", return_value=[])
    @patch("api.handler.get_all_places", return_value=[])
    def test_manage_page_contains_csrf_but_no_api_key(
        self, mock_places, mock_locations
    ):
        from api.handler import lambda_handler

        cookie, csrf_token = _signed_session()
        result = lambda_handler(_event("GET", "/manage-places", cookies=[cookie]), None)

        assert result["statusCode"] == 200
        assert csrf_token in result["body"]
        assert API_KEY not in result["body"]
        assert "?key=" not in result["body"]
        assert "onclick=" not in result["body"]
        assert "script-src 'nonce-" in result["headers"]["Content-Security-Policy"]


def test_security_headers_are_applied_to_json_responses():
    from api.handler import lambda_handler

    event = _event("GET", "/unknown", headers={"x-api-key": API_KEY})
    result = lambda_handler(event, None)

    assert result["statusCode"] == 404
    assert result["headers"]["Cache-Control"] == "no-store"
    assert result["headers"]["Referrer-Policy"] == "no-referrer"
    assert result["headers"]["X-Content-Type-Options"] == "nosniff"
