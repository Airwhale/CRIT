"""Steam API connector — fetches owned games and playtime.

Steam exposes a public Web API at api.steampowered.com. The key endpoint
used here is IPlayerService/GetOwnedGames, which lists every app in a
user's library along with total playtime (in minutes) and the last-played
Unix timestamp.

Requirements:
  STEAM_API_KEY — a 32-char hex key issued at https://steamcommunity.com/dev/apikey
  STEAM_USER_ID — the user's 64-bit Steam ID (find at https://steamid.io)

The Steam profile must be set to public (Game Details visible) or the
API returns an empty response object rather than a proper error code.
"""

import os
import requests
from typing import Optional
from .models import Game


# Base URL for all Steam Web API calls
STEAM_API_BASE = "https://api.steampowered.com"


def get_steam_library(
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> list[Game]:
    """Fetch the Steam library for a given user.

    Args:
        api_key: Steam Web API key. Falls back to STEAM_API_KEY env var.
        user_id: Steam 64-bit user ID. Falls back to STEAM_USER_ID env var.

    Returns:
        List of Game objects sorted by playtime descending (most-played first).

    Raises:
        ValueError: If the API key or user ID is missing after env-var fallback.
        RuntimeError: If Steam returns a valid 200 response but no games
                      (which usually means the profile is set to private).
        requests.HTTPError: On non-2xx responses (e.g. 401 bad key, 403 forbidden).
        requests.Timeout / requests.ConnectionError: On network failures.
    """
    # Prefer explicitly passed values; fall back to environment variables.
    # This lets the CLI pass values from .env while the web app passes
    # per-session credentials without relying on the environment.
    api_key = api_key or os.environ.get("STEAM_API_KEY")
    user_id = user_id or os.environ.get("STEAM_USER_ID")

    if not api_key:
        raise ValueError(
            "Steam API key is required. Set STEAM_API_KEY in your .env file.\n"
            "Get one at: https://steamcommunity.com/dev/apikey"
        )
    if not user_id:
        raise ValueError(
            "Steam user ID is required. Set STEAM_USER_ID in your .env file.\n"
            "Find yours at: https://steamid.io"
        )

    # IPlayerService/GetOwnedGames returns the user's full library.
    # include_appinfo=True adds the game name; without it we only get appid + playtime.
    # include_played_free_games=True adds F2P games like Dota 2 and TF2.
    url = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v0001/"
    params = {
        "key": api_key,
        "steamid": user_id,
        "include_appinfo": True,
        "include_played_free_games": True,
        "format": "json",
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()  # Raise HTTPError on 4xx/5xx

    data = response.json()

    # Steam wraps its data in a top-level "response" object.
    # When the profile is private, Steam returns 200 OK with {"response": {}}
    # (an empty dict) rather than a proper error code — we detect and handle this.
    raw_games = data.get("response", {}).get("games", [])

    if not raw_games:
        # Distinguish between "truly empty library" and "private profile".
        # A private profile produces {"response": {}} (no "games" key at all).
        # A public but empty library produces {"response": {"games": []}}.
        if "response" in data and not data["response"]:
            raise RuntimeError(
                "No games returned. Your Steam profile may be set to private.\n"
                "Set your game details to public at: "
                "https://steamcommunity.com/my/edit/settings"
            )
        # Legitimate empty library — return an empty list normally.
        return []

    games = []
    for g in raw_games:
        games.append(Game(
            # "name" is present when include_appinfo=True; fall back to "App <id>"
            # if it is somehow missing (e.g. delisted or hidden apps).
            name=g.get("name", f"App {g['appid']}"),
            platform="steam",
            app_id=str(g["appid"]),                   # Always convert to string for consistency
            playtime_minutes=g.get("playtime_forever", 0),  # Total lifetime minutes played
            # rtime_last_played is a Unix timestamp; 0 means never played.
            # We store None for "never played" so callers can check truthiness.
            last_played=(
                str(g["rtime_last_played"]) if g.get("rtime_last_played") else None
            ),
        ))

    # Sort most-played first so the CLI and web UI default to the user's
    # most invested games near the top. The LLM also receives games in this
    # order, giving more context weight to heavily-played titles.
    return sorted(games, key=lambda g: g.playtime_minutes, reverse=True)
