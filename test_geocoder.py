"""Unit tests for per-user place name overrides in ReverseGeocoder."""

import os
import tempfile

import pytest

from config import Config
from geocoder import ReverseGeocoder


@pytest.fixture()
def geocoder():
    config = Config.load("config.json")
    config.places_db_path = tempfile.mktemp(suffix=".db")
    # Disable external API calls so tests use only manual places.
    config.amazon_places_api_key = ""
    geo = ReverseGeocoder(config)
    yield geo
    try:
        os.unlink(config.places_db_path)
    except FileNotFoundError:
        pass


@pytest.fixture()
def geocoder_with_places(geocoder):
    """Set up a geocoder with a standard set of test places:

    - Global "Jeremy's House" at (42.0, -71.0)
    - Per-user "Home" for Jeremy at same coords
    - Per-user "Work" for Jeremy at (42.1, -71.1) — no global equivalent
    """
    geocoder.add_manual_place("Jeremy's House", 42.0, -71.0, 200.0)
    geocoder.add_manual_place("Home", 42.0, -71.0, 200.0, user="Jeremy")
    geocoder.add_manual_place("Work", 42.1, -71.1, 200.0, user="Jeremy")
    return geocoder


class TestPerUserResolution:
    def test_per_user_match_returns_name_as_is(self, geocoder_with_places):
        result = geocoder_with_places.resolve_label(42.0, -71.0, for_user="Jeremy")
        assert result == "Home"

    def test_global_match_for_other_user(self, geocoder_with_places):
        result = geocoder_with_places.resolve_label(42.0, -71.0, for_user="Dennis")
        assert result == "Jeremy's House"

    def test_other_users_place_gets_prefixed(self, geocoder_with_places):
        result = geocoder_with_places.resolve_label(42.1, -71.1, for_user="Dennis")
        assert result == "Jeremy's Work"

    def test_per_user_match_own_place_no_prefix(self, geocoder_with_places):
        result = geocoder_with_places.resolve_label(42.1, -71.1, for_user="Jeremy")
        assert result == "Work"

    def test_no_user_falls_through_to_global(self, geocoder_with_places):
        result = geocoder_with_places.resolve_label(42.0, -71.0)
        assert result == "Jeremy's House"

    def test_no_user_no_global_prefixes_other_user(self, geocoder_with_places):
        result = geocoder_with_places.resolve_label(42.1, -71.1)
        assert result == "Jeremy's Work"


class TestPerUserStorage:
    def test_add_global_place(self, geocoder):
        place_id = geocoder.add_manual_place("Park", 42.0, -71.0, 100.0)
        places = geocoder.list_manual_places()
        match = [p for p in places if p["id"] == place_id][0]
        assert match["user"] is None
        assert match["name"] == "Park"

    def test_add_per_user_place(self, geocoder):
        place_id = geocoder.add_manual_place("Office", 42.0, -71.0, 100.0, user="Carol")
        places = geocoder.list_manual_places()
        match = [p for p in places if p["id"] == place_id][0]
        assert match["user"] == "Carol"
        assert match["name"] == "Office"

    def test_list_includes_user_field(self, geocoder_with_places):
        places = geocoder_with_places.list_manual_places()
        assert all("user" in p for p in places)
        users = [p["user"] for p in places]
        assert None in users
        assert "Jeremy" in users


class TestEdgeCases:
    def test_no_places_returns_none_from_manual(self, geocoder):
        # With no manual places and no API config, falls through to coordinates
        result = geocoder.resolve_label(42.0, -71.0, for_user="Dennis")
        assert result == "42.0000, -71.0000"

    def test_out_of_radius_no_match(self, geocoder):
        geocoder.add_manual_place("Spot", 42.0, -71.0, 50.0)
        # ~1.1 km away — well outside 50m radius
        result = geocoder.resolve_label(42.01, -71.0, for_user="Dennis")
        assert result == "42.0100, -71.0000"

    def test_closest_per_user_wins(self, geocoder):
        geocoder.add_manual_place("Far", 42.0, -71.0, 500.0, user="Dennis")
        geocoder.add_manual_place("Near", 42.0001, -71.0001, 500.0, user="Dennis")
        result = geocoder.resolve_label(42.0001, -71.0001, for_user="Dennis")
        assert result == "Near"

    def test_per_user_beats_global_even_if_global_closer(self, geocoder):
        geocoder.add_manual_place("Global Close", 42.0, -71.0, 500.0)
        geocoder.add_manual_place("User Far", 42.001, -71.001, 500.0, user="Dennis")
        # Query point is closer to the global place
        result = geocoder.resolve_label(42.0, -71.0, for_user="Dennis")
        assert result == "User Far"

    def test_none_lat_lon_returns_unknown(self, geocoder):
        assert geocoder.resolve_label(None, None) == "Unknown"
        assert geocoder.resolve_label(None, -71.0) == "Unknown"
        assert geocoder.resolve_label(42.0, None) == "Unknown"
