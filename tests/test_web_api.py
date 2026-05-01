"""
Tests for web/app.py — FastAPI endpoints.

Uses FastAPI's TestClient (sync) for request/response testing.
All outbound network calls (Steam, Epic, GOG, Claude) are mocked.

Organisation:
  - TestIndexRoute          – GET / HTML response and security headers
  - TestSteamAuth           – POST /api/auth/steam (happy path + 400s)
  - TestStatus              – GET /api/status (per-session platform flags)
  - TestDisconnect          – DELETE /api/auth/{platform}
  - TestLibrary             – GET /api/library (fetch, sort, skip_ratings)
  - TestLibraryStream       – GET /api/library/stream (SSE skeleton loading)
  - TestRecommend           – GET /api/recommend SSE stream (3 modes + error paths)
  - TestSteamAuthCornerCases  – edge inputs and Steam API error mappings
  - TestLibraryCornerCases    – RAWG failure, partial platform failure, top-75 boundary
  - TestRecommendCornerCases  – count clamping, threshold boundary, SSE encoding

The `clear_sessions` autouse fixture wipes the module-level `_sessions` dict
before and after every test so that session state from one test cannot affect
another.  The `client` fixture wraps the FastAPI TestClient with
`raise_server_exceptions=False` so that server-side 5xx errors appear as
responses rather than propagating as Python exceptions in test code.
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from web.app import app, _sessions, _session_last_seen
from tests.conftest import make_claude_client, make_openrouter_client, FAKE_GAMES, parse_sse


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_sessions():
    """Isolate each test: start with a fresh session store.

    autouse=True means this runs for every test in this file automatically.
    Clearing before (and after via yield) ensures no cross-test contamination
    of in-memory session state.
    """
    _sessions.clear()
    _session_last_seen.clear()
    yield
    _sessions.clear()
    _session_last_seen.clear()


@pytest.fixture
def client():
    """Return a TestClient wrapping the FastAPI app.

    raise_server_exceptions=False causes 5xx errors to appear as HTTP responses
    (status 500) rather than re-raising the exception in test code.  This lets
    tests assert on the response status without needing try/except.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def steam_session(client):
    """Set up a session with Steam connected and return (client, session_cookie).

    Injects a pre-built session dict directly into `_sessions` rather than
    going through the /api/auth/steam endpoint, which avoids the async httpx
    mock setup overhead for tests that just need a logged-in state.
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


def _set_session_cookie(client, session_id: str) -> None:
    """Set the session cookie on the TestClient for subsequent requests."""
    client.cookies.set("session_id", session_id)


def _cookies(session_id: str) -> dict:
    """Backward-compatible cookie helper for tests not yet migrated."""
    return {"session_id": session_id}


# ── GET / ─────────────────────────────────────────────────────────────────────

class TestIndexRoute:
    """Tests for the root HTML page served at GET /.

    The index route serves a static HTML file that contains the entire
    single-page application.  These tests verify the response shape and
    that the security middleware headers are applied on every response.
    """

    def test_returns_200_html(self, client):
        """Root path returns HTTP 200 with an HTML content-type."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        # Basic sanity check that the right page is being served
        assert "CRIT" in resp.text

    def test_security_headers_present(self, client):
        """Security middleware adds three headers to every response.

        - X-Content-Type-Options: nosniff  — prevents MIME-sniffing attacks
        - X-Frame-Options: DENY            — blocks iframe embedding (clickjacking)
        - Content-Security-Policy          — restricts resource origins
        """
        resp = client.get("/")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"
        # We only check presence, not the exact CSP value, which can evolve
        assert "content-security-policy" in resp.headers


# ── POST /api/auth/steam ───────────────────────────────────────────────────────

class TestSteamAuth:
    """Tests for POST /api/auth/steam — Steam credentials validation and session setup.

    The endpoint validates credentials by making a real Steam API request (mocked
    here).  A successful response stores the credentials in the session dict and
    sets an HTTP-only session cookie.

    The `_connect` helper encapsulates the nested async context manager mock needed
    for `async with httpx.AsyncClient() as c: resp = await c.get(...)`.
    """

    def _connect(self, client, api_key="goodkey", user_id="12345"):
        """Attempt a Steam connect call, mocking the validation request.

        The web app uses `async with httpx.AsyncClient() as c:` — so the mock
        needs to be both an async context manager (for `__aenter__`/`__aexit__`)
        and expose a `.get()` async method on the inner object.
        """
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
        """Valid credentials yield 200 with ok=True and a session_id cookie."""
        resp = self._connect(client)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # TestClient surfaces cookies from Set-Cookie headers in resp.cookies
        assert "session_id" in resp.cookies

    def test_failure_missing_user_id_returns_400(self, client):
        """Request body without user_id is rejected before any Steam call."""
        resp = client.post("/api/auth/steam", json={"api_key": "key"})
        assert resp.status_code == 400

    def test_failure_empty_body_returns_400(self, client):
        """An empty JSON body (both fields missing) is rejected immediately."""
        resp = client.post("/api/auth/steam", json={})
        assert resp.status_code == 400


# ── GET /api/status ───────────────────────────────────────────────────────────

class TestStatus:
    """Tests for GET /api/status — per-session platform connection flags.

    Returns a JSON dict with boolean flags for each supported platform.
    No session cookie → all platforms are False (not connected).
    """

    def test_no_session_returns_all_false(self, client):
        """Without a session cookie, no platforms can be connected."""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["steam"] is False
        assert data["epic"] is False
        assert data["gog"] is False

    def test_with_steam_connected_returns_steam_true(self, client):
        """When only Steam credentials are in the session, only steam is True."""
        session_id = "sess-1"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        resp = client.get("/api/status", cookies=_cookies(session_id))
        data = resp.json()
        assert data["steam"] is True
        assert data["epic"] is False
        assert data["gog"] is False

    def test_with_multiple_platforms(self, client):
        """Steam + GOG in the session → steam=True, gog=True, epic=False."""
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
    """Tests for DELETE /api/auth/{platform} — removing one platform from a session.

    Disconnect removes only the specified platform key from the session dict;
    other platform connections are preserved.
    """

    def test_disconnect_steam_removes_from_session(self, client):
        """After disconnecting Steam, the steam key is gone but GOG remains."""
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
        """An unsupported platform name (e.g. 'playstation') returns 404."""
        resp = client.delete("/api/auth/playstation")
        assert resp.status_code == 404


# ── GET /api/library ──────────────────────────────────────────────────────────

class TestLibrary:
    """Tests for GET /api/library — combined multi-platform game library fetch.

    Without at least one platform session, returns 400.  With at least one
    platform connected, fetches each platform's games concurrently (via
    asyncio.gather), then optionally enriches the top 75 with RAWG ratings.
    """

    def test_no_platforms_returns_400(self, client):
        """No session / no platform connected → cannot build a library → 400."""
        resp = client.get("/api/library")
        assert resp.status_code == 400

    def test_success_returns_games(self, client):
        """With Steam connected, fetching returns the expected games list."""
        session_id = "sess-lib"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # Patch the internal _fetch_steam helper so no real Steam call is made
        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["games"]) == len(FAKE_GAMES)
        assert data["games"][0]["name"] == FAKE_GAMES[0]["name"]

    def test_platform_error_reported_in_errors_list(self, client):
        """A platform fetch failure is non-fatal: games=[] and error appears in errors list."""
        session_id = "sess-err"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", side_effect=RuntimeError("profile is private")):
            resp = client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        # 200 because the endpoint itself succeeded — individual platform errors are reported
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["platform"] == "steam" for e in data["errors"])
        assert data["games"] == []

    def test_skip_ratings_skips_rawg(self, client):
        """skip_ratings=true causes the RAWG enrichment step to be skipped entirely."""
        session_id = "sess-skip"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app._enrich_with_rawg") as mock_rawg:
            client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        # _enrich_with_rawg must never be called when skip_ratings is set
        mock_rawg.assert_not_called()

    def test_games_sorted_by_playtime_descending(self, client):
        """The library response is always sorted most-played first."""
        session_id = "sess-sort"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        games = resp.json()["games"]
        playtimes = [g["playtime_minutes"] for g in games]
        # Verify descending order
        assert playtimes == sorted(playtimes, reverse=True)

    def test_library_response_includes_all_expected_fields(self, client):
        """Every game dict in the library response must include all expected fields.

        This acts as a schema regression test: adding or removing a field from
        _game_to_dict() should be caught here so documentation and tests stay in sync.
        """
        session_id = "sess-fields"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = client.get("/api/library?skip_ratings=true", cookies=_cookies(session_id))

        assert resp.status_code == 200
        expected_fields = {
            "name", "platform", "app_id", "playtime_minutes", "last_played",
            "release_year", "rawg_rating", "metacritic", "genres", "tags", "gog_rating",
        }
        for game in resp.json()["games"]:
            missing = expected_fields - game.keys()
            assert not missing, f"Game '{game.get('name')}' is missing fields: {missing}"


# ── GET /api/recommend (SSE) ──────────────────────────────────────────────────

class TestRecommend:
    """Tests for the streaming recommendation endpoint GET /api/recommend.

    The endpoint streams Server-Sent Events in three different modes:
      - library: recommend from your full existing library
      - new:     recommend unplayed games (below max_new_minutes playtime)
      - sales:   recommend from current Steam sales / wishlist deals

    The full SSE body is captured by TestClient and parsed into event dicts
    by `parse_sse()`.  A successful stream always ends with a `{"done": true}`
    event; errors mid-stream produce an `{"error": "..."}` event instead.

    The `_recommend` helper bundles the common patching + request logic so
    individual tests focus only on what they're verifying.
    """

    @pytest.fixture(autouse=True)
    def set_anthropic_key(self, monkeypatch):
        """Ensure ANTHROPIC_API_KEY is set for every test in this class.

        Without this the endpoint returns 400 before any streaming begins,
        which would cause all success-path tests to fail.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    def _recommend(self, client, session_id, claude_chunks=None, extra_games=None,
                   mode="library", **params):
        """Make a GET /api/recommend request with standard mocks in place.

        claude_chunks: list of text strings yielded by the fake Claude stream
        extra_games:   override FAKE_GAMES for games returned by _fetch_steam
        mode:          recommendation mode (library / new / sales / discover)
        **params:      extra query string parameters (e.g. count=5, preferences=...)
        """
        games = FAKE_GAMES if extra_games is None else extra_games
        chunks = claude_chunks or ["Great picks: ", "The Witcher 3 is amazing."]

        query = {"mode": mode, **params}
        query_str = "&".join(f"{k}={v}" for k, v in query.items())

        with patch("web.app._fetch_steam", return_value=games), \
             patch("web.app.anthropic.AsyncAnthropic",
                   return_value=make_claude_client(chunks)):
            _set_session_cookie(client, session_id)
            resp = client.get(f"/api/recommend?{query_str}")
        return resp

    # ── Success paths ──────────────────────────────────────────────────────

    def test_library_mode_streams_status_text_done(self, client):
        """Library mode: full SSE stream contains status, text, and done events."""
        session_id = "sess-rec-lib"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = self._recommend(client, session_id, mode="library")

        assert resp.status_code == 200
        events = parse_sse(resp.text)

        # At least one status event should be emitted (e.g. "Fetching library…")
        types = [e.get("type") for e in events]
        assert "status" in types

        # At least one text chunk should contain Claude's output
        text_events = [e for e in events if "text" in e]
        assert len(text_events) > 0
        full_text = "".join(e["text"] for e in text_events)
        assert "The Witcher 3" in full_text

        # Stream must end with a done sentinel event
        assert any(e.get("done") is True for e in events)

    def test_new_mode_filters_unplayed_games(self, client):
        """New mode: completes successfully with default max_new_minutes=60."""
        session_id = "sess-rec-new"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # FAKE_GAMES includes Disco Elysium with 0 playtime, which qualifies
        resp = self._recommend(client, session_id, mode="new", max_new_minutes=60)

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_discover_mode_streams_completion(self, client):
        """Discover mode: streams status, text, and done — no external data needed."""
        session_id = "sess-rec-discover"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = self._recommend(client, session_id, mode="discover",
                               claude_chunks=["Try Hollow Knight, Celeste, and Hades!"])

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)
        full_text = "".join(e["text"] for e in events if "text" in e)
        assert "Hollow Knight" in full_text

    def test_sales_mode_fetches_deals(self, client):
        """Sales mode: fetches sale data and streams a completion."""
        session_id = "sess-rec-sales"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        from game_recommender.steam_sales import SaleGame
        fake_sales = [
            SaleGame(name="Cyberpunk 2077", app_id="1091500", discount_percent=60,
                     original_price_cents=5999, sale_price_cents=2399),
        ]

        # Sales mode patches both _fetch_steam and _fetch_sales.
        # _fetch_sales returns (games, warnings) — mock must match that signature.
        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app._fetch_sales", return_value=(fake_sales, [])), \
             patch("web.app.anthropic.AsyncAnthropic",
                   return_value=make_claude_client(["Cyberpunk 2077 is a great deal!"])):
            _set_session_cookie(client, session_id)
            resp = client.get("/api/recommend?mode=sales&min_discount=50&include_wishlist=false")

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_preferences_included_in_stream(self, client):
        """An optional preferences string is accepted and the stream completes normally."""
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
        """No session cookie (no platforms connected) → 400 before any streaming."""
        resp = client.get("/api/recommend")
        assert resp.status_code == 400

    def test_missing_anthropic_key_returns_400(self, client, monkeypatch):
        """Without ANTHROPIC_API_KEY in env, 400 is returned with a descriptive message."""
        session_id = "sess-no-key"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        resp = client.get("/api/recommend", cookies=_cookies(session_id))
        assert resp.status_code == 400
        # Detail message should name the missing env var so users know what to set
        assert "ANTHROPIC_API_KEY" in resp.json()["detail"]

    def test_invalid_mode_returns_400(self, client):
        """An unrecognised mode value is rejected synchronously with 400."""
        session_id = "sess-badmode"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = client.get("/api/recommend?mode=invalid", cookies=_cookies(session_id))
        assert resp.status_code == 400

    def test_preferences_too_long_returns_400(self, client):
        """preferences > 500 characters is rejected with 400 before streaming."""
        session_id = "sess-longpref"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        long_pref = "x" * 501
        _set_session_cookie(client, session_id)
        resp = client.get(
            f"/api/recommend?preferences={long_pref}",
        )
        assert resp.status_code == 400

    # ── Failure paths (errors streamed as SSE events) ─────────────────────

    def test_empty_library_streams_error_event(self, client):
        """An empty games list cannot produce recommendations — an error event is streamed.

        Note: the HTTP status is still 200 because SSE streaming has already begun.
        Errors encountered inside the stream generator are communicated via events,
        not via the HTTP status code.
        """
        session_id = "sess-empty"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=[]):
            resp = client.get("/api/recommend", cookies=_cookies(session_id))

        assert resp.status_code == 200   # SSE response opens fine
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)



    def test_claude_exception_streams_error_event(self, client):
        """An exception from the Claude stream is caught and emitted as an error event."""
        session_id = "sess-claude-err"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # Build a mock Claude client whose stream context manager raises on enter
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
        """If every game is above the max_new_minutes threshold, expect an error event.

        Uses max_new_minutes=0 so every game with any playtime is considered
        'played'.  FAKE_GAMES has games with playtime > 0, so the unplayed
        list would be empty — triggering the error path.
        """
        session_id = "sess-allplayed"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # All FAKE_GAMES have playtime > 0 except Disco Elysium; use max_new_minutes=0
        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            _set_session_cookie(client, session_id)
            resp = client.get("/api/recommend?mode=new&max_new_minutes=0")

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)

    def test_sales_mode_no_deals_streams_error_event(self, client):
        """An empty sales list cannot produce sale recommendations — error event emitted."""
        session_id = "sess-nodeals"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app._fetch_sales", return_value=[]):
            _set_session_cookie(client, session_id)
            resp = client.get("/api/recommend?mode=sales")

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)

    def test_count_clamped_to_50(self, client):
        """count=99 should be silently clamped to 50 rather than erroring.

        The endpoint uses max(1, min(count, 50)) so any value outside [1,50]
        is brought into range without producing a 400 response.
        """
        session_id = "sess-clamp"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = self._recommend(client, session_id, count=99)
        events = parse_sse(resp.text)
        # Should succeed (clamped, not rejected)
        assert any(e.get("done") is True for e in events)


class TestRecommendOpenRouter:
    """Tests for the OpenRouter provider path of GET /api/recommend.

    OpenRouter is offered as an alternative to the direct Anthropic SDK. It
    speaks the OpenAI-compatible chat-completions API, so the streaming shape
    is different (`chunk.choices[0].delta.content`). Extended thinking and
    Anthropic prompt caching are NOT available on this path; the UI disables
    those controls when OpenRouter is selected and the server ignores them.
    """

    def _recommend_or(self, client, session_id, claude_chunks=None, mode="library",
                      provider="openrouter", **params):
        """Make a GET /api/recommend request through the OpenRouter mock.

        Patches both `web.app._fetch_steam` (so the library fetch is
        deterministic) and `web.app.openai.AsyncOpenAI` (so the streaming
        chunks come from the fake instead of the real API).
        """
        chunks = claude_chunks or ["Top pick: ", "The Witcher 3 — start there."]

        query = {"mode": mode, "provider": provider, **params}
        query_str = "&".join(f"{k}={v}" for k, v in query.items())

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app.openai.AsyncOpenAI",
                   return_value=make_openrouter_client(chunks)):
            _set_session_cookie(client, session_id)
            resp = client.get(f"/api/recommend?{query_str}")
        return resp

    # ── Success paths ──────────────────────────────────────────────────────

    def test_openrouter_streams_text_and_done(self, client, monkeypatch):
        """Provider=openrouter with key set returns a normal SSE text/done stream."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        session_id = "sess-or-ok"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = self._recommend_or(client, session_id)

        assert resp.status_code == 200
        events = parse_sse(resp.text)

        text_events = [e for e in events if "text" in e]
        assert len(text_events) > 0
        full_text = "".join(e["text"] for e in text_events)
        assert "The Witcher 3" in full_text

        assert any(e.get("done") is True for e in events)

    def test_openrouter_ignores_use_thinking(self, client, monkeypatch):
        """use_thinking=true must not break the OpenRouter path (it's just ignored).

        The UI disables the thinking checkbox when OpenRouter is selected, but
        the server should still accept the parameter and produce a normal
        stream rather than 500ing.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        session_id = "sess-or-thinking"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        resp = self._recommend_or(client, session_id, use_thinking="true")

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_openrouter_passes_model_through(self, client, monkeypatch):
        """The model query param flows through to the OpenAI client unchanged."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        session_id = "sess-or-model"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        captured_kwargs = {}

        async def _capture_create(**kwargs):
            captured_kwargs.update(kwargs)
            from tests.conftest import _AsyncChunkIter
            return _AsyncChunkIter(["ok"])

        fake_client = MagicMock()
        fake_client.chat.completions.create = _capture_create

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app.openai.AsyncOpenAI", return_value=fake_client):
            _set_session_cookie(client, session_id)
            resp = client.get(
                "/api/recommend?provider=openrouter&model=openai/gpt-4o&mode=library"
            )

        assert resp.status_code == 200
        assert captured_kwargs.get("model") == "openai/gpt-4o"
        # Sanity-check the OpenAI message shape: a single user message with a
        # plain string content (no Anthropic-style content blocks or cache_control).
        msgs = captured_kwargs.get("messages")
        assert isinstance(msgs, list) and len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert isinstance(msgs[0]["content"], str)
        assert "stream" in captured_kwargs and captured_kwargs["stream"] is True

    # ── Failure paths ──────────────────────────────────────────────────────

    def test_missing_openrouter_key_returns_400(self, client, monkeypatch):
        """Without OPENROUTER_API_KEY set, provider=openrouter returns 400 pre-stream."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        session_id = "sess-or-nokey"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        _set_session_cookie(client, session_id)
        resp = client.get("/api/recommend?provider=openrouter&mode=library")

        assert resp.status_code == 400
        assert "OPENROUTER_API_KEY" in resp.text

    def test_unknown_provider_returns_400(self, client, monkeypatch):
        """An unrecognised provider value is rejected pre-stream."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        session_id = "sess-bad-provider"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        _set_session_cookie(client, session_id)
        resp = client.get("/api/recommend?provider=foo&mode=library")

        assert resp.status_code == 400
        assert "provider" in resp.text.lower()

    def test_anthropic_provider_path_unaffected(self, client, monkeypatch):
        """Explicit provider=anthropic still uses the Anthropic SDK and works.

        Regression check: the OpenRouter addition must not have broken the
        original code path. The Anthropic-direct path streams via
        client.messages.stream(), which the old fixture covers.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        session_id = "sess-anth-explicit"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app.anthropic.AsyncAnthropic",
                   return_value=make_claude_client(["Hello ", "world"])):
            _set_session_cookie(client, session_id)
            resp = client.get("/api/recommend?provider=anthropic&mode=library")

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        full_text = "".join(e["text"] for e in events if "text" in e)
        assert "Hello" in full_text
        assert any(e.get("done") is True for e in events)


class TestDealsEndpoint:
    """Tests for GET /api/deals standalone deals-table endpoint."""

    def test_requires_connected_session(self, client):
        resp = client.get('/api/deals')
        assert resp.status_code == 400

    def test_returns_deals_and_warnings(self, client, monkeypatch):
        from game_recommender.steam_sales import SaleGame

        session_id = 'sess-deals-json'
        _sessions[session_id] = {'steam': {'api_key': 'k', 'user_id': 'u'}}
        monkeypatch.setenv('ITAD_API_KEY', 'itad-test-key')

        sales = [
            SaleGame(name='Cyberpunk 2077', app_id='1091500', discount_percent=60,
                     original_price_cents=5999, sale_price_cents=2399),
        ]
        with patch('web.app._fetch_steam', return_value=FAKE_GAMES), \
             patch('web.app._fetch_sales', return_value=(sales, ['wishlist source unavailable'])), \
             patch('web.app._enrich_deals_with_itad', return_value={'Cyberpunk 2077': {'verdict': 'near_low', 'hist_low': 19.99}}):
            _set_session_cookie(client, session_id)
            resp = client.get('/api/deals?min_discount=40&deal_sources=steam_featured')

        assert resp.status_code == 200
        data = resp.json()
        assert len(data['deals']) == 1
        assert data['warnings'] == ['wishlist source unavailable']
        assert 'Cyberpunk 2077' in data['itad_data']

    def test_without_itad_key_skips_itad_enrichment(self, client, monkeypatch):
        session_id = 'sess-deals-no-itad'
        _sessions[session_id] = {'steam': {'api_key': 'k', 'user_id': 'u'}}
        monkeypatch.delenv('ITAD_API_KEY', raising=False)

        with patch('web.app._fetch_steam', return_value=FAKE_GAMES), \
             patch('web.app._fetch_sales', return_value=([], [])), \
             patch('web.app._enrich_deals_with_itad') as mock_itad:
            _set_session_cookie(client, session_id)
            resp = client.get('/api/deals')

        assert resp.status_code == 200
        assert resp.json()['itad_data'] == {}
        mock_itad.assert_not_called()



# ── Corner cases ──────────────────────────────────────────────────────────────

class TestSteamAuthCornerCases:
    """Edge-input tests for POST /api/auth/steam.

    Covers whitespace-only inputs, Steam's private-profile response shape,
    and non-200 responses from the Steam API.
    """

    def _steam_mock(self, json_body, status_code=200):
        """Build a fully-nested async context manager mock for httpx.AsyncClient.

        This reproduces `async with httpx.AsyncClient() as c: resp = await c.get(...)`.
        The inner AsyncMock's `.get()` returns the given json_body/status_code.
        """
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_body
        mock_inner = AsyncMock()
        mock_inner.get = AsyncMock(return_value=mock_resp)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_inner)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        return mock_cm

    def test_whitespace_only_user_id_returns_400(self, client):
        """Tab/newline-only user_id is also treated as missing after strip()."""
        resp = client.post("/api/auth/steam", json={"api_key": "key", "user_id": "\t\n"})
        assert resp.status_code == 400

    def test_steam_returns_empty_response_object_is_rejected(self, client):
        """Steam returns 200 with `response: {}` for private profiles → our 400.

        This is the documented Steam API behaviour for private profiles: HTTP 200
        but with an empty `response` object instead of a `games` list.  The
        endpoint must detect this and return 400 with a user-readable message.
        """
        cm = self._steam_mock({"response": {}})
        with patch("web.app.httpx.AsyncClient", return_value=cm):
            resp = client.post("/api/auth/steam",
                               json={"api_key": "key", "user_id": "123"})
        assert resp.status_code == 400
        # Error detail should explain that the profile needs to be public
        assert "public" in resp.json()["detail"].lower()

    def test_steam_api_error_status_returns_400(self, client):
        """Non-200 from Steam (e.g. 401) surfaces as a 400 with detail including the code."""
        cm = self._steam_mock({}, status_code=401)
        with patch("web.app.httpx.AsyncClient", return_value=cm):
            resp = client.post("/api/auth/steam",
                               json={"api_key": "bad_key", "user_id": "123"})
        assert resp.status_code == 400
        # Detail should include the upstream HTTP status code for debugging
        assert "401" in resp.json()["detail"]

    def test_successful_connect_includes_game_count(self, client):
        """Response body should include game_count from the Steam library.

        The game_count lets the frontend show "Connected — 2 games" without
        a separate library call.
        """
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
    """Edge-case tests for GET /api/library.

    Covers the top-75 RAWG enrichment cap, partial platform failures, and
    non-fatal RAWG errors that still return games (without ratings).
    """

    def test_rawg_enrichment_failure_still_returns_games_without_ratings(
        self, client, monkeypatch
    ):
        """If _enrich_with_rawg raises, games come back without ratings and an error is listed.

        RAWG enrichment is a best-effort step: if it fails the endpoint still
        returns all games from the platform fetch, just without rawg_rating values.
        The RAWG failure appears in the errors list with platform='rawg'.
        """
        monkeypatch.setenv("RAWG_API_KEY", "rawg-test-key")
        session_id = "sess-rawg-fail"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # Use games without pre-set ratings (simulating what _fetch_steam actually returns)
        bare_games = [
            {"name": "Witcher 3", "platform": "steam", "app_id": "1",
             "playtime_minutes": 100, "last_played": None, "release_year": "~",
             "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"},
            {"name": "Hades", "platform": "steam", "app_id": "2",
             "playtime_minutes": 50, "last_played": None, "release_year": "~",
             "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"},
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
        """If Steam fails but Epic succeeds, Epic's games appear and Steam is in errors.

        asyncio.gather(return_exceptions=True) lets one platform's failure be
        non-fatal.  Only the failing platform is added to errors; successful
        platforms contribute their games normally.
        """
        session_id = "sess-partial-fail"
        _sessions[session_id] = {
            "steam": {"api_key": "k", "user_id": "u"},
            "epic":  {"access_token": "t"},
        }
        epic_games = [
            {"name": "Fortnite", "platform": "epic", "app_id": "fn",
             "playtime_minutes": 0, "last_played": None, "release_year": None,
             "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"},
        ]
        with patch("web.app._fetch_steam", side_effect=RuntimeError("profile private")), \
             patch("web.app._fetch_epic", return_value=epic_games):
            resp = client.get("/api/library?skip_ratings=true",
                              cookies={"session_id": session_id})

        assert resp.status_code == 200
        data = resp.json()
        # Epic games must appear despite Steam failure
        assert data["games"][0]["name"] == "Fortnite"
        # Steam failure is reported in the errors list
        assert any(e["platform"] == "steam" for e in data["errors"])

    def test_rawg_limit_param_excludes_games_beyond_limit(self, client, monkeypatch):
        """rawg_limit=75 query param caps enrichment; the 76th game gets no rating.

        The endpoint sorts games by playtime descending, then slices [:rawg_limit]
        before enrichment. The 76th game is returned but with rawg_rating=None.
        """
        monkeypatch.setenv("RAWG_API_KEY", "rawg-key")
        session_id = "sess-76"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        # 76 games with playtime 100, 99, 98, … 25 (index 0 highest, 75 lowest)
        games_76 = [
            {"name": f"Game {i:02d}", "platform": "steam", "app_id": str(i),
             "playtime_minutes": 100 - i, "last_played": None, "release_year": "~",
             "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"}
            for i in range(76)
        ]
        def fake_enrich(games, api_key):
            return [{**g, "rawg_rating": 4.5} for g in games]

        with patch("web.app._fetch_steam", return_value=games_76), \
             patch("web.app._enrich_with_rawg", side_effect=fake_enrich) as mock_enrich:
            resp = client.get("/api/library?rawg_limit=75", cookies={"session_id": session_id})

        called_with = mock_enrich.call_args[0][0]
        assert len(called_with) == 75

        data = resp.json()
        last_game = next(g for g in data["games"] if g["name"] == "Game 75")
        assert last_game["rawg_rating"] is None

    def test_exactly_at_limit_all_enriched(self, client, monkeypatch):
        """With rawg_limit=75 and exactly 75 games, every game is enriched.

        This verifies the boundary from the other side: when the library size
        equals the limit no game is left out.
        """
        monkeypatch.setenv("RAWG_API_KEY", "rawg-key")
        session_id = "sess-75-exact"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        games_75 = [
            {"name": f"Game {i:02d}", "platform": "steam", "app_id": str(i),
             "playtime_minutes": 75 - i, "last_played": None, "release_year": "~",
             "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"}
            for i in range(75)
        ]

        def fake_enrich(games, api_key):
            return [{**g, "rawg_rating": 4.0} for g in games]

        with patch("web.app._fetch_steam", return_value=games_75), \
             patch("web.app._enrich_with_rawg", side_effect=fake_enrich) as mock_enrich:
            resp = client.get("/api/library?rawg_limit=75", cookies={"session_id": session_id})

        called_with = mock_enrich.call_args[0][0]
        assert len(called_with) == 75
        assert all(g["rawg_rating"] == 4.0 for g in resp.json()["games"])

    def test_no_rawg_limit_param_enriches_all_games(self, client, monkeypatch):
        """Without rawg_limit param, every game in the library is enriched (default).

        This is the default behaviour: no cap, all games get RAWG data.
        """
        monkeypatch.setenv("RAWG_API_KEY", "rawg-key")
        session_id = "sess-no-limit"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        games_100 = [
            {"name": f"Game {i:03d}", "platform": "steam", "app_id": str(i),
             "playtime_minutes": 200 - i, "last_played": None, "release_year": "~",
             "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"}
            for i in range(100)
        ]

        def fake_enrich(games, api_key):
            return [{**g, "rawg_rating": 4.0} for g in games]

        with patch("web.app._fetch_steam", return_value=games_100), \
             patch("web.app._enrich_with_rawg", side_effect=fake_enrich) as mock_enrich:
            resp = client.get("/api/library", cookies={"session_id": session_id})

        # All 100 games should be passed to enrichment — no limit applied
        called_with = mock_enrich.call_args[0][0]
        assert len(called_with) == 100
        assert all(g["rawg_rating"] == 4.0 for g in resp.json()["games"])


class TestRecommendCornerCases:
    """Boundary and encoding tests for GET /api/recommend.

    Covers the count parameter clamping boundaries, the max_new_minutes
    threshold semantics (≤ is unplayed, > is played), SSE JSON encoding of
    special characters, and session persistence across calls.
    """

    @pytest.fixture(autouse=True)
    def set_anthropic_key(self, monkeypatch):
        """Set the required ANTHROPIC_API_KEY for every test in this class."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    def _recommend(self, client, session_id, chunks=None, games=None, mode="library", **params):
        """Make a GET /api/recommend request with standard mocks in place."""
        games = FAKE_GAMES if games is None else games
        chunks = chunks or ["Here are my picks."]
        qs = "&".join(f"{k}={v}" for k, v in {"mode": mode, **params}.items())
        with patch("web.app._fetch_steam", return_value=games), \
             patch("web.app.anthropic.AsyncAnthropic",
                   return_value=make_claude_client(chunks)):
            return client.get(f"/api/recommend?{qs}",
                              cookies={"session_id": session_id})

    def test_count_zero_clamped_to_one(self, client):
        """max(1, min(0, 10)) == 1 — count=0 is valid and silently clamped to 1."""
        session_id = "sess-c0"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        resp = self._recommend(client, session_id, count=0)
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_count_one_minimum_valid(self, client):
        """count=1 is within [1,10] and needs no clamping — succeeds normally."""
        session_id = "sess-c1"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        resp = self._recommend(client, session_id, count=1)
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_preferences_exactly_500_chars_accepted(self, client):
        """500 chars is the limit: len > 500 is False at exactly 500 — accepted."""
        session_id = "sess-p500"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        resp = self._recommend(client, session_id, preferences="x" * 500)
        assert resp.status_code == 200
        assert any(e.get("done") is True for e in parse_sse(resp.text))

    def test_claude_text_with_quotes_and_newlines_survives_sse_roundtrip(self, client):
        """JSON special characters in Claude's output survive the SSE encoding round-trip.

        Each SSE event is `data: {json_encoded_dict}\\n\\n`.  json.dumps() inside
        the stream must properly escape quotes and newlines so that the client
        can json.loads() each event back to the original string.
        """
        session_id = "sess-special"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        tricky = 'He said "amazing game"!\nBuy it.'
        resp = self._recommend(client, session_id, chunks=[tricky])
        text_events = [e for e in parse_sse(resp.text) if "text" in e]
        full_text = "".join(e["text"] for e in text_events)
        # Quotes and newline-separated text must both survive the round-trip
        assert '"amazing game"' in full_text
        assert "Buy it." in full_text

    def test_unplayed_game_at_exact_max_new_minutes_is_included(self, client):
        """The filter is `playtime <= max_new_minutes`, so at the threshold the game is included.

        A game with exactly 60 minutes playtime at max_new_minutes=60 should
        be considered 'unplayed' (at the boundary is kept, not excluded).
        If it were excluded, the unplayed list would be empty and we'd get an
        error event; a done event proves the game was included.
        """
        session_id = "sess-exact-threshold"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        games = [{"name": "Threshold Game", "platform": "steam", "app_id": "1",
                  "playtime_minutes": 60, "last_played": None, "release_year": "~",
                  "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"}]
        resp = self._recommend(client, session_id, games=games,
                               mode="new", max_new_minutes=60)
        events = parse_sse(resp.text)
        # Should succeed — if 60-min game was excluded, unplayed list would be empty
        # and we'd get an error event instead of a done event
        assert any(e.get("done") is True for e in events), \
            "A 60-minute game should be unplayed at max_new_minutes=60"

    def test_game_with_61_minutes_is_not_unplayed_at_60_threshold(self, client):
        """61 > 60: game is played, not unplayed. All-played library → error event.

        One minute above the threshold flips the game from 'unplayed' to
        'played'.  With only one game in the library and it being played,
        the unplayed list is empty → error event (not done).
        """
        session_id = "sess-61"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        games = [{"name": "Barely Played", "platform": "steam", "app_id": "1",
                  "playtime_minutes": 61, "last_played": None, "release_year": "~",
                  "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"}]
        resp = self._recommend(client, session_id, games=games,
                               mode="new", max_new_minutes=60)
        events = parse_sse(resp.text)
        assert any("error" in e for e in events)

    def test_library_mode_with_all_unplayed_games_still_recommends(self, client):
        """Library mode has no playtime requirement — all-unplayed library is valid.

        Unlike 'new' mode, 'library' mode does not filter on playtime, so a
        library of entirely unplayed games is a valid input that should produce
        recommendations.
        """
        session_id = "sess-unplayed-lib"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}
        games = [
            {"name": f"Unplayed {i}", "platform": "steam", "app_id": str(i),
             "playtime_minutes": 0, "last_played": None, "release_year": "~",
             "rawg_rating": None, "metacritic": None, "genres": [], "tags": [], "gog_rating": "~"}
            for i in range(5)
        ]
        resp = self._recommend(client, session_id, games=games, mode="library")
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)

    def test_session_not_cleared_between_recommend_calls(self, client):
        """Session state persists across multiple calls in the same test.

        The clear_sessions fixture only runs before/after the test function,
        not between individual HTTP calls within the test.  This verifies that
        the session dict retains its data across two recommend requests.
        """
        session_id = "sess-persist"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        for _ in range(2):
            resp = self._recommend(client, session_id)
            assert any(e.get("done") is True for e in parse_sse(resp.text))


# ── GET /api/library/stream (SSE skeleton loading) ────────────────────────────

class TestLibraryStream:
    """Tests for GET /api/library/stream — streaming library load via SSE.

    The endpoint emits three event types:
      1. {"type": "games",       "games": [...], "errors": [...]}  — immediately after platform fetch
      2. {"type": "rawg_update", "game": {...}}                    — one per enriched game (skip_ratings=false)
      3. {"type": "done"}

    This class verifies the event sequence, field presence, and error handling
    for the streaming skeleton loader.
    """

    def _stream(self, client, session_id, **params):
        """GET /api/library/stream with given query params."""
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"/api/library/stream?{qs}" if qs else "/api/library/stream"
        return client.get(url, cookies={"session_id": session_id})

    def test_no_platforms_returns_400(self, client):
        """No session / no connected platform → 400 before any streaming."""
        resp = client.get("/api/library/stream")
        assert resp.status_code == 400

    def test_games_event_emitted_first(self, client):
        """The first SSE event must have type='games' and contain the games list."""
        session_id = "sess-stream-games"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = self._stream(client, session_id, skip_ratings="true")

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        games_events = [e for e in events if e.get("type") == "games"]
        assert len(games_events) == 1
        assert len(games_events[0]["games"]) == len(FAKE_GAMES)

    def test_done_event_always_last(self, client):
        """The stream always ends with a type='done' event."""
        session_id = "sess-stream-done"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = self._stream(client, session_id, skip_ratings="true")

        events = parse_sse(resp.text)
        assert events[-1].get("type") == "done"

    def test_skip_ratings_produces_no_rawg_update_events(self, client):
        """With skip_ratings=true, no rawg_update events are emitted."""
        session_id = "sess-stream-skip"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("web.app._enrich_with_rawg") as mock_rawg:
            resp = self._stream(client, session_id, skip_ratings="true")

        mock_rawg.assert_not_called()
        events = parse_sse(resp.text)
        assert not any(e.get("type") == "rawg_update" for e in events)

    def test_rawg_update_events_emitted_per_game(self, client, monkeypatch):
        """With skip_ratings=false and RAWG_API_KEY set, one rawg_update per game is emitted."""
        monkeypatch.setenv("RAWG_API_KEY", "rawg-key")
        session_id = "sess-stream-rawg"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        from game_recommender.ratings import GameRating
        fake_rating = GameRating(
            name="The Witcher 3",
            rawg_rating=4.7,
            metacritic_score=93,
            genres=["RPG"],
            tags=["Open World"],
            released="2015-05-19",
        )

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES), \
             patch("game_recommender.ratings.get_game_rating", return_value=fake_rating):
            resp = self._stream(client, session_id, skip_ratings="false")

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        rawg_events = [e for e in events if e.get("type") == "rawg_update"]
        # One rawg_update per game in FAKE_GAMES
        assert len(rawg_events) == len(FAKE_GAMES)
        # Each update carries the enriched game dict
        assert all("game" in e for e in rawg_events)

    def test_platform_error_reported_in_games_event(self, client):
        """A platform fetch failure appears in errors list within the games event."""
        session_id = "sess-stream-err"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", side_effect=RuntimeError("profile is private")):
            resp = self._stream(client, session_id, skip_ratings="true")

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        games_event = next(e for e in events if e.get("type") == "games")
        assert any(err["platform"] == "steam" for err in games_event["errors"])
        assert games_event["games"] == []

    def test_games_sorted_by_playtime_descending(self, client):
        """Games in the initial games event are sorted most-played first."""
        session_id = "sess-stream-sort"
        _sessions[session_id] = {"steam": {"api_key": "k", "user_id": "u"}}

        with patch("web.app._fetch_steam", return_value=FAKE_GAMES):
            resp = self._stream(client, session_id, skip_ratings="true")

        events = parse_sse(resp.text)
        games = next(e for e in events if e.get("type") == "games")["games"]
        playtimes = [g["playtime_minutes"] for g in games]
        assert playtimes == sorted(playtimes, reverse=True)

    def test_cached_library_streamed_without_rawg(self, client):
        """A session with a cached library (no platform) streams it and completes immediately."""
        session_id = "sess-stream-cache"
        _sessions[session_id] = {"library": FAKE_GAMES}

        resp = self._stream(client, session_id)

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.get("type") == "games" for e in events)
        assert events[-1].get("type") == "done"
        # No RAWG enrichment for pre-cached libraries
        assert not any(e.get("type") == "rawg_update" for e in events)
