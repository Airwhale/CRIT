"""
Tests for web/app.py — FastAPI endpoints.

Uses FastAPI's TestClient (sync) for request/response testing.
All outbound network calls (Steam, Epic, GOG, Claude) are mocked.
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from web.app import app, _sessions
from tests.conftest import make_claude_client, FAKE_GAMES, parse_sse


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_sessions():
    """Isolate each test: start with a fresh session store."""
    _sessions.clear()
    yield
    _sessions.clear()


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def steam_session(client):
    """
    Set up a session with Steam connected and return (client, session_cookie).
    Uses monkeypatching to avoid hitting the real Steam API during connect.
    """
    import httpx

    async def fake_steam_validate(*args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"response": {"games": [{"appid": 1, "name": "x", "playtime_forever": 0}]}}
        return mock_resp

    with patch("web.app.httpx.AsyncClient") as MockClient:
        mock_http = MagicMock()
        mock_http.__aenter__ = fake_steam_validate  # makes the "async with" work
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"response": {"games": []}}
        mock_http.__aexit__ = MagicMock(return_value=None)
        mock_http.get = MagicMock(return_value=mock_resp)
        MockClient.return_value = mock_http

        # Inject steam session directly (simpler and reliable for testing)
        session_id = "test-steam-session"
        _sessions[session_id] = {
            "steam": {"api_key": "test_api_key", "user_id": "76561198000000001"}
        }
        yield client, session_id


def _cookies(session_id: str) -> dict:
    return {"session_id": session_id}


# ── GET / ─────────────────────────────────────────────────────────────────────

class TestIndexRoute:

    def test_returns_200_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Game Recommender" in resp.text

    def test_security_headers_present(self, client):
        resp = client.get("/")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"
        assert "content-security-policy" in resp.headers


# ── POST /api/auth/steam ───────────────────────────────────────────────────────

class TestSteamAuth:

    def _connect(self, client, api_key="goodkey", user_id="12345"):
        """Attempt a Steam connect call, mocking the validation request."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": {"games": []}}

        # httpx.AsyncClient is used as `async with httpx.AsyncClient() as c: await c.get(...)`
        # so we need a proper async context manager mock.
        mock_inner = AsyncMock()
        mock_inner.get = AsyncMock(return_value=mock_resp)

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_inner)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("web.app.httpx.AsyncClient", return_value=mock_cm):
            return client.post(
                "/api/auth/steam",
                json={"api_key": api_key, "user_id": user_id},
            )

    def test_success_returns_200_and_sets_cookie(self, client):
        resp = self._connect(client)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "session_id" in resp.cookies

    def test_failure_missing_api_key_returns_400(self, client):
        resp = client.post("/api/auth/steam", json={"user_id": "123"})
        assert resp.status_code == 400

    def test_failure_missing_user_id_returns_400(self, client):
        resp = client.post("/api/auth/steam", json={"api_key": "key"})
        assert resp.status_code == 400

    def test_failure_empty_body_returns_400(self, client):
        resp = client.post("/api/auth/steam", json={})
        assert resp.status_code == 400


# ── GET /api/status ───────────────────────────────────────────────────────────

class TestStatus:

    def test_no_session_returns_all_false(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["steam"] is False
        assert data["epic"] is False
        assert data["gog"] is False

    def test_with_steam_connected_returns_steam_true(self, client):
        session_id = "sess-1"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        resp = client.get("/api/status", cookies=_cookies(session_id))
        data = resp.json()
        assert data["steam"] is True
        assert data["epic"] is False
        assert data["gog"] is False

    def test_with_multiple_platforms(self, client):
        session_id = "sess-multi"
        _sessions[session_id] = {
            "steam": {"api_key": "k", "user_id": "u"},
            "gog": {"access_token": "t"},
        }
        resp = client.get("/api/status", cookies=_cookies(session_id))
        data = resp.json()
        assert data["steam"] is True
        assert data["epic"] is False
        assert data["gog"] is True


# ── DELETE /api/auth/{platform} ───────────────────────────────────────────────

class TestDisconnect:

    def test_disconnect_steam_removes_from_session(self, client):
        session_id = "sess-disc"
        _sessions[session_id] = {
            "steam": {"api_key": "k", "user_id": "u"},
            "gog": {"access_token": "t"},
        }
        resp = client.delete("/api/auth/steam", cookies=_cookies(session_id))
        assert resp.status_code == 200
        assert "steam" not in _sessions[session_id]
        assert "gog" in _sessions[session_id]  # other platform preserved

    def test_disconnect_unknown_platform_returns_404(self, client):
        resp = client.delete("/api/auth/playstation")
        assert resp.status_code == 404


# ── GET /api/library ──────────────────────────────────────────────────────────

class TestLibrary:

    def test_no_platforms_returns_400(self, client):
        resp = client.get("/api/library")
        assert resp.status_code == 400

    def test_success_returns_games(self, client):
        session_id = "sess-lib"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["games"]) == len(FAKE_GAMES)
        assert data["games"][0]["name"] == FAKE_GAMES[0]["name"]

    def test_platform_error_reported_in_errors_list(self, client):
        session_id = "sess-err"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", side_effect=RuntimeError("profile is private")):
            resp = client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        assert resp.status_code == 200
        data = resp.json()
        assert any(e["platform"] == "steam" for e in data["errors"])
        assert data["games"] == []

    def test_skip_ratings_skips_rawg(self, client):
        session_id = "sess-skip"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app._enrich_with_rawg") as mock_rawg:
            client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        mock_rawg.assert_not_called()

    def test_games_sorted_by_playtime_descending(self, client):
        session_id = "sess-sort"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        games = resp.json()["games"]
        playtimes = [g["playtime_minutes"] for g in games]
        assert playtimes == sorted(playtimes, reverse=True)


# ── GET /api/recommend (SSE) ──────────────────────────────────────────────────

class TestRecommend:
    """
    Tests for the streaming recommendation endpoint.
    The full SSE body is captured and parsed into event dicts.
    """

    @pytest.fixture(autouse=True)
    def set_anthropic_key(self, monkeypatch):
        """Ensure ANTHROPIC_API_KEY is set for every test in this class."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    def _recommend(self, client, session_id, claude_chunks=None, extra_games=None,
                   mode="library", **params):
        games = FAKE_GAMES if extra_games is None else extra_games
        chunks = claude_chunks or ["Great picks: ", "The Witcher 3 is amazing."]

        query = {"mode": mode, **params}
        query_str = "&".join(f"{k}={v}" for k, v in query.items())

        with patch("web.app._fetch_steam", return_value=games), \
             patch("web.app.anthropic.AsyncAnthropic",
                   return_value=make_claude_client(chunks)):
            resp = client.get(
                f"/api/recommend?{query_str}",
                cookies=_cookies(session_id),
            )
        return resp

    # ── Success paths ──────────────────────────────────────────────────────

    def test_library_mode_streams_status_text_done(self, client):
        session_id = "sess-rec-lib"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = self._recommend(client, session_id, mode="library")

        assert resp.status_code == 200
        events = parse_sse(resp.text)

        types = [e.get("type") for e in events]
        assert "status" in types

        text_events = [e for e in events if "text" in e]
        assert len(text_events) > 0
        full_text = "".join(e["text"] for e in text_events)
        assert "The Witcher 3" in full_text

        assert any(e.get("done") is True for e in events)

    def test_new_mode_filters_unplayed_games(self, client):
        session_id = "sess-rec-new"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = self._recommend(client, session_id, mode="new", max_new_minutes=60)

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_sales_mode_fetches_deals(self, client):
        session_id = "sess-rec-sales"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        from game_recommender.steam_sales import SaleGame
        fake_sales = [
            SaleGame(name="Cyberpunk 2077", app_id="1091500", discount_percent=60,
                     original_price_cents=5999, sale_price_cents=2399),
        ]

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app._fetch_sales", return_value=fake_sales), \
             patch("web.app.anthropic.AsyncAnthropic",
                   return_value=make_claude_client(["Cyberpunk 2077 is a great deal!"])):
            resp = client.get(
                "/api/recommend?mode=sales&min_discount=50&include_wishlist=false",
                cookies=_cookies(session_id),
            )

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_preferences_included_in_stream(self, client):
        session_id = "sess-rec-pref"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # Verify the endpoint accepts preferences without error
        resp = self._recommend(
            client, session_id,
            mode="library",
            preferences="something+relaxing",
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    # ── Validation failures (return 400 before streaming) ─────────────────

    def test_no_session_returns_400(self, client):
        resp = client.get("/api/recommend")
        assert resp.status_code == 400

    def test_missing_anthropic_key_returns_400(self, client, monkeypatch):
        session_id = "sess-no-key"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        resp = client.get("/api/recommend", cookies=_cookies(session_id))
        assert resp.status_code == 400
        assert "ANTHROPIC_API_KEY" in resp.json()["detail"]

    def test_invalid_mode_returns_400(self, client):
        session_id = "sess-badmode"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = client.get("/api/recommend?mode=invalid", cookies=_cookies(session_id))
        assert resp.status_code == 400

    def test_preferences_too_long_returns_400(self, client):
        session_id = "sess-longpref"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        long_pref = "x" * 501
        resp = client.get(
            f"/api/recommend?preferences={long_pref}",
            cookies=_cookies(session_id),
        )
        assert resp.status_code == 400

    # ── Failure paths (errors streamed as SSE events) ─────────────────────

    def test_empty_library_streams_error_event(self, client):
        session_id = "sess-empty"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=[]):
            resp = client.get("/api/recommend", cookies=_cookies(session_id))

        assert resp.status_code == 200   # SSE response opens fine
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)

    def test_claude_exception_streams_error_event(self, client):
        session_id = "sess-claude-err"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        bad_client = MagicMock()
        bad_stream = MagicMock()
        bad_stream.__aenter__ = MagicMock(side_effect=Exception("Claude API error"))
        bad_stream.__aexit__ = MagicMock(return_value=None)
        bad_client.messages.stream.return_value = bad_stream

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app.anthropic.AsyncAnthropic", return_value=bad_client):
            resp = client.get("/api/recommend", cookies=_cookies(session_id))

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)

    def test_new_mode_all_played_streams_error_event(self, client):
        """If every game is above the max_new_minutes threshold, expect an error event."""
        session_id = "sess-allplayed"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # All FAKE_GAMES have playtime > 0 except Disco Elysium; use max_new_minutes=0
        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = client.get(
                "/api/recommend?mode=new&max_new_minutes=0",
                cookies=_cookies(session_id),
            )

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)

    def test_sales_mode_no_deals_streams_error_event(self, client):
        session_id = "sess-nodeals"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app._fetch_sales", return_value=[]):
            resp = client.get(
                "/api/recommend?mode=sales",
                cookies=_cookies(session_id),
            )

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)

    def test_count_clamped_to_10(self, client):
        """count=99 should be silently clamped to 10 rather than erroring."""
        session_id = "sess-clamp"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = self._recommend(client, session_id, count=99)
        events = parse_sse(resp.text)
        # Should succeed (clamped, not rejected)
        assert any(e.get("done") is True for e in events)


# ── Corner cases ──────────────────────────────────────────────────────────────

class TestSteamAuthCornerCases:

    def _steam_mock(self, json_body, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_body
        mock_inner = AsyncMock()
        mock_inner.get = AsyncMock(return_value=mock_resp)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_inner)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        return mock_cm

    def test_whitespace_only_api_key_returns_400(self, client):
        """`"   ".strip()` == '' → treated as missing → 400."""
        resp = client.post("/api/auth/steam", json={"api_key": "   ", "user_id": "123"})
        assert resp.status_code == 400

    def test_whitespace_only_user_id_returns_400(self, client):
        resp = client.post("/api/auth/steam", json={"api_key": "key", "user_id": "\t\n"})
        assert resp.status_code == 400

    def test_steam_returns_empty_response_object_is_rejected(self, client):
        """Steam returns 200 with `response: {}` (private profile) → 400."""
        cm = self._steam_mock({"response": {}})
        with patch("web.app.httpx.AsyncClient", return_value=cm):
            resp = client.post("/api/auth/steam",
                               json={"api_key": "key", "user_id": "123"})
        assert resp.status_code == 400
        assert "public" in resp.json()["detail"].lower()

    def test_steam_api_error_status_returns_400(self, client):
        """Non-200 from Steam (e.g. 401) surfaces as a 400 with detail."""
        cm = self._steam_mock({}, status_code=401)
        with patch("web.app.httpx.AsyncClient", return_value=cm):
            resp = client.post("/api/auth/steam",
                               json={"api_key": "bad_key", "user_id": "123"})
        assert resp.status_code == 400
        assert "401" in resp.json()["detail"]

    def test_successful_connect_includes_game_count(self, client):
        """Response body should include game_count from the Steam library."""
        cm = self._steam_mock({"response": {"games": [
            {"appid": 1, "name": "X", "playtime_forever": 0},
            {"appid": 2, "name": "Y", "playtime_forever": 0},
        ]}})
        with patch("web.app.httpx.AsyncClient", return_value=cm):
            resp = client.post("/api/auth/steam",
                               json={"api_key": "key", "user_id": "123"})
        assert resp.status_code == 200
        assert resp.json()["game_count"] == 2


class TestLibraryCornerCases:

    def test_rawg_enrichment_failure_still_returns_games_without_ratings(
        self, client, monkeypatch
    ):
        """If _enrich_with_rawg raises, games come back without ratings and an error is listed."""
        monkeypatch.setenv("RAWG_API_KEY", "rawg-test-key")
        session_id = "sess-rawg-fail"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # Use games without pre-set ratings (simulating what _fetch_steam actually returns)
        bare_games = [
            {"name": "Witcher 3", "platform": "steam", "app_id": "1",
             "playtime_minutes": 100, "rawg_rating": None, "metacritic": None, "genres": []},
            {"name": "Hades", "platform": "steam", "app_id": "2",
             "playtime_minutes": 50, "rawg_rating": None, "metacritic": None, "genres": []},
        ]
        with patch("web.app._fetch_steam", return_value=bare_games), \
             patch("web.app._enrich_with_rawg", side_effect=RuntimeError("RAWG down")):
            resp = client.get("/api/library", cookies={"session_id": session_id})

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["games"]) == 2                              # games still returned
        assert any(e["platform"] == "rawg" for e in data["errors"])
        assert all(g["rawg_rating"] is None for g in data["games"])  # unenriched

    def test_partial_platform_failure_returns_surviving_platform_games(self, client):
        """If Steam fails but Epic succeeds, Epic's games appear and Steam is in errors."""
        session_id = "sess-partial-fail"
        _sessions[session_id] = {
            "steam": {"api_key": "k", "user_id": "u"},
            "epic":  {"access_token": "t"},
        }
        epic_games = [
            {"name": "Fortnite", "platform": "epic", "app_id": "fn",
             "playtime_minutes": 0, "rawg_rating": None, "metacritic": None, "genres": []},
        ]
        with patch("web.app._fetch_steam", side_effect=RuntimeError("profile private")), \
             patch("web.app._fetch_epic", return_value=epic_games):
            resp = client.get("/api/library?skip_ratings=true",
                              cookies={"session_id": session_id})

        assert resp.status_code == 200
        data = resp.json()
        assert data["games"][0]["name"] == "Fortnite"
        assert any(e["platform"] == "steam" for e in data["errors"])

    def test_top_75_boundary_76th_game_not_enriched(self, client, monkeypatch):
        """Only the top 75 games by playtime are passed to RAWG; the 76th is skipped."""
        monkeypatch.setenv("RAWG_API_KEY", "rawg-key")
        session_id = "sess-76"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        games_76 = [
            {"name": f"Game {i:02d}", "platform": "steam", "app_id": str(i),
             "playtime_minutes": 100 - i,
             "rawg_rating": None, "metacritic": None, "genres": []}
            for i in range(76)
        ]
        # Enrichment adds a rating to each of the 75 it receives
        def fake_enrich(games, api_key):
            return [{**g, "rawg_rating": 4.5} for g in games]

        with patch("web.app._fetch_steam", return_value=games_76), \
             patch("web.app._enrich_with_rawg", side_effect=fake_enrich) as mock_enrich:
            resp = client.get("/api/library", cookies={"session_id": session_id})

        called_with = mock_enrich.call_args[0][0]
        assert len(called_with) == 75                            # exactly 75 sent to RAWG

        data = resp.json()
        # Game 75 has the lowest playtime (100-75=25 mins) — not enriched
        last_game = next(g for g in data["games"] if g["name"] == "Game 75")
        assert last_game["rawg_rating"] is None

    def test_exactly_75_games_all_enriched(self, client, monkeypatch):
        """With exactly 75 games all_games[:75] == all_games — every one is enriched."""
        monkeypatch.setenv("RAWG_API_KEY", "rawg-key")
        session_id = "sess-75-exact"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        games_75 = [
            {"name": f"Game {i:02d}", "platform": "steam", "app_id": str(i),
             "playtime_minutes": 75 - i,
             "rawg_rating": None, "metacritic": None, "genres": []}
            for i in range(75)
        ]

        def fake_enrich(games, api_key):
            return [{**g, "rawg_rating": 4.0} for g in games]

        with patch("web.app._fetch_steam", return_value=games_75), \
             patch("web.app._enrich_with_rawg", side_effect=fake_enrich) as mock_enrich:
            resp = client.get("/api/library", cookies={"session_id": session_id})

        called_with = mock_enrich.call_args[0][0]
        assert len(called_with) == 75
        assert all(g["rawg_rating"] == 4.0 for g in resp.json()["games"])


class TestRecommendCornerCases:

    @pytest.fixture(autouse=True)
    def set_anthropic_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    def _recommend(self, client, session_id, chunks=None, games=None, mode="library", **params):
        games = FAKE_GAMES if games is None else games
        chunks = chunks or ["Here are my picks."]
        qs = "&".join(f"{k}={v}" for k, v in {"mode": mode, **params}.items())
        with patch("web.app._fetch_steam", return_value=games), \
             patch("web.app.anthropic.AsyncAnthropic",
                   return_value=make_claude_client(chunks)):
            return client.get(f"/api/recommend?{qs}",
                              cookies={"session_id": session_id})

    def test_count_zero_clamped_to_one(self, client):
        """max(1, min(0, 10)) == 1 — count=0 is valid and silently clamped."""
        session_id = "sess-c0"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        resp = self._recommend(client, session_id, count=0)
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_count_one_minimum_valid(self, client):
        session_id = "sess-c1"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        resp = self._recommend(client, session_id, count=1)
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_preferences_exactly_500_chars_accepted(self, client):
        """500 chars is the limit: len > 500 is False at exactly 500."""
        session_id = "sess-p500"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        resp = self._recommend(client, session_id, preferences="x" * 500)
        assert resp.status_code == 200
        assert any(e.get("done") is True for e in parse_sse(resp.text))

    def test_claude_text_with_quotes_and_newlines_survives_sse_roundtrip(self, client):
        """
        json.dumps inside the SSE loop must escape special characters.
        Parsed events should reproduce the original text faithfully.
        """
        session_id = "sess-special"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        tricky = 'He said "amazing game"!\nBuy it.'
        resp = self._recommend(client, session_id, chunks=[tricky])
        text_events = [e for e in parse_sse(resp.text) if "text" in e]
        full_text = "".join(e["text"] for e in text_events)
        assert '"amazing game"' in full_text
        assert "Buy it." in full_text

    def test_unplayed_game_at_exact_max_new_minutes_is_included(self, client):
        """
        The filter is `playtime <= max_new_minutes`, so a game with playtime==threshold
        is counted as unplayed and should appear in the recommendations.
        """
        session_id = "sess-exact-threshold"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        games = [{"name": "Threshold Game", "platform": "steam", "app_id": "1",
                  "playtime_minutes": 60,
                  "rawg_rating": None, "metacritic": None, "genres": []}]
        resp = self._recommend(client, session_id, games=games,
                               mode="new", max_new_minutes=60)
        events = parse_sse(resp.text)
        # Should succeed — if 60-min game was excluded, unplayed list would be empty
        # and we'd get an error event instead of a done event
        assert any(e.get("done") is True for e in events), \
            "A 60-minute game should be unplayed at max_new_minutes=60"

    def test_game_with_61_minutes_is_not_unplayed_at_60_threshold(self, client):
        """61 > 60: game is played, not unplayed. All-played library → error event."""
        session_id = "sess-61"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        games = [{"name": "Barely Played", "platform": "steam", "app_id": "1",
                  "playtime_minutes": 61,
                  "rawg_rating": None, "metacritic": None, "genres": []}]
        resp = self._recommend(client, session_id, games=games,
                               mode="new", max_new_minutes=60)
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)

    def test_library_mode_with_all_unplayed_games_still_recommends(self, client):
        """Library mode has no playtime requirement — all-unplayed library is valid."""
        session_id = "sess-unplayed-lib"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        games = [
            {"name": f"Unplayed {i}", "platform": "steam", "app_id": str(i),
             "playtime_minutes": 0, "rawg_rating": None, "metacritic": None, "genres": []}
            for i in range(5)
        ]
        resp = self._recommend(client, session_id, games=games, mode="library")
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_session_not_cleared_between_recommend_calls(self, client):
        """Session state persists across multiple calls in the same test."""
        session_id = "sess-persist"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        for _ in range(2):
            resp = self._recommend(client, session_id)
            assert any(e.get("done") is True for e in parse_sse(resp.text))
