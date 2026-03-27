"""Tests for the Weasley REST API Lambda handler."""

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud"))


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-secret-key")
    monkeypatch.setenv("LOCATIONS_TABLE", "weasley-locations")
    monkeypatch.setenv("PLACES_TABLE", "weasley-places")


def _api_event(method, path, body=None, headers=None, path_params=None):
    """Build an API Gateway HTTP API v2 event."""
    event = {
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
            }
        },
        "headers": {"x-api-key": "test-secret-key", **(headers or {})},
        "pathParameters": path_params or {},
    }
    if body is not None:
        event["body"] = json.dumps(body) if isinstance(body, dict) else body
    return event


class TestAuth:
    def test_missing_api_key_returns_401(self):
        from api.handler import lambda_handler

        event = _api_event("GET", "/prod/locations", headers={"x-api-key": ""})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    def test_wrong_api_key_returns_401(self):
        from api.handler import lambda_handler

        event = _api_event("GET", "/prod/locations", headers={"x-api-key": "wrong-key"})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    def test_no_header_returns_401(self):
        from api.handler import lambda_handler

        event = _api_event("GET", "/prod/locations")
        event["headers"] = {}
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401


class TestGetLocations:
    @patch("api.handler.get_all_locations")
    def test_returns_all_locations(self, mock_get):
        from api.handler import lambda_handler

        mock_get.return_value = [
            {"person": "Dennis", "lat": 42.36, "lon": -71.06},
            {"person": "Steph", "lat": 42.35, "lon": -71.07},
        ]
        event = _api_event("GET", "/prod/locations")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body) == 2
        assert body[0]["person"] == "Dennis"

    @patch("api.handler.get_all_locations")
    def test_empty_locations(self, mock_get):
        from api.handler import lambda_handler

        mock_get.return_value = []
        event = _api_event("GET", "/prod/locations")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == []


class TestGetPlaces:
    @patch("api.handler.get_all_places")
    def test_returns_all_places(self, mock_get):
        from api.handler import lambda_handler

        mock_get.return_value = [
            {"place_id": "abc", "name": "Home", "lat": 42.36, "lon": -71.06}
        ]
        event = _api_event("GET", "/prod/places")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body) == 1
        assert body[0]["name"] == "Home"


class TestCreatePlace:
    @patch("api.handler.create_place")
    def test_creates_place(self, mock_create):
        from api.handler import lambda_handler

        mock_create.return_value = {
            "place_id": "new-id",
            "name": "Office",
            "lat": 42.35,
            "lon": -71.07,
            "radius_m": 200.0,
        }
        event = _api_event(
            "POST",
            "/prod/places",
            body={"name": "Office", "lat": 42.35, "lon": -71.07},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["name"] == "Office"
        mock_create.assert_called_once_with(
            name="Office", lat=42.35, lon=-71.07, radius_m=200.0, user=None
        )

    @patch("api.handler.create_place")
    def test_creates_place_with_optional_fields(self, mock_create):
        from api.handler import lambda_handler

        mock_create.return_value = {
            "place_id": "new-id",
            "name": "Home",
            "lat": 42.36,
            "lon": -71.06,
            "radius_m": 150.0,
            "user": "Dennis",
        }
        event = _api_event(
            "POST",
            "/prod/places",
            body={
                "name": "Home",
                "lat": 42.36,
                "lon": -71.06,
                "radius_m": 150,
                "user": "Dennis",
            },
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 201
        mock_create.assert_called_once_with(
            name="Home", lat=42.36, lon=-71.06, radius_m=150.0, user="Dennis"
        )

    def test_missing_required_fields(self):
        from api.handler import lambda_handler

        event = _api_event("POST", "/prod/places", body={"name": "Office"})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 400
        assert "Missing required fields" in json.loads(result["body"])["error"]

    def test_invalid_json_body(self):
        from api.handler import lambda_handler

        event = _api_event("POST", "/prod/places")
        event["body"] = "not json{"
        result = lambda_handler(event, None)

        assert result["statusCode"] == 400

    def test_invalid_lat_lon_types(self):
        from api.handler import lambda_handler

        event = _api_event(
            "POST",
            "/prod/places",
            body={"name": "Bad", "lat": "not-a-number", "lon": -71.0},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 400


class TestDeletePlace:
    @patch("api.handler.delete_place")
    def test_deletes_place(self, mock_delete):
        from api.handler import lambda_handler

        mock_delete.return_value = True
        event = _api_event(
            "DELETE",
            "/prod/places/abc-123",
            path_params={"place_id": "abc-123"},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deleted"] == "abc-123"
        mock_delete.assert_called_once_with("abc-123")


class TestRouting:
    def test_unknown_route_returns_404(self):
        from api.handler import lambda_handler

        event = _api_event("GET", "/prod/unknown")
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_json_content_type_header(self):
        from api.handler import lambda_handler

        event = _api_event("GET", "/prod/unknown")
        result = lambda_handler(event, None)
        assert result["headers"]["Content-Type"] == "application/json"
