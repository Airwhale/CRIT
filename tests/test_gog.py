"""
Tests for game_recommender/gog.py — GOG auth and library fetching.

Covers:
  - exchange_code: token exchange via GET (GOG uses GET, not POST)
  - refresh_tokens: token refresh via GET
  - get_gog_library: page-based pagination, title normalization, app_id handling
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock

from game_recommender.gog import exchange_code, refresh_tokens, get_gog_library


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_resp(json_body: dict) -> MagicMock:
    """Mock a successful GET response (raise_for_status is a no-op)."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def _http_error_resp(code: int = 401) -> MagicMock:
    """Mock a failed response that raises HTTPError on raise_for_status."""
    resp = MagicMock()
    resp.raise_for_status.side_effect = req_lib.HTTPError(str(code))
    return resp


def _products_resp(products: list, total_pages: int = 1) -> MagicMock:
    """Build a mock getFilteredProducts response with the given products list.

    total_pages controls the pagination loop — if page >= total_pages, iteration stops.
    """
    return _get_resp({"products": products, "totalPages": total_pages})


# ── exchange_code ─────────────────────────────────────────────────────────────

class TestGogExchangeCode:

    def test_success_returns_token_dict(self):
        """Successful code exchange returns all expected token fields."""
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
        # GOG uses GET with query params (not POST with body)
        params = mock_get.call_args[1]["params"]
        assert params["grant_type"] == "authorization_code"
        assert params["code"] == "auth_code_abc"

    def test_failure_invalid_code_raises_http_error(self):
        """A bad auth code gets a 400 response which should raise HTTPError."""
        with patch("game_recommender.gog.requests.get", return_value=_http_error_resp(400)):
            with pytest.raises(req_lib.HTTPError):
                exchange_code("bad_code")

    def test_failure_network_error_propagates(self):
        """A network error during the exchange should propagate unchanged."""
        with patch("game_recommender.gog.requests.get",
                   side_effect=req_lib.ConnectionError("no route")):
            with pytest.raises(req_lib.ConnectionError):
                exchange_code("code")


# ── refresh_tokens ────────────────────────────────────────────────────────────

class TestGogRefreshTokens:

    def test_success_returns_new_tokens(self):
        """Token refresh returns a new token dict with fresh access and refresh tokens."""
        body = {"access_token": "new_gog_tok", "refresh_token": "new_gog_ref", "expires_in": 3600}
        with patch("game_recommender.gog.requests.get", return_value=_get_resp(body)) as mock_get:
            result = refresh_tokens("old_ref")

        assert result["access_token"] == "new_gog_tok"
        params = mock_get.call_args[1]["params"]
        assert params["grant_type"] == "refresh_token"
        assert params["refresh_token"] == "old_ref"

    def test_failure_expired_token_raises_http_error(self):
        """An expired refresh token results in a 401, which should raise HTTPError."""
        with patch("game_recommender.gog.requests.get", return_value=_http_error_resp(401)):
            with pytest.raises(req_lib.HTTPError):
                refresh_tokens("expired_token")


# ── get_gog_library ───────────────────────────────────────────────────────────

class TestGetGogLibrary:

    def test_success_returns_games(self):
        """Two products → two Game objects with gog platform and 0 playtime."""
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
        # GOG exposes no playtime API — all games must show 0
        assert all(g.playtime_minutes == 0 for g in games)

    def test_games_sorted_alphabetically(self):
        """Output is sorted alphabetically regardless of API response order."""
        resp = _products_resp([
            {"id": 3, "title": "Zelda"},
            {"id": 1, "title": "Alpha"},
            {"id": 2, "title": "Middle"},
        ])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert [g.name for g in games] == ["Alpha", "Middle", "Zelda"]

    def test_app_id_is_string(self):
        """GOG product IDs are integers in the API but must be stored as strings."""
        resp = _products_resp([{"id": 12345, "title": "My Game"}])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert games[0].app_id == "12345"  # str, not int

    def test_pagination_fetches_all_pages(self):
        """When totalPages > 1, multiple GET requests are made with increasing page numbers."""
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
        """Products with empty or whitespace-only titles are silently skipped."""
        resp = _products_resp([
            {"id": 1, "title": ""},          # empty string
            {"id": 2, "title": "   "},       # whitespace only
            {"id": 3, "title": "Good Game"},
        ])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert len(games) == 1
        assert games[0].name == "Good Game"

    def test_empty_library_returns_empty_list(self):
        """An account with no GOG games returns an empty list without error."""
        resp = _products_resp([])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert games == []

    def test_http_error_propagates(self):
        """A 403 from the products endpoint propagates as HTTPError."""
        with patch("game_recommender.gog.requests.get", return_value=_http_error_resp(403)):
            with pytest.raises(req_lib.HTTPError):
                get_gog_library("bad_tok")

    def test_network_error_propagates(self):
        """A network failure during the library fetch propagates as ConnectionError."""
        with patch("game_recommender.gog.requests.get",
                   side_effect=req_lib.ConnectionError("timeout")):
            with pytest.raises(req_lib.ConnectionError):
                get_gog_library("tok")


# ── Corner cases ──────────────────────────────────────────────────────────────

class TestGetGogLibraryCornerCases:

    def test_total_pages_zero_still_makes_one_request_and_returns_empty(self):
        """
        The loop starts at page=1 and checks `page >= totalPages` at the end.
        When totalPages=0, 1 >= 0 is True immediately — one request is made,
        but no games are returned (empty products list).
        """
        resp = _products_resp([], total_pages=0)
        with patch("game_recommender.gog.requests.get", return_value=resp) as mock_get:
            games = get_gog_library("tok")

        assert games == []
        assert mock_get.call_count == 1  # One request was still issued (unavoidable)

    def test_title_null_coerced_to_empty_string_and_skipped(self):
        """title: null → (None or '').strip() == '' → skipped as blank title."""
        resp = _products_resp([
            {"id": 1, "title": None},
            {"id": 2, "title": "Real Game"},
        ])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert len(games) == 1
        assert games[0].name == "Real Game"

    def test_products_null_raises_type_error(self):
        """products: null → `for product in None:` raises TypeError.

        This documents an unhandled crash path — if GOG returns null for products,
        the function will raise rather than returning empty.
        """
        resp = _get_resp({"products": None, "totalPages": 1})
        with patch("game_recommender.gog.requests.get", return_value=resp):
            with pytest.raises(TypeError):
                get_gog_library("tok")

    def test_product_id_zero_stored_as_string_zero(self):
        """id: 0 is a valid (if unusual) app_id. str(0) == '0', which is non-empty."""
        resp = _products_resp([{"id": 0, "title": "Free Game"}])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert len(games) == 1
        assert games[0].app_id == "0"  # Not empty, not None — just "0"

    def test_missing_product_id_stored_as_empty_string(self):
        """When 'id' key is absent, app_id becomes str('') == ''."""
        resp = _products_resp([{"title": "No ID Game"}])
        with patch("game_recommender.gog.requests.get", return_value=resp):
            games = get_gog_library("tok")

        assert len(games) == 1
        assert games[0].app_id == ""  # str(product.get("id", "")) == str("") == ""

    def test_single_page_library_does_not_make_second_request(self):
        """With totalPages=1, page(1) >= 1 is True after the first request — stops there."""
        resp = _products_resp([{"id": 1, "title": "Solo Game"}], total_pages=1)
        with patch("game_recommender.gog.requests.get", return_value=resp) as mock_get:
            games = get_gog_library("tok")

        assert len(games) == 1
        assert mock_get.call_count == 1  # Exactly one request, not two
