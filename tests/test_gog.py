"""
Tests for game_recommender/gog.py — GOG auth and library fetching.
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock

from game_recommender.gog import exchange_code, refresh_tokens, get_gog_library


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_resp(json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def _http_error_resp(code: int = 401) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.side_effect = req_lib.HTTPError(str(code))
    return resp


def _products_resp(products: list, total_pages: int = 1) -> MagicMock:
    return _get_resp({"products": products, "totalPages": total_pages})


# ── exchange_code ─────────────────────────────────────────────────────────────

class TestGogExchangeCode:

    def test_success_returns_token_dict(self):
        body = {
            "access_token": "gog_tok",
            "refresh_token": "gog_ref",
            "expires_in": 3600,
            "user_id": "gog_user",
        }
        with patch("game_recommender.gog.requests.get", return_value=_get_resp(body)) as mock_get:
            result = exchange_code("auth_code_abc")

        assert result["access_token"] == "gog_tok"
        assert result["refresh_token"] == "gog_ref"
        params = mock_get.call_args[1]["params"]
        assert params["grant_type"] == "authorization_code"
        assert params["code"] == "auth_code_abc"

    def test_failure_invalid_code_raises_http_error(self):
        with patch("game_recommender.gog.requests.get", return_value=_http_error_resp(400)):
            with pytest.raises(req_lib.HTTPError):
                exchange_code("bad_code")

    def test_failure_network_error_propagates(self):
        with patch("game_recommender.gog.requests.get",
                   side_effect=req_lib.ConnectionError("no route")):
            with pytest.raises(req_lib.ConnectionError):
                exchange_code("code")


# ── refresh_tokens ────────────────────────────────────────────────────────────

class TestGogRefreshTokens:

    def test_success_returns_new_tokens(self):
        body = {"access_token": "new_gog_tok", "refresh_token": "new_gog_ref", "expires_in": 3600}
        with patch("game_recommender.gog.requests.get", return_value=_get_resp(body)) as mock_get:
            result = refresh_tokens("old_ref")

        assert result["access_token"] == "new_gog_tok"
        params = mock_get.call_args[1]["params"]
        assert params["grant_type"] == "refresh_token"
        assert params["refresh_token"] == "old_ref"

    def test_failure_expired_token_raises_http_error(self):
        with patch("game_recommender.gog.requests.get", return_value=_http_error_resp(401)):
            with pytest.raises(req_lib.HTTPError):
                refresh_tokens("expired_token")


# ── get_gog_library ───────────────────────────────────────────────────────────

class TestGetGogLibrary:

    def test_success_returns_games(self):
        resp = _products_resp([
            {"id": 1, "title": "The Witcher"},
            {"id": 2, "title": "Cyberpunk 2077"},
        ])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert len(games) == 2
        names = {g.name for g in games}
        assert "The Witcher" in names
        assert "Cyberpunk 2077" in names
        assert all(g.platform == "gog" for g in games)
        assert all(g.playtime_minutes == 0 for g in games)

    def test_games_sorted_alphabetically(self):
        resp = _products_resp([
            {"id": 3, "title": "Zelda"},
            {"id": 1, "title": "Alpha"},
            {"id": 2, "title": "Middle"},
        ])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert [g.name for g in games] == ["Alpha", "Middle", "Zelda"]

    def test_app_id_is_string(self):
        resp = _products_resp([{"id": 12345, "title": "My Game"}])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert games[0].app_id == "12345"

    def test_pagination_fetches_all_pages(self):
        page1 = _products_resp([{"id": 1, "title": "Alpha"}], total_pages=2)
        page2 = _products_resp([{"id": 2, "title": "Beta"}],  total_pages=2)
        with patch("game_recommender.gog.requests.get", side_effect=[page1, page2]) as mock_get:
            games = get_gog_library("tok")

        assert len(games) == 2
        assert mock_get.call_count == 2
        # Second call should use page=2
        second_params = mock_get.call_args_list[1][1]["params"]
        assert second_params["page"] == 2

    def test_skips_blank_titles(self):
        resp = _products_resp([
            {"id": 1, "title": ""},          # blank
            {"id": 2, "title": "   "},       # whitespace
            {"id": 3, "title": "Good Game"},
        ])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert len(games) == 1
        assert games[0].name == "Good Game"

    def test_empty_library_returns_empty_list(self):
        resp = _products_resp([])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert games == []

    def test_http_error_propagates(self):
        with patch("game_recommender.gog.requests.get", return_value=_http_error_resp(403)):
            with pytest.raises(req_lib.HTTPError):
                get_gog_library("bad_tok")

    def test_network_error_propagates(self):
        with patch("game_recommender.gog.requests.get",
                   side_effect=req_lib.ConnectionError("timeout")):
            with pytest.raises(req_lib.ConnectionError):
                get_gog_library("tok")
