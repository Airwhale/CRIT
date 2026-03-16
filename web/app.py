"""
FastAPI web application for the game recommendation system.

Architecture overview:
  - Sessions are stored in-memory (_sessions dict). This is intentional for a
    single-user local app: credentials never touch disk, and restarting the
    server clears all sessions gracefully.
  - All platform fetches (Steam, Epic, GOG) happen concurrently using
    asyncio.gather() so the library load time is bounded by the slowest
    platform, not the sum of all platforms.
  - Recommendations are streamed to the browser via Server-Sent Events (SSE)
    so the user sees Claude's output word-by-word rather than waiting for
    the full response.
  - Security headers are applied globally via middleware (not per-route).

Run with:
  uvicorn web.app:app --reload --port 8000
  # Then open http://localhost:8000
"""

import uuid
import json
import asyncio
import os
import re
import html
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus
from contextlib import asynccontextmanager

import anthropic
import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Cookie
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from dotenv import load_dotenv

# Load .env before anything else reads environment variables
load_dotenv()

app = FastAPI(title="CRIT — Curated Recommendations In Titles")

# Whether to set the Secure flag on session cookies.
# In production (HTTPS), set COOKIE_SECURE=true in .env.
# For localhost development, leave it false (Secure cookies are rejected over HTTP).
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")


# ── Security headers ──────────────────────────────────────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Apply security headers to every HTTP response.

    Using middleware (rather than per-route decorators) ensures headers are
    present on all responses including error pages, redirects, and SSE streams.
    """
    response = await call_next(request)
    # Prevent MIME-type sniffing (e.g. serving a JS file as HTML)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Prevent the app from being embedded in iframes (clickjacking protection)
    response.headers["X-Frame-Options"] = "DENY"
    # Don't send the full Referer header to third-party sites
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Content Security Policy: allow scripts only from self and the CDN that
    # serves marked.js and DOMPurify. Inline styles are allowed because the
    # template uses them for dynamic color coding.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self';"
    )
    return response


# ── Session store ─────────────────────────────────────────────────────────────
# Simple in-memory dict: session_id (UUID string) → {platform: credentials}
# This is intentional: credentials never persist to disk, and clearing sessions
# is as simple as restarting the server. Not suitable for multi-user production.
_sessions: dict[str, dict] = {}
_session_last_seen: dict[str, float] = {}

# Keep in-memory sessions bounded and age them out so memory cannot grow
# indefinitely if many sessions are created over long-running uptime.
_SESSION_MAX = 256
_SESSION_MAX_AGE_SECONDS = 60 * 60 * 8


def _prune_sessions(now: float | None = None) -> None:
    """Drop expired sessions and evict oldest sessions when above capacity."""
    now = now or time.time()

    expired = [
        sid for sid in _sessions
        if now - _session_last_seen.get(sid, now) > _SESSION_MAX_AGE_SECONDS
    ]
    for sid in expired:
        _sessions.pop(sid, None)
        _session_last_seen.pop(sid, None)

    overflow = len(_sessions) - _SESSION_MAX
    if overflow > 0:
        oldest = sorted(_sessions, key=lambda sid: _session_last_seen.get(sid, 0))
        for sid in oldest[:overflow]:
            _sessions.pop(sid, None)
            _session_last_seen.pop(sid, None)


def _get_session(session_id: str | None) -> tuple[str, dict]:
    """Return the existing session or create a new one.

    Args:
        session_id: The value from the session_id cookie, or None if absent.

    Returns:
        (session_id, session_data) tuple. If the cookie was valid, returns
        the existing session; otherwise creates a new UUID and empty session.
    """
    _prune_sessions()

    if session_id and session_id in _sessions:
        _session_last_seen[session_id] = time.time()
        return session_id, _sessions[session_id]
    # Create a fresh session with a new UUID
    new_id = str(uuid.uuid4())
    _sessions[new_id] = {}
    _session_last_seen[new_id] = time.time()
    return new_id, _sessions[new_id]


@asynccontextmanager
async def _sse_timeout(seconds: int):
    """Python-version-safe timeout context for SSE recommendation streams."""
    if hasattr(asyncio, "timeout"):
        async with asyncio.timeout(seconds):
            yield
    else:
        yield


def _set_session_cookie(response: Response, session_id: str) -> None:
    """Attach the session cookie to an outgoing response.

    Settings:
      httponly=True  — JavaScript cannot read the cookie (XSS mitigation)
      samesite=lax   — Sent on top-level navigations from external sites (required
                       for OAuth/OpenID redirect callbacks: Steam redirects back from
                       steamcommunity.com, Epic redirects back from epicgames.com).
                       samesite=strict would block these redirects, causing the server
                       to create a new session for the callback and orphan the existing
                       one (losing Epic/GOG credentials when connecting Steam, etc.).
                       lax still blocks cross-origin POST/iframe requests, protecting
                       against CSRF on all state-changing endpoints.
      secure=?       — Only sent over HTTPS in production (configurable)
      max_age=8h     — Session expires after 8 hours of inactivity
    """
    response.set_cookie(
        "session_id",
        session_id,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="lax",
        max_age=60 * 60 * 8,  # 8 hours in seconds
    )


# ── HTML page ─────────────────────────────────────────────────────────────────
TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-page frontend.

    The entire UI is a single HTML file — no build step, no bundler,
    no framework. It embeds CSS and vanilla JS directly.
    """
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


# ── Auth: Steam OpenID redirect flow ──────────────────────────────────────────
# Steam supports OpenID 2.0 so users can "Login with Steam" without needing to
# generate a developer API key. After the redirect the Steam ID is extracted from
# the verified claimed_id URL. The library is then fetched via the public XML feed
# (no API key required). An API key can be added separately to unlock wishlist
# support in Steam Deals mode.

_STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"


@app.get("/auth/steam/start")
async def steam_auth_start(request: Request, session_id: str | None = Cookie(default=None)):
    """Redirect the browser to Steam's OpenID login page."""
    sid, _ = _get_session(session_id)
    base = str(request.base_url).rstrip("/")
    return_to = f"{base}/auth/steam/callback"
    realm = base + "/"
    params = {
        "openid.ns":         "http://specs.openid.net/auth/2.0",
        "openid.mode":       "checkid_setup",
        "openid.return_to":  return_to,
        "openid.realm":      realm,
        "openid.identity":   "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    auth_url = _STEAM_OPENID_URL + "?" + "&".join(
        f"{k}={quote_plus(v)}" for k, v in params.items()
    )
    response = RedirectResponse(auth_url)
    _set_session_cookie(response, sid)
    return response


@app.get("/auth/steam/callback")
async def steam_auth_callback(
    request: Request,
    session_id: str | None = Cookie(default=None),
):
    """Verify the Steam OpenID response and store the Steam ID in the session.

    Steam redirects here after login with a signed set of openid.* query params.
    We verify the signature by POSTing back to Steam with openid.mode=check_authentication
    and checking for 'is_valid:true' in the response. If valid, the Steam ID is
    extracted from the claimed_id URL and stored in the session (no API key).
    """
    params = dict(request.query_params)

    if params.get("openid.mode") == "cancel":
        return RedirectResponse("/?auth_error=login_cancelled")

    # Replace mode and POST back to Steam for verification
    verify_params = {**params, "openid.mode": "check_authentication"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_STEAM_OPENID_URL, data=verify_params)

    if "is_valid:true" not in resp.text:
        return RedirectResponse("/?auth_error=steam_verification_failed")

    # claimed_id looks like: https://steamcommunity.com/openid/id/76561198XXXXXXXXX
    claimed_id = params.get("openid.claimed_id", "")
    match = re.search(r"/openid/id/(\d+)$", claimed_id)
    if not match:
        return RedirectResponse("/?auth_error=could_not_extract_steam_id")

    steam_id = match.group(1)
    sid, session = _get_session(session_id)
    # Store without api_key — _fetch_steam will use the server STEAM_API_KEY
    # env var if available, otherwise fall back to the public XML feed.
    session["steam"] = {"user_id": steam_id, "api_key": None}

    response = RedirectResponse("/?auth_success=steam")
    _set_session_cookie(response, sid)
    return response


# ── Auth: Epic OAuth redirect flow ────────────────────────────────────────────
# This is the fully automated path: browser is redirected to Epic, user logs in,
# Epic sends them back to our callback with ?code=XXX, we exchange silently.
# Epic's launcherAppClient2 was designed for desktop launchers that use localhost
# callbacks, so localhost redirect URIs are accepted without pre-registration.

@app.get("/auth/epic/start")
async def epic_auth_start(request: Request, session_id: str | None = Cookie(default=None)):
    """Redirect the browser to Epic's OAuth authorization page.

    The callback URL is dynamically constructed from the current request's
    base_url so this works on both localhost and any deployed hostname.
    """
    sid, _ = _get_session(session_id)
    base = str(request.base_url).rstrip("/")
    callback_uri = f"{base}/auth/epic/callback"
    auth_url = (
        "https://www.epicgames.com/id/authorize"
        f"?client_id=34a02cf8f4414e29b15921876da36f9a"
        f"&response_type=code"
        f"&redirect_uri={callback_uri}"
        f"&scope=basic_profile"
    )
    # Set the session cookie on the redirect so the callback can find this session
    response = RedirectResponse(auth_url)
    _set_session_cookie(response, sid)
    return response


@app.get("/auth/epic/callback")
async def epic_auth_callback(
    request: Request,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    session_id: str | None = Cookie(default=None),
):
    """Receive the authorization code from Epic and exchange it for tokens.

    Epic redirects here after the user logs in. If the user cancels or an
    error occurs, Epic sends ?error= instead of ?code=. Both are handled
    by redirecting back to the home page with an appropriate query parameter
    that the frontend reads and displays as a toast message.
    """
    if error or not code:
        # User cancelled login or Epic returned an error
        reason = error_description or error or "login_cancelled"
        return RedirectResponse(f"/?auth_error={quote_plus(reason)}")

    from game_recommender.epic import exchange_code
    try:
        # Run the synchronous exchange_code() in a thread pool so we don't
        # block the async event loop during the HTTP round-trip to Epic.
        # Pass the redirect_uri so it matches what was sent in the authorization
        # request — required by RFC 6749 §4.1.3 when redirect_uri was included.
        base = str(request.base_url).rstrip("/")
        callback_uri = f"{base}/auth/epic/callback"
        tokens = await asyncio.to_thread(exchange_code, code, callback_uri)
    except Exception as e:
        # Truncate to 120 chars so the error fits in a URL query parameter
        return RedirectResponse(f"/?auth_error={quote_plus(str(e)[:120])}")

    if "access_token" not in tokens:
        # Epic returned 200 but with an error payload (e.g. code already used)
        msg = tokens.get("errorMessage", "token_exchange_failed")
        return RedirectResponse(f"/?auth_error={quote_plus(msg[:120])}")

    # Store credentials in the session — never written to disk
    sid, session = _get_session(session_id)
    session["epic"] = {
        "access_token":  tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "account_id":    tokens.get("account_id"),
    }
    # ?auth_success=epic tells the frontend to show a "Connected!" toast
    response = RedirectResponse("/?auth_success=epic")
    _set_session_cookie(response, sid)
    return response


# ── Auth: status ──────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status(session_id: str | None = Cookie(default=None)):
    """Return which platforms are connected in the current session.

    The frontend polls this on page load to restore the connected state after
    a page refresh (as long as the session cookie and server process are alive).
    """
    _, session = _get_session(session_id)
    steam_creds = session.get("steam")
    epic_creds  = session.get("epic")
    gog_creds   = session.get("gog")
    return {
        "steam":             bool(steam_creds),
        "steam_has_api_key": bool(steam_creds and (steam_creds.get("api_key") or os.environ.get("STEAM_API_KEY"))),
        "steam_user_id":     steam_creds.get("user_id") if steam_creds else None,
        "epic":              bool(epic_creds),
        "epic_tokens":       {
            "access_token":  epic_creds.get("access_token"),
            "refresh_token": epic_creds.get("refresh_token"),
            "account_id":    epic_creds.get("account_id"),
        } if epic_creds else None,
        "gog":               bool(gog_creds),
        "gog_tokens":        {
            "access_token":  gog_creds.get("access_token"),
            "refresh_token": gog_creds.get("refresh_token"),
            "user_id":       gog_creds.get("user_id"),
        } if gog_creds else None,
    }


# ── Auth: Steam ───────────────────────────────────────────────────────────────

@app.post("/api/auth/steam")
async def connect_steam(
    request: Request,
    session_id: str | None = Cookie(default=None),
):
    """Validate and store Steam credentials.

    api_key is optional. When provided, credentials are validated via the Web API
    (IPlayerService/GetOwnedGames). When omitted, the user_id is validated via the
    public XML feed instead — no API key needed.

    Request body (JSON): {"user_id": "...", "api_key": "..."}  (api_key optional)
    Response: {"ok": true, "game_count": N}
    """
    body = await request.json()
    api_key = (body.get("api_key") or "").strip() or None
    user_id = (body.get("user_id") or "").strip()

    if not user_id:
        raise HTTPException(400, "user_id is required")

    if api_key:
        # Validate with the Steam Web API
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/",
                params={
                    "key":             api_key,
                    "steamid":         user_id,
                    "include_appinfo": True,
                    "format":          "json",
                },
            )
        if resp.status_code != 200:
            raise HTTPException(400, f"Steam API error: {resp.status_code}")
        data = resp.json()
        if not data.get("response"):
            raise HTTPException(400, "No response from Steam. Is your profile public?")
        game_count = len(data["response"].get("games", []))
    else:
        # Validate via the public XML feed (no API key required)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"https://steamcommunity.com/profiles/{user_id}/games",
                params={"xml": "1"},
            )
        if resp.status_code != 200:
            raise HTTPException(400, f"Steam returned HTTP {resp.status_code}")
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            raise HTTPException(400, "Steam returned invalid data. Is the Steam ID correct?")
        error_el = root.find("error")
        if error_el is not None:
            raise HTTPException(400, f"Steam error: {error_el.text}")
        games_el = root.find("games")
        if games_el is None:
            raise HTTPException(400, "No games found. Is your Steam profile set to public?")
        game_count = len(games_el.findall("game"))

    sid, session = _get_session(session_id)
    session["steam"] = {"api_key": api_key, "user_id": user_id}

    response = JSONResponse({"ok": True, "game_count": game_count})
    _set_session_cookie(response, sid)
    return response


@app.post("/api/auth/steam/apikey")
async def add_steam_apikey(
    request: Request,
    session_id: str | None = Cookie(default=None),
):
    """Add or replace the Steam API key on an existing Steam session.

    Used after an OpenID login (which stores only the user_id) to unlock
    wishlist support in Steam Deals mode.

    Request body (JSON): {"api_key": "..."}
    """
    _, session = _get_session(session_id)
    if "steam" not in session:
        raise HTTPException(400, "Connect Steam first before adding an API key")

    body = await request.json()
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")

    user_id = session["steam"]["user_id"]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/",
            params={"key": api_key, "steamid": user_id, "format": "json"},
        )
    if resp.status_code != 200:
        raise HTTPException(400, f"Steam API error: {resp.status_code}")
    if not resp.json().get("response"):
        raise HTTPException(400, "API key validation failed. Is your profile public?")

    session["steam"]["api_key"] = api_key
    return JSONResponse({"ok": True})


# ── Auth: Epic (manual code fallback) ─────────────────────────────────────────

@app.post("/api/auth/epic")
async def connect_epic(
    request: Request,
    session_id: str | None = Cookie(default=None),
):
    """Store Epic credentials entered via the manual code paste fallback.

    This endpoint is used when the OAuth redirect flow fails (e.g. redirect URI
    mismatch, pop-up blocker). The user manually visits EPIC_AUTH_URL, copies
    the authorizationCode from the JSON response, and pastes it here.

    Request body (JSON): {"auth_code": "..."}
    """
    body = await request.json()
    auth_code = (body.get("auth_code") or "").strip()

    if not auth_code:
        raise HTTPException(400, "auth_code is required")

    from game_recommender.epic import exchange_code
    try:
        tokens = await asyncio.to_thread(exchange_code, auth_code)
    except Exception as e:
        raise HTTPException(400, f"Epic auth failed: {e}")

    if "access_token" not in tokens:
        raise HTTPException(400, f"Epic returned unexpected response: {tokens.get('errorMessage', 'unknown error')}")

    sid, session = _get_session(session_id)
    session["epic"] = {
        "access_token":  tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "account_id":    tokens.get("account_id"),
    }

    response = JSONResponse({"ok": True})
    _set_session_cookie(response, sid)
    return response


# ── Auth: GOG ─────────────────────────────────────────────────────────────────

@app.post("/api/auth/gog")
async def connect_gog(
    request: Request,
    session_id: str | None = Cookie(default=None),
):
    """Store GOG credentials from the code pasted by the user.

    GOG's redirect URI is fixed to embed.gog.com, so we cannot receive the
    callback directly. The frontend opens a popup, the user logs in, and then
    either the postMessage listener auto-captures the code from the popup URL,
    or the user pastes it manually from the URL bar.

    Request body (JSON): {"auth_code": "..."}
    """
    body = await request.json()
    auth_code = (body.get("auth_code") or "").strip()

    if not auth_code:
        raise HTTPException(400, "auth_code is required")

    from game_recommender.gog import exchange_code
    try:
        tokens = await asyncio.to_thread(exchange_code, auth_code)
    except Exception as e:
        raise HTTPException(400, f"GOG auth failed: {e}")

    if "access_token" not in tokens:
        raise HTTPException(400, f"GOG returned unexpected response: {tokens}")

    sid, session = _get_session(session_id)
    session["gog"] = {
        "access_token":  tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "user_id":       tokens.get("user_id"),
    }

    response = JSONResponse({"ok": True})
    _set_session_cookie(response, sid)
    return response


# ── Auth: disconnect ──────────────────────────────────────────────────────────

@app.delete("/api/auth/{platform}")
async def disconnect(platform: str, session_id: str | None = Cookie(default=None)):
    """Remove a platform's credentials from the session.

    The user can disconnect individual platforms without affecting others.
    Credentials are simply deleted from the in-memory session dict — nothing
    else needs to happen since they were never stored persistently.
    """
    if platform not in ("steam", "epic", "gog"):
        raise HTTPException(404, "Unknown platform")
    _, session = _get_session(session_id)
    session.pop(platform, None)  # pop with default avoids KeyError if already missing
    return {"ok": True}


# ── Library ───────────────────────────────────────────────────────────────────

@app.get("/api/library")
async def get_library(
    skip_ratings: bool = False,
    rawg_limit: int | None = None,
    session_id: str | None = Cookie(default=None),
):
    """Fetch and merge game libraries from all connected platforms.

    Concurrent fetch: all platform fetches run simultaneously via asyncio.gather().
    A failure in one platform (e.g. expired token, private profile) is recorded
    in the "errors" list but doesn't prevent the other platforms from returning.

    Post-fetch RAWG enrichment: by default every game is enriched. Pass
    rawg_limit=N to cap enrichment to the top N games by playtime (useful
    on large libraries to reduce load time).

    Returns:
        {"games": [...], "errors": [...]}
        Each game dict: name, platform, app_id, playtime_minutes, rawg_rating,
                        metacritic, genres
    """
    _, session = _get_session(session_id)

    has_platforms = any(k in session for k in ("steam", "epic", "gog"))
    if not has_platforms:
        # Fall back to a library imported from CSV, if one was cached
        if "library" in session:
            return {"games": session["library"], "errors": []}
        raise HTTPException(400, "No platforms connected. Connect at least one platform first.")

    # Build a dict of {platform_name: coroutine} for the connected platforms.
    # asyncio.to_thread() wraps the synchronous platform fetchers so they run
    # in a thread pool without blocking the async event loop.
    tasks = {}
    if "steam" in session:
        tasks["steam"] = asyncio.to_thread(_fetch_steam, session["steam"])
    if "epic" in session:
        tasks["epic"] = asyncio.to_thread(_fetch_epic, session["epic"])
    if "gog" in session:
        tasks["gog"] = asyncio.to_thread(_fetch_gog, session["gog"])

    # return_exceptions=True prevents one platform failure from cancelling others.
    # Failed tasks return Exception objects; successful tasks return lists of dicts.
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    all_games = []
    errors = []
    for platform, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            # Record the error and continue — partial results are better than none
            errors.append({"platform": platform, "error": str(result)})
        else:
            all_games.extend(result)

    # Primary sort: most-played first. Secondary sort: alphabetical for ties.
    all_games.sort(key=lambda g: (-g["playtime_minutes"], g["name"].lower()))

    # ── Optional RAWG enrichment ──────────────────────────────────────────────
    rawg_key = os.environ.get("RAWG_API_KEY")
    if not skip_ratings and rawg_key and all_games:
        try:
            to_enrich = all_games[:rawg_limit] if rawg_limit is not None else all_games
            enriched = await asyncio.to_thread(_enrich_with_rawg, to_enrich, rawg_key)
            all_games = enriched + (all_games[rawg_limit:] if rawg_limit is not None else [])
        except Exception as e:
            # RAWG failure is non-fatal — games still display without ratings
            errors.append({"platform": "rawg", "error": str(e)})

    return {"games": all_games, "errors": errors}


@app.get("/api/library/stream")
async def stream_library(
    skip_ratings: bool = False,
    rawg_limit: int | None = None,
    session_id: str | None = Cookie(default=None),
):
    """Stream library loading via SSE, showing games immediately then enriching with RAWG.

    Event sequence:
      1. {"type": "games",       "games": [...], "errors": [...]}  — all games, unenriched
      2. {"type": "rawg_update", "game": {...}}                    — one per enriched game (out of order)
      3. {"type": "done"}

    This lets the UI render the table as soon as platform fetch completes, then
    update individual rows in-place as RAWG data arrives for each game.
    """
    _, session = _get_session(session_id)

    has_platforms = any(k in session for k in ("steam", "epic", "gog"))
    if not has_platforms:
        if "library" in session:
            async def _cached_stream():
                yield f'data: {json.dumps({"type": "games", "games": session["library"], "errors": []})}\n\n'
                yield f'data: {json.dumps({"type": "done"})}\n\n'
            return StreamingResponse(_cached_stream(), media_type="text/event-stream")
        raise HTTPException(400, "No platforms connected. Connect at least one platform first.")

    async def event_stream():
        # ── Phase 1: fetch all platforms concurrently ──────────────────────────
        fetch_tasks = {}
        if "steam" in session:
            fetch_tasks["steam"] = asyncio.to_thread(_fetch_steam, session["steam"])
        if "epic" in session:
            fetch_tasks["epic"] = asyncio.to_thread(_fetch_epic, session["epic"])
        if "gog" in session:
            fetch_tasks["gog"] = asyncio.to_thread(_fetch_gog, session["gog"])

        results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)

        all_games: list[dict] = []
        errors: list[dict] = []
        for platform, result in zip(fetch_tasks.keys(), results):
            if isinstance(result, Exception):
                errors.append({"platform": platform, "error": str(result)})
            else:
                all_games.extend(result)

        all_games.sort(key=lambda g: (-g["playtime_minutes"], g["name"].lower()))

        # Immediately yield all games (unenriched) so the UI can render the table
        yield f'data: {json.dumps({"type": "games", "games": all_games, "errors": errors})}\n\n'

        # ── Phase 2: RAWG enrichment — stream one update per game ─────────────
        rawg_key = os.environ.get("RAWG_API_KEY")
        if not skip_ratings and rawg_key and all_games:
            from game_recommender.ratings import get_game_rating

            to_enrich = all_games[:rawg_limit] if rawg_limit is not None else all_games
            sem = asyncio.Semaphore(5)

            async def _enrich_one(game: dict) -> dict:
                async with sem:
                    try:
                        rating = await asyncio.to_thread(get_game_rating, game["name"], rawg_key)
                    except Exception:
                        return game
                    if not rating:
                        return game
                    cur_year = game.get("release_year")
                    if rating.released and cur_year in (None, "~"):
                        new_year = int(rating.released[:4])
                    elif cur_year == "~" and not rating.released:
                        new_year = None
                    else:
                        new_year = cur_year
                    return {**game,
                        "rawg_rating":  rating.rawg_rating,
                        "metacritic":   rating.metacritic_score,
                        "genres":       [_fix_mojibake(g) for g in rating.genres],
                        "tags":         [_fix_mojibake(t) for t in (rating.tags or [])],
                        "release_year": new_year,
                    }

            tasks = [asyncio.create_task(_enrich_one(g)) for g in to_enrich]
            for fut in asyncio.as_completed(tasks):
                enriched = await fut
                yield f'data: {json.dumps({"type": "rawg_update", "game": enriched})}\n\n'

        yield f'data: {json.dumps({"type": "done"})}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/library/cache")
async def cache_library(
    body: dict,
    response: Response,
    session_id: str | None = Cookie(default=None),
):
    """Store a client-side library (e.g. from CSV import) in the server session.

    This lets /api/recommend use the imported games even when no platform
    credentials are connected. The cached library is returned by get_library()
    as a fallback when no platforms are present.
    """
    games = body.get("games")
    if not isinstance(games, list):
        raise HTTPException(400, "body must be {\"games\": [...]}")
    sid, session = _get_session(session_id)
    session["library"] = games
    _set_session_cookie(response, sid)
    return {"ok": True, "count": len(games)}


# ── Platform fetch helpers (synchronous, run in thread pool) ──────────────────

def _fetch_steam(creds: dict) -> list[dict]:
    """Fetch the Steam library for the credentials stored in session.

    Priority:
      1. User-supplied api_key (stored after manual connection or /api/auth/steam/apikey)
      2. Server-side STEAM_API_KEY env var — lets OpenID-authenticated users get
         full Web API data (playtime, last_played, F2P games) without supplying
         their own key
      3. Public XML feed — no credentials required; less data (no last_played,
         fewer F2P titles)
    """
    api_key = creds.get("api_key") or os.environ.get("STEAM_API_KEY")
    user_id = creds["user_id"]
    if api_key:
        from game_recommender.steam import get_steam_library
        games = get_steam_library(api_key, user_id)
    else:
        from game_recommender.steam import get_steam_library_xml
        games = get_steam_library_xml(user_id)
    return [_game_to_dict(g) for g in games]


def _fetch_epic(creds: dict) -> list[dict]:
    """Fetch the Epic library, automatically retrying with a token refresh on 401.

    Epic access tokens expire after ~2 hours. Rather than forcing re-authentication,
    we silently refresh on HTTP 401 and retry once. Non-auth errors (network failures,
    malformed API responses, etc.) propagate immediately without a refresh attempt.
    """
    import requests as _req
    from game_recommender.epic import get_epic_library, refresh_tokens
    try:
        games = get_epic_library(creds["access_token"])
    except _req.HTTPError as exc:
        # Only attempt refresh for authentication failures (401 Unauthorized).
        # Other HTTP errors (5xx, rate limits, etc.) are not fixed by refreshing.
        if exc.response is not None and exc.response.status_code != 401:
            raise
        if not creds.get("refresh_token"):
            raise  # No refresh token — user must re-authenticate
        new_tokens = refresh_tokens(creds["refresh_token"])
        # Update the in-memory credentials so subsequent calls use the new token
        creds["access_token"]  = new_tokens["access_token"]
        creds["refresh_token"] = new_tokens.get("refresh_token", creds["refresh_token"])
        games = get_epic_library(creds["access_token"])
    return [_game_to_dict(g) for g in games]


def _fetch_gog(creds: dict) -> list[dict]:
    """Fetch the GOG library with the same token-refresh retry logic as _fetch_epic."""
    from game_recommender.gog import get_gog_library, refresh_tokens
    try:
        games = get_gog_library(creds["access_token"])
    except Exception:
        if creds.get("refresh_token"):
            new_tokens = refresh_tokens(creds["refresh_token"])
            creds["access_token"]  = new_tokens["access_token"]
            creds["refresh_token"] = new_tokens.get("refresh_token", creds["refresh_token"])
            games = get_gog_library(creds["access_token"])
        else:
            raise
    return [_game_to_dict(g) for g in games]


def _fix_mojibake(s: str) -> str:
    """Clean up common encoding problems in strings from platform/RAWG APIs.

    1. Unescape HTML entities: &amp; → &, &#39; → ', Tom Clancy&#39;s → Tom Clancy's
    2. Fix UTF-8 bytes mis-decoded as Latin-1: â€™ → ', ðŸŸ¦ → 🟦
    """
    if not s:
        return s
    s = html.unescape(s)
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _game_to_dict(game) -> dict:
    """Serialize a Game dataclass to a JSON-compatible dict.

    The rating fields (rawg_rating, metacritic, genres) are initialized to
    their "empty" values here; _enrich_with_rawg will overwrite them for
    games that have RAWG data.
    """
    # "~" is a sentinel meaning "this platform does not provide this field at all".
    # None means "the platform has this field type but no data for this specific game".
    # The frontend renders "~" as – and None as ○.
    steam = game.platform == "steam"
    gog   = game.platform == "gog"
    return {
        "name":             _fix_mojibake(game.name),
        "platform":         game.platform,
        "app_id":           game.app_id,
        "playtime_minutes": game.playtime_minutes,
        "last_played":      game.last_played,
        "rawg_rating":      None,   # Populated by _enrich_with_rawg if called
        "metacritic":       None,   # Populated by _enrich_with_rawg if called
        "genres":           [],     # Populated by _enrich_with_rawg if called
        "tags":             [],     # Populated by _enrich_with_rawg if called
        # Steam library API has no release dates — use "~" so the UI shows –.
        # RAWG enrichment will fill this in when it finds the game.
        "release_year":     game.release_year if not steam else (game.release_year or "~"),
        # GOG community rating: non-GOG platforms use "~" (not applicable).
        "gog_rating":       game.gog_rating if gog else "~",
    }


def _enrich_with_rawg(games: list[dict], api_key: str) -> list[dict]:
    """Add RAWG rating data to a list of game dicts.

    Uses a thread pool to fetch ratings concurrently (5 workers), which keeps
    wall-clock time reasonable even for large libraries. Results are cached
    in-process, so a second load of the same library is near-instant.

    Returns a new list of dicts with rawg_rating, metacritic, and genres
    fields filled in where RAWG found a match. Unmatched games keep None/[].
    """
    from game_recommender.ratings import get_game_rating
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(game: dict) -> dict:
        try:
            rating = get_game_rating(game["name"], api_key=api_key)
        except Exception:
            return game  # Network/auth error for this title — skip rating, keep game
        if rating:
            # Fill release_year from RAWG if the platform didn't supply one.
            # "~" (Steam sentinel) and None (Epic/GOG with no native date) both
            # get filled in from RAWG. If RAWG has no date either, "~" becomes
            # None (RAWG knows the game but has no release date → ○, not –).
            cur_year = game.get("release_year")
            if rating.released and cur_year in (None, "~"):
                new_year = int(rating.released[:4])
            elif cur_year == "~" and not rating.released:
                new_year = None  # RAWG found game but has no date → ○
            else:
                new_year = cur_year  # Keep platform-supplied year
            return {**game,
                "rawg_rating":  rating.rawg_rating,
                "metacritic":   rating.metacritic_score,
                "genres":       [_fix_mojibake(g) for g in rating.genres],
                "tags":         [_fix_mojibake(t) for t in (rating.tags or [])],
                "release_year": new_year,
            }
        return game

    results = [None] * len(games)
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_idx = {pool.submit(fetch_one, g): i for i, g in enumerate(games)}
        for future in as_completed(future_to_idx):
            results[future_to_idx[future]] = future.result()
    return results


# ── Recommendations (SSE) ─────────────────────────────────────────────────────

@app.get("/api/recommend")
async def recommend(
    mode: str = "library",          # "library" | "sales" | "new"
    preferences: str = "",          # Freeform user mood/preference text
    count: int = 5,                 # Number of recommendations to request
    # Sales mode options
    min_discount: int = 40,         # Minimum discount percentage to include
    deal_sources: str = "",         # Comma-separated source keys; empty = all
    # Backlog mode options
    max_new_minutes: int = 60,      # Games with <= this many minutes are "unplayed"
    # Model options
    model: str = "claude-sonnet-4-6",
    use_thinking: bool = False,
    session_id: str | None = Cookie(default=None),
):
    """Stream game recommendations from Claude via Server-Sent Events.

    SSE format: each event is a JSON-encoded line:
      data: {"type": "status", "msg": "Loading your library…"}\n\n
      data: {"text": "Here are my picks..."}\n\n   (one per Claude text chunk)
      data: {"done": true}\n\n                      (signals stream end)
      data: {"error": "something went wrong"}\n\n   (stream ends on error)

    Validation failures (bad mode, missing key, preferences too long) return
    HTTP 400 before the stream opens. Runtime errors (empty library, Claude
    exception) are sent as error events within the stream.

    Three recommendation modes:
      library — recommend games the user already owns (default)
      sales   — find current Steam deals that match the user's taste
      new     — suggest unplayed games from the user's backlog
    """
    _, session = _get_session(session_id)

    # Pre-stream validation — these checks return 400 before any SSE headers are sent
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise HTTPException(400, "ANTHROPIC_API_KEY not configured on the server")
    if not session:
        raise HTTPException(400, "No platforms connected")
    if mode not in ("library", "sales", "new", "discover"):
        raise HTTPException(400, f"Unknown mode: {mode}")
    if len(preferences) > 500:
        raise HTTPException(400, "preferences must be 500 characters or fewer")

    # Clamp count silently rather than erroring — improves UX for edge inputs
    count = max(1, min(count, 50))

    async def event_stream():
        """Async generator that yields SSE-formatted data lines."""

        def status(msg: str):
            """Helper: format a status message as an SSE event."""
            return f'data: {json.dumps({"type": "status", "msg": msg})}\n\n'

        # ── Step 1: Fetch library (needed by all three modes) ─────────────────
        yield status("Loading your library…")
        try:
            # Call the library endpoint internally rather than duplicating its logic.
            # skip_ratings=True because we don't need RAWG data for the recommendation
            # prompt — the LLM only needs game names and playtime for context.
            library_resp = await get_library(skip_ratings=True, session_id=session_id)
            games_raw = library_resp["games"]
        except Exception as e:
            yield f'data: {json.dumps({"error": str(e)})}\n\n'
            return  # Abort the stream

        if not games_raw:
            yield f'data: {json.dumps({"error": "Library is empty"})}\n\n'
            return

        # ── Step 2: Mode-specific data gathering ──────────────────────────────

        if mode == "sales":
            from game_recommender.steam_sales import _ALL_SOURCES
            steam_creds = session.get("steam", {})
            owned_ids = {g["app_id"] for g in games_raw if g.get("app_id")}

            # Parse requested sources; default to all when none specified
            sources = (
                set(deal_sources.split(",")) & _ALL_SOURCES
                if deal_sources
                else _ALL_SOURCES
            )
            has_steam_key = bool(steam_creds.get("api_key"))
            # Wishlist needs credentials — silently drop it if unavailable
            if "steam_wishlist" in sources and not has_steam_key:
                sources = sources - {"steam_wishlist"}
            if "steam_wishlist" in sources:
                yield status("Checking your Steam wishlist — up to 100 items, ~20 sec…")
            else:
                yield status("Fetching deals…")

            try:
                sale_games, sale_warnings = await asyncio.to_thread(
                    _fetch_sales,
                    min_discount,
                    steam_creds,
                    sources,
                    owned_ids,
                )
            except Exception as e:
                yield f'data: {json.dumps({"error": f"Sales fetch failed: {e}"})}\n\n'
                return

            # Surface warnings for any deal sources that failed
            if sale_warnings:
                yield f'data: {json.dumps({"warnings": sale_warnings})}\n\n'

            if not sale_games:
                yield f'data: {json.dumps({"error": "No sales found above the discount threshold. Try lowering it."})}\n\n'
                return

            # Emit the raw deals list so the frontend can render a table
            deals_payload = [
                {
                    "name":     g.name,
                    "store":    g.store,
                    "app_id":   g.app_id,
                    "discount": g.discount_percent,
                    "sale":     g.sale_price,
                    "original": g.original_price,
                    "wishlist": g.from_wishlist,
                    "url":      g.store_url,
                }
                for g in sale_games
            ]
            yield f'data: {json.dumps({"deals": deals_payload})}\n\n'

            # Enrich deals with ITAD historical lows (non-blocking, non-fatal)
            itad_key = os.environ.get("ITAD_API_KEY")
            if itad_key:
                yield status("Looking up price history on IsThereAnyDeal…")
                try:
                    itad_data = await asyncio.to_thread(
                        _enrich_deals_with_itad, sale_games, itad_key
                    )
                    if itad_data:
                        yield f'data: {json.dumps({"itad_data": itad_data})}\n\n'
                except Exception:
                    pass  # ITAD enrichment failure is non-fatal

            prompt = _build_sales_prompt(games_raw, sale_games, preferences, count)

        elif mode == "new":
            # "New" means games the user hasn't played (or barely played).
            # max_new_minutes is the threshold: games at or below it are "unplayed".
            unplayed = [g for g in games_raw if g["playtime_minutes"] <= max_new_minutes]
            if not unplayed:
                yield f'data: {json.dumps({"error": "No unplayed games found in your library."})}\n\n'
                return
            prompt = _build_new_game_prompt(games_raw, unplayed, preferences, count)

        elif mode == "discover":
            prompt = _build_discover_prompt(games_raw, preferences, count)

        else:  # mode == "library"
            prompt = _build_library_prompt(games_raw, preferences, count)

        # ── Step 3: Stream Claude's response ──────────────────────────────────
        yield status("Asking Claude…")

        # Use AsyncAnthropic so the streaming doesn't block the event loop
        client = anthropic.AsyncAnthropic(api_key=anthropic_key)
        api_kwargs = {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_thinking:
            api_kwargs["thinking"] = {"type": "adaptive"}
        try:
            async with _sse_timeout(240):
                async with client.messages.stream(**api_kwargs) as stream:
                    # Yield each text chunk as it arrives — the browser renders it immediately
                    async for text in stream.text_stream:
                        # json.dumps handles quoting, escaping newlines, etc.
                        yield f'data: {json.dumps({"text": text})}\n\n'

            # Signal that the stream is complete so the frontend can hide the spinner
            yield 'data: {"done": true}\n\n'

        except TimeoutError:
            yield f'data: {json.dumps({"error": "Claude response timed out after 4 minutes."})}\n\n'
        except Exception as e:
            # Yield the error as an SSE event rather than crashing the stream
            yield f'data: {json.dumps({"error": str(e)})}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",       # Don't cache SSE responses
            "X-Accel-Buffering": "no",         # Disable nginx buffering for SSE
        },
    )


# ── Sales helper ──────────────────────────────────────────────────────────────

def _fetch_sales(
    min_discount: int,
    steam_creds: dict,
    sources: set[str],
    owned_ids: set[str],
) -> tuple[list, list[str]]:
    """Wrapper around get_all_sales, extracting credentials from the session dict.

    Returns (sale_games, warnings) where warnings lists any sources that failed.
    """
    from game_recommender.steam_sales import get_all_sales
    warnings: list[str] = []
    games = get_all_sales(
        min_discount=min_discount,
        steam_api_key=steam_creds.get("api_key"),
        steam_user_id=steam_creds.get("user_id"),
        sources=sources,
        owned_app_ids=owned_ids,
        error_callback=lambda source, err: warnings.append(f"{source}: {err}"),
    )
    return games, warnings


def _enrich_deals_with_itad(sale_games: list, itad_key: str) -> dict:
    """Add ITAD historical low data to a list of sale games.

    Returns a dict mapping game name → {hist_low, hist_low_store, hist_low_date,
    verdict, slug} for use in the deals table.

    The verdict field is one of:
      "all_time_low"   — current sale price ≤ historical low
      "near_low"       — within 10% of historical low
      "below_regular"  — good deal but historical low was cheaper
      "no_data"        — ITAD has no history for this game
    """
    from game_recommender.itad import batch_lookup_game_ids, get_overview
    from datetime import datetime

    if not sale_games:
        return {}

    # Build (title, app_id) pairs — use app_id only for Steam games
    game_infos = [
        (g.name, g.app_id if g.store == "Steam" else None)
        for g in sale_games
    ]

    # Step 1: Parallel ITAD ID lookup (cache makes subsequent calls instant)
    id_map = batch_lookup_game_ids(game_infos, itad_key, max_workers=8)

    name_to_id = {name: gid for name, gid in id_map.items() if gid}
    if not name_to_id:
        return {}

    # Step 2: Batch overview for all found IDs
    all_ids = list(name_to_id.values())
    overview = get_overview(all_ids, itad_key)
    if not overview:
        return {}

    # Build reverse map: itad_id → game_name
    id_to_name = {v: k for k, v in name_to_id.items()}

    # Build sale_price lookup: game_name → current sale price (as float)
    sale_price_map: dict[str, float] = {}
    for g in sale_games:
        if g.sale_price_cents:
            sale_price_map[g.name] = g.sale_price_cents / 100.0

    result: dict = {}
    for gid, item in overview.items():
        name = id_to_name.get(gid)
        if not name:
            continue

        lowest = item.get("lowest") or {}
        slug = item.get("slug", "")

        if not lowest:
            result[name] = {"verdict": "no_data", "slug": slug}
            continue

        hist_price = (lowest.get("price") or {}).get("amount")
        hist_store = (lowest.get("shop") or {}).get("name", "")
        hist_ts = lowest.get("timestamp")
        hist_date = None
        if hist_ts:
            try:
                hist_date = datetime.fromtimestamp(hist_ts).strftime("%b %Y")
            except Exception:
                pass

        current_sale = sale_price_map.get(name)

        # Determine verdict
        if hist_price is not None and current_sale is not None:
            if current_sale <= hist_price * 1.01:  # within 1% (rounding)
                verdict = "all_time_low"
            elif current_sale <= hist_price * 1.10:  # within 10%
                verdict = "near_low"
            else:
                verdict = "below_regular"
        else:
            verdict = "no_data"

        result[name] = {
            "hist_low":       hist_price,
            "hist_low_store": hist_store,
            "hist_low_date":  hist_date,
            "verdict":        verdict,
            "slug":           slug,
        }

    return result


# ── Auth: Epic token restore ───────────────────────────────────────────────────

@app.post("/api/auth/epic/restore")
async def restore_epic(
    body: dict,
    response: Response,
    session_id: str | None = Cookie(default=None),
):
    """Restore Epic session from tokens stored in browser localStorage.

    Does not validate the tokens — the next library fetch will do that.
    If the access_token is expired, the fetch helper will use refresh_token.
    """
    access_token = (body.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(400, "access_token required")
    sid, session = _get_session(session_id)
    session["epic"] = {
        "access_token":  access_token,
        "refresh_token": body.get("refresh_token"),
        "account_id":    body.get("account_id"),
    }
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, sid)
    return resp


@app.post("/api/auth/gog/restore")
async def restore_gog(
    body: dict,
    response: Response,
    session_id: str | None = Cookie(default=None),
):
    """Restore GOG session from tokens stored in browser localStorage."""
    access_token = (body.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(400, "access_token required")
    sid, session = _get_session(session_id)
    session["gog"] = {
        "access_token":  access_token,
        "refresh_token": body.get("refresh_token"),
        "user_id":       body.get("user_id"),
    }
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, sid)
    return resp


# ── ITAD (IsThereAnyDeal) endpoints ──────────────────────────────────────────

@app.get("/api/itad/lookup")
async def itad_lookup(title: str, app_id: str | None = None):
    """Look up a game's ITAD UUID by title or Steam app ID."""
    from game_recommender.itad import lookup_game_id
    game_id = await asyncio.to_thread(lookup_game_id, title, app_id)
    if not game_id:
        return {"found": False}
    return {"found": True, "id": game_id}


@app.get("/api/itad/history")
async def itad_history(game_id: str):
    """Get full price history for a single game (for chart rendering)."""
    from game_recommender.itad import get_price_history
    data = await asyncio.to_thread(get_price_history, game_id)
    return {"history": data}


@app.post("/api/itad/games")
async def itad_games(request: Request):
    """Look up ITAD historical low data for a list of game names.

    Used by the recommendation UI to show pricing for games the user doesn't own.
    Returns {"available": false} when ITAD_API_KEY is not configured so the
    frontend can show a "set up your key" prompt instead of silently doing nothing.

    Request body:  {"names": ["Game A", "Game B", ...]}
    Response:      {"available": true,  "data": {"Game A": {...}, ...}}
               or  {"available": false}
    """
    itad_key = os.environ.get("ITAD_API_KEY")
    if not itad_key:
        return {"available": False}

    body = await request.json()
    names = [n for n in body.get("names", []) if n and isinstance(n, str)]
    if not names:
        return {"available": True, "data": {}}

    from game_recommender.itad import batch_lookup_game_ids, get_overview
    from datetime import datetime

    game_infos = [(name, None) for name in names]
    id_map = await asyncio.to_thread(batch_lookup_game_ids, game_infos, itad_key)

    name_to_id = {name: gid for name, gid in id_map.items() if gid}
    if not name_to_id:
        return {"available": True, "data": {}}

    overview = await asyncio.to_thread(get_overview, list(name_to_id.values()), itad_key)
    if not overview:
        return {"available": True, "data": {}}

    id_to_name = {v: k for k, v in name_to_id.items()}
    result: dict = {}
    for gid, item in overview.items():
        name = id_to_name.get(gid)
        if not name:
            continue
        lowest   = item.get("lowest") or {}
        slug     = item.get("slug", "")
        if not lowest:
            result[name] = {"slug": slug}
            continue
        hist_price = (lowest.get("price") or {}).get("amount")
        hist_store = (lowest.get("shop")  or {}).get("name", "")
        hist_ts    = lowest.get("timestamp")
        hist_date  = None
        if hist_ts:
            try:
                hist_date = datetime.fromtimestamp(hist_ts).strftime("%b %Y")
            except Exception:
                pass
        result[name] = {
            "hist_low":       hist_price,
            "hist_low_store": hist_store,
            "hist_low_date":  hist_date,
            "slug":           slug,
        }

    return {"available": True, "data": result}


@app.post("/api/itad/overview")
async def itad_overview(request: Request):
    """Get current best price + historical low for a batch of games.

    Request body: {"game_ids": ["uuid1", "uuid2", ...]}
    """
    from game_recommender.itad import get_overview
    body = await request.json()
    game_ids = body.get("game_ids", [])
    if not game_ids:
        return {"prices": {}}
    data = await asyncio.to_thread(get_overview, game_ids)
    return {"prices": data}


# ── Library disk cache ───────────────────────────────────────────────────────

_LIBRARY_CACHE_DIR = Path(os.environ.get("CRIT_CACHE_DIR", Path.home() / ".cache" / "crit"))
_LIBRARY_CACHE_FILE = _LIBRARY_CACHE_DIR / "library_cache.json"


@app.post("/api/library/save")
async def save_library_cache(request: Request):
    """Save the enriched library to disk for instant reload across restarts."""
    body = await request.json()
    games = body.get("games", [])
    try:
        _LIBRARY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _LIBRARY_CACHE_FILE.write_text(json.dumps(games), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"Failed to save library cache: {e}")
    return {"ok": True, "count": len(games)}


@app.get("/api/library/cached")
async def get_library_cache():
    """Load the cached library from disk (if it exists)."""
    if not _LIBRARY_CACHE_FILE.exists():
        return {"games": None}
    try:
        games = json.loads(_LIBRARY_CACHE_FILE.read_text(encoding="utf-8"))
        return {"games": games}
    except Exception:
        return {"games": None}


# ── Recommendation history ───────────────────────────────────────────────────

_HISTORY_FILE = _LIBRARY_CACHE_DIR / "rec_history.json"


@app.post("/api/recommendations/save")
async def save_recommendation(request: Request):
    """Save a recommendation session to the history file."""
    body = await request.json()
    entry = {
        "mode": body.get("mode", "library"),
        "preferences": body.get("preferences", ""),
        "output": body.get("output", ""),
        "timestamp": body.get("timestamp"),
    }
    try:
        _LIBRARY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        history = []
        if _HISTORY_FILE.exists():
            history = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        history.append(entry)
        # Keep last 50 recommendations
        history = history[-50:]
        _HISTORY_FILE.write_text(json.dumps(history, indent=1), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"Failed to save recommendation: {e}")
    return {"ok": True}


@app.get("/api/recommendations/history")
async def get_recommendation_history():
    """Load recommendation history from disk."""
    if not _HISTORY_FILE.exists():
        return {"history": []}
    try:
        history = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        return {"history": history}
    except Exception:
        return {"history": []}


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_library_prompt(games: list[dict], preferences: str, count: int) -> str:
    """Build a prompt asking Claude to pick games from the user's owned library."""
    return _build_prompt(games, preferences, count)


def _build_sales_prompt(
    games: list[dict],
    sale_games: list,
    preferences: str,
    count: int,
) -> str:
    """Build a prompt asking Claude to find the best current Steam deals for this player.

    The prompt includes two sections:
      1. The user's library (as a taste profile — what they've played and liked)
      2. The current deals (the candidate set Claude must choose from)

    Claude is instructed to pick deals that match the library taste profile,
    prioritizing wishlist items since those are pre-selected by the user.
    """
    def _game_line(g: dict) -> str:
        """Format one library game for the taste profile section."""
        line = f"- {g['name']} ({g['platform'].upper()})"
        if g["playtime_minutes"] > 0:
            line += f" | {round(g['playtime_minutes'] / 60, 1)}h played"
        if g.get("genres"):
            line += f" | {', '.join(g['genres'][:3])}"
        return line

    def _sale_line(s) -> str:
        """Format one sale game, including store and flagging wishlist items."""
        tag = "⭐ WISHLIST" if s.from_wishlist else "🛒"
        line = (
            f"{tag}: {s.name} [{s.store}] | {s.discount_percent}% OFF → {s.sale_price}"
            f" (was {s.original_price})"
        )
        if s.genres:
            line += f" | {', '.join(s.genres[:3])}"
        return line

    library_lines = "\n".join(_game_line(g) for g in games)
    sale_lines    = "\n".join(_sale_line(s) for s in sale_games)
    prefs_section = f"\n\n**Player's mood / preferences:** {preferences}" if preferences else ""

    if count > 10:
        format_instructions = (
            "For each, use this compact format:\n\n"
            "**N. Game Name** — *XX% off → $Y.YY* — One sentence on why they should buy it.\n\n"
            "No other commentary. Just the numbered list."
        )
    else:
        format_instructions = (
            "For each recommendation:\n\n"
            "1. **Game Name** — *XX% off → $Y.YY (was $Z.ZZ)*\n"
            "   - **Why it fits:** 2\u20133 sentences connecting it to their library history\n"
            "   - **Genre match:** Which games they've played it's most similar to\n"
            "   - **Deal quality:** Is this a historically good discount or just okay?\n\n"
            "End with a one-sentence verdict on whether this is a great sale or a "
            '"wait for a better deal" situation.'
        )

    return f"""You are a gaming deal advisor. Your job is to identify which current game deals best match a player's taste.

**PLAYER'S LIBRARY (taste profile):**
{library_lines}
{prefs_section}

**CURRENT DEALS (not yet owned, across multiple stores):**
{sale_lines}

From the deals listed above, recommend exactly {count} games this player should buy. Include the store name in each recommendation so the player knows where to get it. Wishlist items are already ones they want \u2014 prioritise them if they match.

{format_instructions}

Only recommend games from the deals list above."""


def _build_new_game_prompt(
    games: list[dict],
    unplayed: list[dict],
    preferences: str,
    count: int,
) -> str:
    """Build a prompt asking Claude to suggest unplayed games from the user's backlog.

    The prompt includes:
      1. Games the user has played (taste profile — shows what they enjoy)
      2. Unplayed games in their library (the candidate set)

    Claude matches unplayed games to the demonstrated taste from the played section.
    """
    def _played_line(g: dict) -> str:
        """Format one played game for the taste profile section."""
        line = f"- {g['name']} | {round(g['playtime_minutes'] / 60, 1)}h"
        if g.get("genres"):
            line += f" | {', '.join(g['genres'][:3])}"
        return line

    def _unplayed_line(g: dict) -> str:
        """Format one unplayed game as a candidate recommendation."""
        mins = g["playtime_minutes"]
        # Show minutes (not hours) for low-playtime games so "5 min" reads naturally
        playtime = f"{mins}min" if mins else "0 min"
        line = f"- {g['name']} ({g['platform'].upper()}) | {playtime}"
        if g.get("rawg_rating"):
            line += f" | RAWG {g['rawg_rating']:.1f}/5"
        if g.get("genres"):
            line += f" | {', '.join(g['genres'][:3])}"
        return line

    # Only games with >60 minutes played are a meaningful taste signal
    played = [g for g in games if g["playtime_minutes"] > 60]
    played_lines   = "\n".join(_played_line(g) for g in played)
    unplayed_lines = "\n".join(_unplayed_line(g) for g in unplayed)
    prefs_section  = f"\n\n**Player's current mood:** {preferences}" if preferences else ""

    if count > 10:
        format_instructions = (
            "For each, use this compact format:\n\n"
            "**N. Game Name** (Platform) \u2014 One sentence on why they should play it.\n\n"
            "No other commentary. Just the numbered list."
        )
    else:
        format_instructions = (
            "For each recommendation:\n\n"
            "1. **Game Name** (Platform)\n"
            "   - **Why start now:** 2\u20133 sentences connecting it to games they already love\n"
            "   - **What to expect:** Tone, pacing, length \u2014 so they can set expectations\n"
            "   - **Best entry point:** Any tip for the first 30 minutes to hook them\n\n"
            "End with a sentence about the hidden gem in the list \u2014 "
            "the one they'd least expect to love but probably will."
        )

    return f"""You are a gaming advisor helping a player explore their backlog.

**GAMES THEY'VE PLAYED (their taste profile):**
{played_lines}
{prefs_section}

**UNPLAYED GAMES IN THEIR LIBRARY:**
{unplayed_lines}

Recommend exactly {count} unplayed games they should try next, chosen specifically because they match the player's demonstrated taste.

{format_instructions}

Only recommend games from the unplayed list above."""


def _build_prompt(games: list[dict], preferences: str, count: int) -> str:
    """Build the default library recommendation prompt.

    Formats all games with playtime and any available rating data,
    then asks Claude for exactly `count` recommendations with a structured
    format for each (Why now / Ratings / Best for / Similar to).
    """
    lines = []
    for g in games:
        line = f"- {g['name']} ({g['platform'].upper()})"
        if g["playtime_minutes"] > 0:
            hours = round(g["playtime_minutes"] / 60, 1)
            line += f" | {hours}h played"
        else:
            line += " | unplayed"
        if g.get("rawg_rating"):
            line += f" | RAWG {g['rawg_rating']:.1f}/5"
        if g.get("metacritic"):
            line += f" | Metacritic {g['metacritic']}/100"
        if g.get("genres"):
            line += f" | {', '.join(g['genres'][:3])}"
        lines.append(line)

    played   = sum(1 for g in games if g["playtime_minutes"] > 0)
    unplayed = len(games) - played
    prefs_section = f"\n\n**Player's mood / preferences:** {preferences}" if preferences else ""

    if count > 10:
        format_instructions = f"""List exactly {count} games. For each, use this compact format:

**N. Game Name** (Platform) — One sentence on why they should play it.

No other commentary. Just the numbered list."""
    else:
        format_instructions = f"""Recommend exactly {count} games from their library. For each:

1. **Game Name** (Platform) — *[X hours played / unplayed]*
   - **Why now:** 2-3 sentences tailored to their history
   - **Ratings:** Mention scores with context
   - **Best for:** Session type (quick burst / long session / chill evening / etc.)
   - **Similar to:** Other games in their library they've played

End with a 2-3 sentence insight about patterns in their gaming taste."""

    return f"""You are a knowledgeable gaming advisor helping a player decide what to play next from their existing library.

Here is the player's game library with playtime and ratings:

{chr(10).join(lines)}

**Library stats:** {len(games)} total | {played} played | {unplayed} unplayed{prefs_section}

{format_instructions}

Be specific and grounded in their actual library. Only recommend games listed above."""


def _build_discover_prompt(games: list[dict], preferences: str, count: int) -> str:
    """Build a prompt asking Claude to recommend any games the player doesn't own.

    The player's library is used purely as a taste profile — Claude is free to
    recommend anything in its knowledge base, with no constraint to owned games.
    """
    lines = []
    for g in games:
        line = f"- {g['name']} ({g['platform'].upper()})"
        if g["playtime_minutes"] > 0:
            line += f" | {round(g['playtime_minutes'] / 60, 1)}h played"
        if g.get("genres"):
            line += f" | {', '.join(g['genres'][:3])}"
        lines.append(line)

    owned_names = "\n".join(f"- {g['name']}" for g in games)
    prefs_section = f"\n\n**Player's mood / preferences:** {preferences}" if preferences else ""

    if count > 10:
        format_instructions = f"""List exactly {count} games. For each, use this compact format:

**N. [Game Name](https://store.steampowered.com/search/?term=Game+Name)** (Platform) — One sentence on why it suits this player.

Replace "Game Name" and "Game+Name" with the actual title. Use the Steam search link for Steam games; for Epic-exclusive games use https://store.epicgames.com/browse?q=Game+Name; for GOG-only games use https://www.gog.com/en/games?search=Game+Name.

No other commentary. Just the numbered list."""
    else:
        format_instructions = f"""Recommend exactly {count} games. For each:

1. **[Game Name](https://store.steampowered.com/search/?term=Game+Name)** (Platform, Release Year)
   - **Why it fits:** 2–3 sentences connecting it to games they already love
   - **What makes it special:** The one thing that makes it stand out
   - **Where to get it:** Steam / Epic / GOG / console — and roughly what it costs

Replace "Game Name" and "Game+Name" in each link with the actual title. For Epic-exclusive games use https://store.epicgames.com/browse?q=Game+Name; for GOG-only games use https://www.gog.com/en/games?search=Game+Name.

End with a one-sentence note on the common thread running through your picks."""

    return f"""You are a gaming advisor with encyclopedic knowledge of games across all platforms and eras.

**PLAYER'S TASTE PROFILE (games they already own — DO NOT recommend any of these):**
{chr(10).join(lines)}
{prefs_section}

**COMPLETE LIST OF OWNED GAMES (every title below is already owned — never recommend these):**
{owned_names}

Based on this player's demonstrated taste, recommend exactly {count} games they do not own. Draw on your full knowledge of games across Steam, Epic, GOG, consoles, and any platform. Every game you recommend must be absent from the owned list above.

{format_instructions}"""
