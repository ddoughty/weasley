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
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")


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
    @patch("api.handler.refresh_location_labels")
    @patch("api.handler.create_place")
    def test_creates_place(self, mock_create, mock_refresh):
        from api.handler import lambda_handler

        mock_create.return_value = {
            "place_id": "new-id",
            "name": "Office",
            "lat": 42.35,
            "lon": -71.07,
            "radius_m": 200.0,
        }
        mock_refresh.return_value = []
        event = _api_event(
            "POST",
            "/prod/places",
            body={"name": "Office", "lat": 42.35, "lon": -71.07},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["place"]["name"] == "Office"
        mock_create.assert_called_once_with(
            name="Office", lat=42.35, lon=-71.07, radius_m=200.0, user=None
        )

    @patch("api.handler.refresh_location_labels")
    @patch("api.handler.create_place")
    def test_creates_place_with_optional_fields(self, mock_create, mock_refresh):
        from api.handler import lambda_handler

        mock_create.return_value = {
            "place_id": "new-id",
            "name": "Home",
            "lat": 42.36,
            "lon": -71.06,
            "radius_m": 150.0,
            "user": "Dennis",
        }
        mock_refresh.return_value = []
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
    @patch("api.handler.refresh_location_labels")
    @patch("api.handler.delete_place")
    def test_deletes_place(self, mock_delete, mock_refresh):
        from api.handler import lambda_handler

        mock_delete.return_value = True
        mock_refresh.return_value = []
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


class TestQueryParamAuth:
    @patch("api.handler.get_all_locations")
    def test_auth_via_query_param(self, mock_get):
        from api.handler import lambda_handler

        mock_get.return_value = []
        event = _api_event("GET", "/prod/locations", headers={})
        event["headers"] = {}
        event["queryStringParameters"] = {"key": "test-secret-key"}
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_wrong_query_param_returns_401(self):
        from api.handler import lambda_handler

        event = _api_event("GET", "/prod/locations", headers={})
        event["headers"] = {}
        event["queryStringParameters"] = {"key": "wrong-key"}
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401


class TestDashboard:
    @patch("api.handler.get_all_locations")
    def test_returns_html(self, mock_get):
        from api.handler import lambda_handler

        mock_get.return_value = [
            {
                "person": "Dennis",
                "lat": 42.36,
                "lon": -71.06,
                "location_label": "Home",
                "battery_level": 0.85,
                "battery_status": "Unplugged",
                "timestamp": 1711334400000,
            },
        ]
        event = _api_event("GET", "/prod/dashboard")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert result["headers"]["Content-Type"] == "text/html"
        assert "Dennis" in result["body"]
        assert "Home" in result["body"]
        assert "85%" in result["body"]
        assert "Weasley Clock" in result["body"]

    @patch("api.handler.get_all_locations")
    def test_empty_locations_shows_message(self, mock_get):
        from api.handler import lambda_handler

        mock_get.return_value = []
        event = _api_event("GET", "/prod/dashboard")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert "No family members tracked yet" in result["body"]

    @patch("api.handler.get_all_locations")
    def test_html_escapes_user_data(self, mock_get):
        from api.handler import lambda_handler

        mock_get.return_value = [
            {
                "person": "<script>alert(1)</script>",
                "lat": 42.36,
                "lon": -71.06,
                "location_label": '<img src=x onerror="alert(1)">',
                "timestamp": 1711334400000,
            },
        ]
        event = _api_event("GET", "/prod/dashboard")
        result = lambda_handler(event, None)

        assert "<script>" not in result["body"]
        assert "&lt;script&gt;" in result["body"]
        # Verify the img tag is escaped (angle brackets neutralized)
        assert "<img " not in result["body"]
        assert "&lt;img " in result["body"]

    @patch("api.handler.get_all_locations")
    def test_dashboard_via_query_param_auth(self, mock_get):
        from api.handler import lambda_handler

        mock_get.return_value = []
        event = _api_event("GET", "/prod/dashboard", headers={})
        event["headers"] = {}
        event["queryStringParameters"] = {"key": "test-secret-key"}
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert result["headers"]["Content-Type"] == "text/html"


class TestUpdatePlace:
    @patch("api.handler.refresh_location_labels")
    @patch("api.handler.update_place")
    def test_updates_place(self, mock_update, mock_refresh):
        from api.handler import lambda_handler

        mock_update.return_value = {
            "place_id": "abc-123",
            "name": "New Name",
            "lat": 42.36,
            "lon": -71.06,
            "radius_m": 200.0,
        }
        mock_refresh.return_value = []
        event = _api_event(
            "PUT",
            "/prod/places/abc-123",
            body={"name": "New Name"},
            path_params={"place_id": "abc-123"},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["place"]["name"] == "New Name"
        assert body["label_changes"] == []
        mock_update.assert_called_once_with("abc-123", {"name": "New Name"})

    @patch("api.handler.refresh_location_labels")
    @patch("api.handler.update_place")
    def test_update_returns_label_changes(self, mock_update, mock_refresh):
        from api.handler import lambda_handler

        mock_update.return_value = {
            "place_id": "abc-123",
            "name": "Work",
            "lat": 42.36,
            "lon": -71.06,
            "radius_m": 200.0,
        }
        mock_refresh.return_value = [
            {"person": "Dennis", "old_label": "Office", "new_label": "Work"}
        ]
        event = _api_event(
            "PUT",
            "/prod/places/abc-123",
            body={"name": "Work"},
            path_params={"place_id": "abc-123"},
        )
        result = lambda_handler(event, None)

        body = json.loads(result["body"])
        assert len(body["label_changes"]) == 1
        assert body["label_changes"][0]["new_label"] == "Work"

    @patch("api.handler.update_place")
    def test_update_not_found(self, mock_update):
        from api.handler import lambda_handler

        mock_update.return_value = None
        event = _api_event(
            "PUT",
            "/prod/places/nonexistent",
            body={"name": "X"},
            path_params={"place_id": "nonexistent"},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 404

    def test_update_no_valid_fields(self):
        from api.handler import lambda_handler

        event = _api_event(
            "PUT",
            "/prod/places/abc-123",
            body={"bogus": "field"},
            path_params={"place_id": "abc-123"},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 400

    def test_update_invalid_lat(self):
        from api.handler import lambda_handler

        event = _api_event(
            "PUT",
            "/prod/places/abc-123",
            body={"lat": "not-a-number"},
            path_params={"place_id": "abc-123"},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 400


class TestManagePlacesUI:
    @patch("api.handler.get_all_locations")
    @patch("api.handler.get_all_places")
    def test_returns_html(self, mock_places, mock_locations):
        from api.handler import lambda_handler

        mock_places.return_value = [
            {
                "place_id": "abc",
                "name": "Home",
                "lat": 42.36,
                "lon": -71.06,
                "radius_m": 250,
            }
        ]
        mock_locations.return_value = [
            {
                "person": "Dennis",
                "lat": 42.36,
                "lon": -71.06,
                "location_label": "Home",
            }
        ]
        event = _api_event("GET", "/prod/manage-places")
        event["queryStringParameters"] = {"key": "test-secret-key"}
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert result["headers"]["Content-Type"] == "text/html"
        assert "Manage Places" in result["body"]
        assert "Home" in result["body"]
        assert "Dennis" in result["body"]

    @patch("api.handler.get_all_locations")
    @patch("api.handler.get_all_places")
    def test_html_escapes_place_names(self, mock_places, mock_locations):
        from api.handler import lambda_handler

        mock_places.return_value = [
            {
                "place_id": "xss",
                "name": '<script>alert("xss")</script>',
                "lat": 0,
                "lon": 0,
                "radius_m": 100,
            }
        ]
        mock_locations.return_value = []
        event = _api_event("GET", "/prod/manage-places")
        event["queryStringParameters"] = {"key": "test-secret-key"}
        result = lambda_handler(event, None)

        # The escaped version should appear as an input value
        assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in result["body"]
        # The unescaped alert payload should not appear in the HTML
        assert 'alert("xss")' not in result["body"]

    @patch("api.handler.get_all_locations")
    @patch("api.handler.get_all_places")
    def test_empty_state(self, mock_places, mock_locations):
        from api.handler import lambda_handler

        mock_places.return_value = []
        mock_locations.return_value = []
        event = _api_event("GET", "/prod/manage-places")
        event["queryStringParameters"] = {"key": "test-secret-key"}
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert "No places defined yet" in result["body"]
        assert "No family members tracked yet" in result["body"]

    @patch("api.handler.get_all_locations")
    @patch("api.handler.get_all_places")
    def test_per_user_places_shown(self, mock_places, mock_locations):
        from api.handler import lambda_handler

        mock_places.return_value = [
            {
                "place_id": "a",
                "name": "Apartment",
                "lat": 37.76,
                "lon": -122.39,
                "radius_m": 200,
                "user": "Benjamin",
            }
        ]
        mock_locations.return_value = []
        event = _api_event("GET", "/prod/manage-places")
        event["queryStringParameters"] = {"key": "test-secret-key"}
        result = lambda_handler(event, None)

        assert "Benjamin" in result["body"]
        assert "user-tag" in result["body"]


class TestCreatePlaceLabelRefresh:
    @patch("api.handler.refresh_location_labels")
    @patch("api.handler.create_place")
    def test_create_returns_label_changes(self, mock_create, mock_refresh):
        from api.handler import lambda_handler

        mock_create.return_value = {
            "place_id": "new",
            "name": "Office",
            "lat": 42.35,
            "lon": -71.07,
            "radius_m": 200.0,
        }
        mock_refresh.return_value = [
            {"person": "Dennis", "old_label": "123 Main St", "new_label": "Office"}
        ]
        event = _api_event(
            "POST",
            "/prod/places",
            body={"name": "Office", "lat": 42.35, "lon": -71.07},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["place"]["name"] == "Office"
        assert len(body["label_changes"]) == 1


class TestDeletePlaceLabelRefresh:
    @patch("api.handler.refresh_location_labels")
    @patch("api.handler.delete_place")
    def test_delete_returns_label_changes(self, mock_delete, mock_refresh):
        from api.handler import lambda_handler

        mock_delete.return_value = True
        mock_refresh.return_value = []
        event = _api_event(
            "DELETE",
            "/prod/places/abc-123",
            path_params={"place_id": "abc-123"},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deleted"] == "abc-123"
        assert "label_changes" in body
        mock_refresh.assert_called_once()


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
