"""
Tests for game_recommender/steam.py — Steam library fetching.

Each function is tested for both the happy path and failure scenarios.
All HTTP calls are mocked so no network access is required.
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock

from game_recommender.steam import get_steam_library


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_resp(json_body: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


def _mock_http_error(status_code: int = 403) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.side_effect = req_lib.HTTPError(f"{status_code}")
    return resp


# ── Success cases ─────────────────────────────────────────────────────────────

class TestGetSteamLibrarySuccess:

    def test_returns_games_sorted_by_playtime_descending(self):
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
        payload = {"response": {"games": [
            {"appid": 292030, "name": "The Witcher 3", "playtime_forever": 7200,
             "rtime_last_played": 1700000000},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        g = games[0]
        assert g.name == "The Witcher 3"
        assert g.platform == "steam"
        assert g.app_id == "292030"
        assert g.playtime_minutes == 7200
        assert g.last_played == "1700000000"

    def test_last_played_zero_maps_to_none(self):
        payload = {"response": {"games": [
            {"appid": 440, "name": "TF2", "playtime_forever": 10, "rtime_last_played": 0},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games[0].last_played is None

    def test_missing_name_field_uses_app_id_fallback(self):
        payload = {"response": {"games": [
            {"appid": 99999, "playtime_forever": 5},  # no "name" key
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games[0].name == "App 99999"

    def test_empty_games_list_returns_empty(self):
        """A library with an explicit empty games array is valid (not private)."""
        payload = {"response": {"games": []}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games == []

    def test_playtime_zero_is_preserved(self):
        payload = {"response": {"games": [
            {"appid": 1, "name": "Unplayed Game", "playtime_forever": 0},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library(api_key="key", user_id="123")

        assert games[0].playtime_minutes == 0

    def test_credentials_from_env_vars(self, monkeypatch):
        monkeypatch.setenv("STEAM_API_KEY", "env_key")
        monkeypatch.setenv("STEAM_USER_ID", "env_uid")
        payload = {"response": {"games": [
            {"appid": 1, "name": "Game", "playtime_forever": 0},
        ]}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            games = get_steam_library()  # no explicit args — uses env vars

        assert len(games) == 1


# ── Failure cases ─────────────────────────────────────────────────────────────

class TestGetSteamLibraryFailure:

    def test_missing_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("STEAM_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key"):
            get_steam_library(api_key=None, user_id="123")

    def test_empty_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("STEAM_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key"):
            get_steam_library(api_key="", user_id="123")

    def test_missing_user_id_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("STEAM_USER_ID", raising=False)
        with pytest.raises(ValueError, match="user ID"):
            get_steam_library(api_key="key", user_id=None)

    def test_private_profile_raises_runtime_error(self):
        """Steam returns an empty response dict when the profile is private."""
        payload = {"response": {}}
        with patch("game_recommender.steam.requests.get", return_value=_mock_resp(payload)):
            with pytest.raises(RuntimeError, match="private"):
                get_steam_library(api_key="key", user_id="123")

    def test_http_403_raises_http_error(self):
        with patch("game_recommender.steam.requests.get", return_value=_mock_http_error(403)):
            with pytest.raises(req_lib.HTTPError):
                get_steam_library(api_key="bad_key", user_id="123")

    def test_http_401_raises_http_error(self):
        with patch("game_recommender.steam.requests.get", return_value=_mock_http_error(401)):
            with pytest.raises(req_lib.HTTPError):
                get_steam_library(api_key="key", user_id="123")

    def test_network_timeout_raises(self):
        with patch("game_recommender.steam.requests.get",
                   side_effect=req_lib.Timeout("timed out")):
            with pytest.raises(req_lib.Timeout):
                get_steam_library(api_key="key", user_id="123")

    def test_connection_error_raises(self):
        with patch("game_recommender.steam.requests.get",
                   side_effect=req_lib.ConnectionError("no route to host")):
            with pytest.raises(req_lib.ConnectionError):
                get_steam_library(api_key="key", user_id="123")
