"""
Tests for game_recommender/epic.py — Epic Games auth and library fetching.
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock, call

from game_recommender.epic import exchange_code, refresh_tokens, get_epic_library


# ── Helpers ───────────────────────────────────────────────────────────────────

def _post_resp(json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def _http_error_resp(code: int = 401) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.side_effect = req_lib.HTTPError(str(code))
    return resp


def _library_resp(records: list, next_cursor: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    body = {"records": records, "responseMetadata": {}}
    if next_cursor:
        body["responseMetadata"]["nextCursor"] = next_cursor
    resp.json.return_value = body
    return resp


# ── exchange_code ─────────────────────────────────────────────────────────────

class TestExchangeCode:

    def test_success_returns_token_dict(self):
        resp = _post_resp({
            "access_token": "tok_abc",
            "refresh_token": "ref_xyz",
            "account_id": "user123",
            "expires_in": 7200,
        })
        with patch("game_recommender.epic.requests.post", return_value=resp) as mock_post:
            result = exchange_code("my_auth_code")

        assert result["access_token"] == "tok_abc"
        assert result["refresh_token"] == "ref_xyz"
        assert result["account_id"] == "user123"
        # Verify the correct grant_type and code were sent
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["grant_type"] == "authorization_code"
        assert kwargs["data"]["code"] == "my_auth_code"

    def test_failure_invalid_code_raises_http_error(self):
        with patch("game_recommender.epic.requests.post", return_value=_http_error_resp(401)):
            with pytest.raises(req_lib.HTTPError):
                exchange_code("bad_code")

    def test_failure_network_error_propagates(self):
        with patch("game_recommender.epic.requests.post",
                   side_effect=req_lib.ConnectionError("unreachable")):
            with pytest.raises(req_lib.ConnectionError):
                exchange_code("code")


# ── refresh_tokens ────────────────────────────────────────────────────────────

class TestRefreshTokens:

    def test_success_returns_new_tokens(self):
        resp = _post_resp({
            "access_token": "new_tok",
            "refresh_token": "new_ref",
            "expires_in": 7200,
        })
        with patch("game_recommender.epic.requests.post", return_value=resp) as mock_post:
            result = refresh_tokens("old_refresh_token")

        assert result["access_token"] == "new_tok"
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["grant_type"] == "refresh_token"
        assert kwargs["data"]["refresh_token"] == "old_refresh_token"

    def test_failure_expired_refresh_token_raises(self):
        with patch("game_recommender.epic.requests.post", return_value=_http_error_resp(401)):
            with pytest.raises(req_lib.HTTPError):
                refresh_tokens("expired_token")


# ── get_epic_library ──────────────────────────────────────────────────────────

class TestGetEpicLibrary:

    def _mock_session(self, *responses):
        """Return a mock requests.Session whose .get() yields the given responses."""
        session = MagicMock()
        session.get.side_effect = list(responses)
        return session

    def test_success_returns_games(self):
        resp = _library_resp([
            {"appName": "Fortnite",      "catalogId": "fn01", "metadata": {"title": "Fortnite"}},
            {"appName": "RocketLeague",  "catalogId": "rl02", "metadata": {"title": "Rocket League"}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok_abc")

        assert len(games) == 2
        names = {g.name for g in games}
        assert "Fortnite" in names
        assert "Rocket League" in names
        assert all(g.platform == "epic" for g in games)
        assert all(g.playtime_minutes == 0 for g in games)

    def test_games_sorted_alphabetically(self):
        resp = _library_resp([
            {"appName": "z", "catalogId": "z", "metadata": {"title": "Zombie Game"}},
            {"appName": "a", "catalogId": "a", "metadata": {"title": "Alpha Game"}},
            {"appName": "m", "catalogId": "m", "metadata": {"title": "Middle Game"}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert [g.name for g in games] == ["Alpha Game", "Middle Game", "Zombie Game"]

    def test_pagination_fetches_all_pages(self):
        page1 = _library_resp(
            [{"appName": "A", "catalogId": "a", "metadata": {"title": "Alpha"}}],
            next_cursor="cursor_page2",
        )
        page2 = _library_resp(
            [{"appName": "B", "catalogId": "b", "metadata": {"title": "Beta"}}],
        )
        with patch("game_recommender.epic.requests.Session") as MockSession:
            session = self._mock_session(page1, page2)
            MockSession.return_value = session
            games = get_epic_library("tok")

        assert len(games) == 2
        assert session.get.call_count == 2
        # Second call must include the cursor
        second_call_params = session.get.call_args_list[1][1]["params"]
        assert second_call_params.get("cursor") == "cursor_page2"

    def test_deduplicates_by_title(self):
        resp = _library_resp([
            {"appName": "x", "catalogId": "a", "metadata": {"title": "My Game"}},
            {"appName": "y", "catalogId": "b", "metadata": {"title": "My Game"}},  # duplicate
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1
        assert games[0].name == "My Game"

    def test_skips_32_char_alphanumeric_internal_ids(self):
        internal_id = "a" * 32  # looks like an internal Epic catalog ID
        resp = _library_resp([
            {"appName": "internal", "catalogId": "x", "metadata": {"title": internal_id}},
            {"appName": "real",     "catalogId": "y", "metadata": {"title": "Real Game"}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1
        assert games[0].name == "Real Game"

    def test_falls_back_to_app_name_when_no_metadata_title(self):
        resp = _library_resp([
            {"appName": "FallbackName", "catalogId": "c1", "metadata": {}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert games[0].name == "FallbackName"

    def test_falls_back_to_catalog_id_when_no_app_name_or_title(self):
        resp = _library_resp([
            {"catalogId": "cat123", "metadata": {}},  # no appName
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert games[0].name == "cat123"

    def test_skips_blank_titles(self):
        resp = _library_resp([
            {"appName": "", "catalogId": "x", "metadata": {"title": "  "}},  # whitespace only
            {"appName": "Real", "catalogId": "y", "metadata": {"title": "Real Game"}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1

    def test_empty_library_returns_empty_list(self):
        resp = _library_resp([])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert games == []

    def test_http_error_propagates(self):
        session = MagicMock()
        session.get.return_value = _http_error_resp(401)
        with patch("game_recommender.epic.requests.Session", return_value=session):
            with pytest.raises(req_lib.HTTPError):
                get_epic_library("bad_tok")
