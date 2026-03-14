"""
Tests for game_recommender/steam.py — Steam library fetching.

Each public function is tested for both the happy path and failure scenarios.
All HTTP calls are mocked with unittest.mock so no network access is required.
The tests are organized into three classes:
  - TestGetSteamLibrarySuccess — normal, expected responses
  - TestGetSteamLibraryFailure — error conditions that should raise
  - TestGetSteamLibraryCornerCases — edge cases and documented gotchas
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock

from game_recommender.steam import get_steam_library


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_resp(json_body: dict, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response with the given JSON body and status code.

    raise_for_status is a no-op (simulates a successful HTTP call).
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


def _mock_http_error(status_code: int = 403) -> MagicMock:
    """Build a mock requests.Response whose raise_for_status() raises HTTPError.

    Used to simulate non-2xx responses (bad API key, rate limit, etc.).
    """
    resp = MagicMock()
    resp.raise_for_status.side_effect = req_lib.HTTPError(f"{status_code}")
    return resp


# ── Success cases ─────────────────────────────────────────────────────────────

class TestGetSteamLibrarySuccess:

    def test_returns_games_sorted_by_playtime_descending(self):
        """Library is always returned most-played first, regardless of API order."""
        payload = {"response": {"games": [
            {"appid": 570,  "name": "Dota 2",            "playtime_forever": 30},
            {"appid": 730,  "name": "Counter-Strike 2",  "playtime_forever": 600},
            {"appid": 440,  "name": "Team Fortress 2",   "playtime_forever": 120},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert [g.name for g in games] == [
            "Counter-Strike 2",
            "Team Fortress 2",
            "Dota 2",
        ]

    def test_game_fields_populated_correctly(self):
        """All Game fields are mapped correctly from the Steam API response."""
        payload = {"response": {"games": [
            {"appid": 292030, "name": "The Witcher 3", "playtime_forever": 7200,
             "rtime_last_played": 1700000000},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        g = games[0]
        assert g.name == "The Witcher 3"
        assert g.platform == "steam"
        assert g.app_id == "292030"          # Must be a string, not int
        assert g.playtime_minutes == 7200
        assert g.last_played == "1700000000"  # Unix timestamp stored as string

    def test_last_played_zero_maps_to_none(self):
        """rtime_last_played=0 means "never played" — should produce last_played=None."""
        payload = {"response": {"games": [
            {"appid": 440, "name": "TF2", "playtime_forever": 10, "rtime_last_played": 0},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        # 0 is falsy, so the conditional `if g.get("rtime_last_played")` is False
        assert games[0].last_played is None

    def test_missing_name_field_uses_app_id_fallback(self):
        """If the "name" key is absent (e.g. delisted game), fall back to "App <appid>"."""
        payload = {"response": {"games": [
            {"appid": 99999, "playtime_forever": 5},  # no "name" key
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games[0].name == "App 99999"

    def test_empty_games_list_returns_empty(self):
        """A public library with zero games is valid and should return an empty list."""
        payload = {"response": {"games": []}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games == []

    def test_playtime_zero_is_preserved(self):
        """A game with playtime_forever=0 is unplayed and should appear with playtime_minutes=0."""
        payload = {"response": {"games": [
            {"appid": 1, "name": "Unplayed Game", "playtime_forever": 0},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games[0].playtime_minutes == 0

    def test_credentials_from_env_vars(self, monkeypatch):
        """When called with no args, credentials should be read from environment variables."""
        monkeypatch.setenv("STEAM_API_KEY", "env_key")
        monkeypatch.setenv("STEAM_USER_ID", "env_uid")
        payload = {"response": {"games": [
            {"appid": 1, "name": "Game", "playtime_forever": 0},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library()  # no explicit args — must fall back to env vars

        assert len(games) == 1


# ── Failure cases ─────────────────────────────────────────────────────────────

class TestGetSteamLibraryFailure:

    def test_missing_api_key_raises_value_error(self, monkeypatch):
        """No API key in args or env should raise ValueError before any HTTP call."""
        monkeypatch.delenv("STEAM_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key"):
            get_steam_library(api_key=None, user_id="123")

    def test_empty_api_key_raises_value_error(self, monkeypatch):
        """An empty string API key is treated the same as None (falsy)."""
        monkeypatch.delenv("STEAM_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key"):
            get_steam_library(api_key="", user_id="123")

    def test_missing_user_id_raises_value_error(self, monkeypatch):
        """No user ID should raise ValueError with a message mentioning "user ID"."""
        monkeypatch.delenv("STEAM_USER_ID", raising=False)
        with pytest.raises(ValueError, match="user ID"):
            get_steam_library(api_key="key", user_id=None)

    def test_private_profile_raises_runtime_error(self):
        """Steam returns 200 OK with {"response": {}} for private profiles."""
        payload = {"response": {}}  # Empty dict — Steam's private profile response
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            with pytest.raises(RuntimeError, match="private"):
                get_steam_library(api_key="key", user_id="123")

    def test_http_403_raises_http_error(self):
        """A 403 response (bad API key) should propagate as HTTPError."""
        with patch("game_recommender.steam.requests.get", return_value=_mock_http_error(403)):
            with pytest.raises(req_lib.HTTPError):
                get_steam_library(api_key="bad_key", user_id="123")

    def test_http_401_raises_http_error(self):
        """A 401 response (unauthorized) should also propagate as HTTPError."""
        with patch("game_recommender.steam.requests.get", return_value=_mock_http_error(401)):
            with pytest.raises(req_lib.HTTPError):
                get_steam_library(api_key="key", user_id="123")

    def test_network_timeout_raises(self):
        """A timeout (no response) should propagate as requests.Timeout."""
        with patch("game_recommender.steam.requests.get",
                   side_effect=req_lib.Timeout("timed out")):
            with pytest.raises(req_lib.Timeout):
                get_steam_library(api_key="key", user_id="123")

    def test_connection_error_raises(self):
        """A connection error (DNS failure, refused) should propagate as ConnectionError."""
        with patch("game_recommender.steam.requests.get",
                   side_effect=req_lib.ConnectionError("no route to host")):
            with pytest.raises(req_lib.ConnectionError):
                get_steam_library(api_key="key", user_id="123")


# ── Corner cases ──────────────────────────────────────────────────────────────

class TestGetSteamLibraryCornerCases:

    def test_empty_string_name_is_not_replaced_by_fallback(self):
        """
        dict.get("name", default) only uses the default when the key is MISSING.
        When "name" is present but set to "", the empty string passes through —
        the fallback "App {appid}" is NOT triggered.
        This documents the current behaviour so it doesn't change silently.
        """
        payload = {"response": {"games": [
            {"appid": 9999, "name": "", "playtime_forever": 0},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games[0].name == ""  # Empty string is kept as-is

    def test_games_with_identical_playtime_all_returned(self):
        """When all games have the same playtime, sorted() must not drop any."""
        payload = {"response": {"games": [
            {"appid": 1, "name": "Alpha", "playtime_forever": 120},
            {"appid": 2, "name": "Beta",  "playtime_forever": 120},
            {"appid": 3, "name": "Gamma", "playtime_forever": 120},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert len(games) == 3
        # Order is undefined for ties, but all three must be present
        assert {g.name for g in games} == {"Alpha", "Beta", "Gamma"}

    def test_very_large_playtime_does_not_crash(self):
        """Steam could theoretically return an extremely large playtime_forever value."""
        payload = {"response": {"games": [
            {"appid": 1, "name": "No Lifer", "playtime_forever": 999_999_999},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games[0].playtime_minutes == 999_999_999

    def test_single_game_library_returns_one_item_list(self):
        """Sorting a one-element list should never raise and should return that element."""
        payload = {"response": {"games": [
            {"appid": 730, "name": "Only Game", "playtime_forever": 5},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert len(games) == 1
        assert games[0].name == "Only Game"

    def test_playtime_is_integer_and_hours_rounds_to_one_decimal(self):
        """playtime_minutes must be stored as int; playtime_hours must be rounded to 1dp."""
        payload = {"response": {"games": [
            {"appid": 1, "name": "Game", "playtime_forever": 90},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert isinstance(games[0].playtime_minutes, int)
        assert games[0].playtime_hours == 1.5  # 90 / 60 = 1.5 exactly
