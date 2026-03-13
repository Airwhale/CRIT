"""Steam API connector — fetches owned games and playtime."""

import os
import requests
from typing import Optional
from .models import Game


STEAM_API_BASE = "https://api.steampowered.com"


def get_steam_library(
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> list[Game]:
    """
    Fetch the Steam library for a given user.

    Args:
        api_key: Steam Web API key. Falls back to STEAM_API_KEY env var.
        user_id: Steam 64-bit user ID. Falls back to STEAM_USER_ID env var.

    Returns:
        List of Game objects from the user's Steam library.

    Raises:
        ValueError: If API key or user ID is missing.
        RuntimeError: If the Steam API returns an error.
    """
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

    url = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v0001/"
    params = {
        "key": api_key,
        "steamid": user_id,
        "include_appinfo": True,
        "include_played_free_games": True,
        "format": "json",
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()
    raw_games = data.get("response", {}).get("games", [])

    if not raw_games:
        # Profile may be private
        if "response" in data and not data["response"]:
            raise RuntimeError(
                "No games returned. Your Steam profile may be set to private.\n"
                "Set your game details to public at: "
                "https://steamcommunity.com/my/edit/settings"
            )
        return []

    games = []
    for g in raw_games:
        games.append(Game(
            name=g.get("name", f"App {g['appid']}"),
            platform="steam",
            app_id=str(g["appid"]),
            playtime_minutes=g.get("playtime_forever", 0),
            last_played=(
                str(g["rtime_last_played"]) if g.get("rtime_last_played") else None
            ),
        ))

    return sorted(games, key=lambda g: g.playtime_minutes, reverse=True)
