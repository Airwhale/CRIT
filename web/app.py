"""
FastAPI web application for the game recommendation system.

Run with:
  uvicorn web.app:app --reload --port 8000
  # Then open http://localhost:8000
"""

import uuid
import json
import asyncio
import os
from pathlib import Path

import anthropic
import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Cookie
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Game Recommender")

# ── Session store ──────────────────────────────────────────────────────────────
# Simple in-memory store: session_id -> {platform: credentials}
# Fine for a single-user local app; restart clears all sessions.
_sessions: dict[str, dict] = {}

def _get_session(session_id: str | None) -> tuple[str, dict]:
    """Get or create a session, returning (session_id, session_data)."""
    if session_id and session_id in _sessions:
        return session_id, _sessions[session_id]
    new_id = str(uuid.uuid4())
    _sessions[new_id] = {}
    return new_id, _sessions[new_id]


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie("session_id", session_id, httponly=True, samesite="lax")


# ── HTML page ──────────────────────────────────────────────────────────────────
TEMPLATES_DIR = Path(__file__).parent / "templates"

@app.get("/", response_class=HTMLResponse)
async def index():
    return (TEMPLATES_DIR / "index.html").read_text()


# ── Auth: Epic OAuth redirect flow ────────────────────────────────────────────
# This is the fully automated path: browser is redirected to Epic, user logs in,
# Epic sends them back to our callback with ?code=XXX, we exchange silently.
# Epic's launcherAppClient2 was designed for desktop launchers that use localhost
# callbacks, so localhost redirect URIs are accepted.

@app.get("/auth/epic/start")
async def epic_auth_start(request: Request, session_id: str | None = Cookie(default=None)):
    """Redirect the browser to Epic's OAuth authorization page."""
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
    """Receive the authorization code from Epic, exchange it, and redirect home."""
    if error or not code:
        reason = error_description or error or "login_cancelled"
        return RedirectResponse(f"/?auth_error={reason}")

    from game_recommender.epic import exchange_code
    try:
        tokens = await asyncio.to_thread(exchange_code, code)
    except Exception as e:
        return RedirectResponse(f"/?auth_error={str(e)[:80]}")

    if "access_token" not in tokens:
        msg = tokens.get("errorMessage", "token_exchange_failed")
        return RedirectResponse(f"/?auth_error={msg[:80]}")

    sid, session = _get_session(session_id)
    session["epic"] = {
        "access_token":  tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "account_id":    tokens.get("account_id"),
    }
    response = RedirectResponse("/?auth_success=epic")
    _set_session_cookie(response, sid)
    return response


# ── Auth: status ───────────────────────────────────────────────────────────────
@app.get("/api/status")
async def get_status(session_id: str | None = Cookie(default=None)):
    _, session = _get_session(session_id)
    return {
        "steam": "steam" in session,
        "epic":  "epic"  in session,
        "gog":   "gog"   in session,
    }


# ── Auth: Steam ────────────────────────────────────────────────────────────────
@app.post("/api/auth/steam")
async def connect_steam(
    request: Request,
    session_id: str | None = Cookie(default=None),
):
    body = await request.json()
    api_key = (body.get("api_key") or "").strip()
    user_id = (body.get("user_id") or "").strip()

    if not api_key or not user_id:
        raise HTTPException(400, "api_key and user_id are required")

    # Quick validation: try fetching the library
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params={
            "key": api_key,
            "steamid": user_id,
            "include_appinfo": True,
            "format": "json",
        })

    if resp.status_code != 200:
        raise HTTPException(400, f"Steam API error: {resp.status_code}")

    data = resp.json()
    if not data.get("response"):
        raise HTTPException(400, "No response from Steam. Is your profile public?")

    sid, session = _get_session(session_id)
    session["steam"] = {"api_key": api_key, "user_id": user_id}

    response = JSONResponse({"ok": True, "game_count": len(data["response"].get("games", []))})
    _set_session_cookie(response, sid)
    return response


# ── Auth: Epic ─────────────────────────────────────────────────────────────────
@app.post("/api/auth/epic")
async def connect_epic(
    request: Request,
    session_id: str | None = Cookie(default=None),
):
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


# ── Auth: GOG ──────────────────────────────────────────────────────────────────
@app.post("/api/auth/gog")
async def connect_gog(
    request: Request,
    session_id: str | None = Cookie(default=None),
):
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


# ── Auth: disconnect ───────────────────────────────────────────────────────────
@app.delete("/api/auth/{platform}")
async def disconnect(platform: str, session_id: str | None = Cookie(default=None)):
    if platform not in ("steam", "epic", "gog"):
        raise HTTPException(404, "Unknown platform")
    _, session = _get_session(session_id)
    session.pop(platform, None)
    return {"ok": True}


# ── Library ────────────────────────────────────────────────────────────────────
@app.get("/api/library")
async def get_library(
    skip_ratings: bool = False,
    session_id: str | None = Cookie(default=None),
):
    _, session = _get_session(session_id)

    if not session:
        raise HTTPException(400, "No platforms connected. Connect at least one platform first.")

    # Gather games from all connected platforms concurrently
    tasks = {}
    if "steam" in session:
        tasks["steam"] = asyncio.to_thread(_fetch_steam, session["steam"])
    if "epic" in session:
        tasks["epic"] = asyncio.to_thread(_fetch_epic, session["epic"])
    if "gog" in session:
        tasks["gog"] = asyncio.to_thread(_fetch_gog, session["gog"])

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    all_games = []
    errors = []
    for platform, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            errors.append({"platform": platform, "error": str(result)})
        else:
            all_games.extend(result)

    # Sort by playtime desc then name
    all_games.sort(key=lambda g: (-g["playtime_minutes"], g["name"].lower()))

    # Optionally enrich with RAWG ratings
    rawg_key = os.environ.get("RAWG_API_KEY")
    if not skip_ratings and rawg_key and all_games:
        # Only enrich top 75 to keep load time reasonable
        to_enrich = all_games[:75]
        try:
            enriched = await asyncio.to_thread(_enrich_with_rawg, to_enrich, rawg_key)
            # Merge back: enriched replaces the first N, rest stay as-is
            ratings_map = {g["name"]: g for g in enriched}
            all_games = [ratings_map.get(g["name"], g) for g in all_games]
        except Exception as e:
            errors.append({"platform": "rawg", "error": str(e)})

    return {"games": all_games, "errors": errors}


def _fetch_steam(creds: dict) -> list[dict]:
    from game_recommender.steam import get_steam_library
    games = get_steam_library(creds["api_key"], creds["user_id"])
    return [_game_to_dict(g) for g in games]


def _fetch_epic(creds: dict) -> list[dict]:
    from game_recommender.epic import get_epic_library, refresh_tokens
    try:
        games = get_epic_library(creds["access_token"])
    except Exception:
        # Try refreshing token
        if creds.get("refresh_token"):
            new_tokens = refresh_tokens(creds["refresh_token"])
            creds["access_token"] = new_tokens["access_token"]
            creds["refresh_token"] = new_tokens.get("refresh_token", creds["refresh_token"])
            games = get_epic_library(creds["access_token"])
        else:
            raise
    return [_game_to_dict(g) for g in games]


def _fetch_gog(creds: dict) -> list[dict]:
    from game_recommender.gog import get_gog_library, refresh_tokens
    try:
        games = get_gog_library(creds["access_token"])
    except Exception:
        if creds.get("refresh_token"):
            new_tokens = refresh_tokens(creds["refresh_token"])
            creds["access_token"] = new_tokens["access_token"]
            creds["refresh_token"] = new_tokens.get("refresh_token", creds["refresh_token"])
            games = get_gog_library(creds["access_token"])
        else:
            raise
    return [_game_to_dict(g) for g in games]


def _game_to_dict(game) -> dict:
    return {
        "name":             game.name,
        "platform":         game.platform,
        "app_id":           game.app_id,
        "playtime_minutes": game.playtime_minutes,
        "rawg_rating":      None,
        "metacritic":       None,
        "genres":           [],
    }


def _enrich_with_rawg(games: list[dict], api_key: str) -> list[dict]:
    from game_recommender.ratings import get_game_rating
    enriched = []
    for game in games:
        rating = get_game_rating(game["name"], api_key=api_key)
        if rating:
            game = {**game,
                "rawg_rating": rating.rawg_rating,
                "metacritic":  rating.metacritic_score,
                "genres":      rating.genres,
            }
        enriched.append(game)
    return enriched


# ── Recommendations (SSE) ──────────────────────────────────────────────────────
@app.get("/api/recommend")
async def recommend(
    preferences: str = "",
    count: int = 5,
    session_id: str | None = Cookie(default=None),
):
    _, session = _get_session(session_id)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise HTTPException(400, "ANTHROPIC_API_KEY not configured on the server")

    # Get library from session cache (client should have called /api/library first)
    # We re-fetch here so the SSE endpoint is self-contained
    if not session:
        raise HTTPException(400, "No platforms connected")

    # Fetch library synchronously (in thread) before streaming starts
    library_resp = await get_library(skip_ratings=False, session_id=session_id)
    games_raw = library_resp["games"]

    if not games_raw:
        raise HTTPException(400, "Library is empty")

    # Build prompt
    prompt = _build_prompt(games_raw, preferences, count)

    async def event_stream():
        client = anthropic.AsyncAnthropic(api_key=anthropic_key)
        try:
            async with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=4096,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    payload = json.dumps({"text": text})
                    yield f"data: {payload}\n\n"
            yield 'data: {"done": true}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"error": str(e)})}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _build_prompt(games: list[dict], preferences: str, count: int) -> str:
    lines = []
    for g in games[:100]:
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

    return f"""You are a knowledgeable gaming advisor helping a player decide what to play next from their existing library.

Here is the player's game library with playtime and ratings:

{chr(10).join(lines)}

**Library stats:** {len(games)} total | {played} played | {unplayed} unplayed{prefs_section}

Recommend exactly {count} games from their library. For each:

1. **Game Name** (Platform) — *[X hours played / unplayed]*
   - **Why now:** 2-3 sentences tailored to their history
   - **Ratings:** Mention scores with context
   - **Best for:** Session type (quick burst / long session / chill evening / etc.)
   - **Similar to:** Other games in their library they've played

End with a 2-3 sentence insight about patterns in their gaming taste.

Be specific, enthusiastic, and grounded in their actual library. Only recommend games listed above."""
