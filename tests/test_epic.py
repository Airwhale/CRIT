"""
Tests for game_recommender/epic.py — Epic Games auth and library fetching.

Covers:
  - exchange_code: token exchange via POST
  - refresh_tokens: token refresh via POST
  - get_epic_library: cursor-based pagination, title resolution, deduplication, filtering
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock, call

from game_recommender.epic import exchange_code, refresh_tokens, get_epic_library


# ── Helpers ───────────────────────────────────────────────────────────────────

def _post_resp(json_body: dict) -> MagicMock:
    """Mock a successful POST response (raise_for_status is a no-op)."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def _http_error_resp(code: int = 401) -> MagicMock:
    """Mock a failed response that raises HTTPError on raise_for_status."""
    resp = MagicMock()
    resp.raise_for_status.side_effect = req_lib.HTTPError(str(code))
    return resp


def _library_resp(records: list, next_cursor: str | None = None) -> MagicMock:
    """Build a mock library API response with optional pagination cursor.

    If next_cursor is provided, it's placed in responseMetadata so the
    pagination loop continues. If it's None or empty, pagination stops.
    """
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
        """Successful code exchange returns a dict with all expected token fields."""
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
        # Verify the correct grant_type and code were sent in the POST body
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["grant_type"] == "authorization_code"
        assert kwargs["data"]["code"] == "my_auth_code"
        # No redirect_uri when not provided (manual code-paste flow)
        assert "redirect_uri" not in kwargs["data"]

    def test_redirect_uri_included_when_provided(self):
        """When redirect_uri is given (OAuth redirect flow), it is sent in the POST body."""
        resp = _post_resp({"access_token": "tok", "refresh_token": "ref", "account_id": "u"})
        with patch("game_recommender.epic.requests.post", return_value=resp) as mock_post:
            exchange_code("code123", redirect_uri="http://localhost:8000/auth/epic/callback")

        _, kwargs = mock_post.call_args
        assert kwargs["data"]["redirect_uri"] == "http://localhost:8000/auth/epic/callback"
        assert kwargs["data"]["code"] == "code123"

    def test_failure_invalid_code_raises_http_error(self):
        """An expired or invalid auth code returns 401, which should raise HTTPError."""
        with patch("game_recommender.epic.requests.post", return_value=_http_error_resp(401)):
            with pytest.raises(req_lib.HTTPError):
                exchange_code("bad_code")

    def test_failure_network_error_propagates(self):
        """A network error during the token exchange should propagate unchanged."""
        with patch("game_recommender.epic.requests.post",
                   side_effect=req_lib.ConnectionError("unreachable")):
            with pytest.raises(req_lib.ConnectionError):
                exchange_code("code")


# ── refresh_tokens ────────────────────────────────────────────────────────────

class TestRefreshTokens:

    def test_success_returns_new_tokens(self):
        """Token refresh returns a new access token with the same dict shape."""
        resp = _post_resp({
            "access_token": "new_tok",
            "refresh_token": "new_ref",
            "expires_in": 7200,
        })
        with patch("game_recommender.epic.requests.post", return_value=resp) as mock_post:
            result = refresh_tokens("old_refresh_token")

        assert result["access_token"] == "new_tok"
        # Verify the correct grant_type and refresh_token were sent
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["grant_type"] == "refresh_token"
        assert kwargs["data"]["refresh_token"] == "old_refresh_token"

    def test_failure_expired_refresh_token_raises(self):
        """An expired refresh token returns 401 — user must re-authenticate."""
        with patch("game_recommender.epic.requests.post", return_value=_http_error_resp(401)):
            with pytest.raises(req_lib.HTTPError):
                refresh_tokens("expired_token")


# ── get_epic_library ──────────────────────────────────────────────────────────

class TestGetEpicLibrary:

    def _mock_session(self, *responses):
        """Return a mock requests.Session whose .get() yields the given responses in order."""
        session = MagicMock()
        session.get.side_effect = list(responses)
        return session

    def test_success_returns_games(self):
        """Basic case: two records → two Game objects with correct platform and 0 playtime."""
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
        # Epic exposes no playtime API — all games must show 0
        assert all(g.playtime_minutes == 0 for g in games)

    def test_games_sorted_alphabetically(self):
        """Output is sorted alphabetically regardless of API response order."""
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
        """When nextCursor is present, a second GET request is made with that cursor."""
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
        # The second call must include the cursor from the first response
        second_call_params = session.get.call_args_list[1][1]["params"]
        assert second_call_params.get("cursor") == "cursor_page2"

    def test_deduplicates_by_title(self):
        """When the same title appears twice in one page, only one Game is created."""
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
        """32-char all-alphanumeric strings are internal Epic IDs, not user-visible titles."""
        internal_id = "a" * 32  # exactly 32 alphanumeric chars
        resp = _library_resp([
            {"appName": "internal", "catalogId": "x", "metadata": {"title": internal_id}},
            {"appName": "real",     "catalogId": "y", "metadata": {"title": "Real Game"}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1
        assert games[0].name == "Real Game"

    def test_skips_record_with_no_metadata_title(self):
        """Records without metadata.title are internal entitlements and are skipped.

        appName values like "Arrowroot", "bobcat", "prokofiev" are Epic codenames —
        falling back to them would surface internal noise, not real game titles.
        """
        resp = _library_resp([
            {"appName": "Arrowroot", "catalogId": "c1", "metadata": {}},
            {"appName": "real",      "catalogId": "c2", "metadata": {"title": "Real Game"}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1
        assert games[0].name == "Real Game"

    def test_skips_record_with_null_metadata(self):
        """Records where metadata itself is null/absent are also skipped."""
        resp = _library_resp([
            {"appName": "bobcat", "catalogId": "c1"},  # no metadata key at all
            {"appName": "real",   "catalogId": "c2", "metadata": {"title": "Real Game"}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1
        assert games[0].name == "Real Game"

    def test_skips_blank_titles(self):
        """Records with blank or whitespace-only titles are silently skipped."""
        resp = _library_resp([
            {"appName": "", "catalogId": "x", "metadata": {"title": "  "}},  # whitespace only
            {"appName": "Real", "catalogId": "y", "metadata": {"title": "Real Game"}},
        ])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1  # Only "Real Game" survives

    def test_empty_library_returns_empty_list(self):
        """An account with no games returns an empty list without error."""
        resp = _library_resp([])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._mock_session(resp)
            games = get_epic_library("tok")

        assert games == []

    def test_http_error_propagates(self):
        """A 401 from the library endpoint propagates as HTTPError."""
        session = MagicMock()
        session.get.return_value = _http_error_resp(401)
        with patch("game_recommender.epic.requests.Session", return_value=session):
            with pytest.raises(req_lib.HTTPError):
                get_epic_library("bad_tok")


# ── Corner cases ──────────────────────────────────────────────────────────────

class TestGetEpicLibraryCornerCases:

    def _resp(self, records, next_cursor=None):
        """Build a mock library response (local version matching the class style)."""
        r = MagicMock()
        r.raise_for_status.return_value = None
        body = {"records": records, "responseMetadata": {}}
        if next_cursor is not None:
            body["responseMetadata"]["nextCursor"] = next_cursor
        r.json.return_value = body
        return r

    def _session(self, *responses):
        """Build a mock Session whose .get() yields the given responses."""
        s = MagicMock()
        s.get.side_effect = list(responses)
        return s

    def test_empty_string_cursor_stops_pagination(self):
        """nextCursor='' is falsy — the pagination loop exits after one page."""
        resp = self._resp(
            [{"appName": "A", "catalogId": "a", "metadata": {"title": "Alpha"}}],
            next_cursor="",   # empty string — falsy, same effect as None
        )
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1
        MockSession.return_value.get.assert_called_once()  # Only one HTTP call made

    def test_31_char_alphanumeric_title_not_filtered(self):
        """31-char alphanumeric strings are one character below the ID filter threshold."""
        title_31 = "a" * 31
        resp = self._resp([{"appName": "x", "catalogId": "y", "metadata": {"title": title_31}}])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._session(resp)
            games = get_epic_library("tok")

        assert len(games) == 1
        assert games[0].name == title_31  # 31-char string is kept

    def test_32_char_title_with_non_alphanumeric_char_not_filtered(self):
        """32 chars but contains a space — isalnum() returns False, not treated as an ID.

        Note: strip() removes the trailing space, leaving a 31-char title.
        """
        title = "a" * 31 + " "   # trailing space makes it 32 chars before strip
        resp = self._resp([{"appName": "x", "catalogId": "y", "metadata": {"title": title}}])
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._session(resp)
            games = get_epic_library("tok")

        # After stripping the trailing space: 31-char string, kept
        assert len(games) == 1
        assert games[0].name == "a" * 31

    def test_null_records_returns_empty_list(self):
        """records: null is treated as an empty page — the function returns [] gracefully.

        `data.get("records", [])` returns None when the key exists with a null value
        (the default only applies when the key is absent). Using `or []` handles this.
        """
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"records": None, "responseMetadata": {}}
        with patch("game_recommender.epic.requests.Session") as MockSession:
            s = MagicMock()
            s.get.return_value = resp
            MockSession.return_value = s
            games = get_epic_library("tok")
        assert games == []

    def test_records_accumulated_across_pages_before_dedup(self):
        """Deduplication happens after all pages are fetched — not per-page.

        A title appearing on page 1 and page 2 should result in exactly one game.
        """
        page1 = self._resp(
            [{"appName": "A", "catalogId": "a1", "metadata": {"title": "Same Game"}}],
            next_cursor="cur",
        )
        page2 = self._resp(
            [{"appName": "B", "catalogId": "b1", "metadata": {"title": "Same Game"}}],
        )
        with patch("game_recommender.epic.requests.Session") as MockSession:
            MockSession.return_value = self._session(page1, page2)
            games = get_epic_library("tok")

        assert len(games) == 1
        assert games[0].name == "Same Game"
