"""
RAWG game ratings fetcher.

RAWG is a free game database with ratings, genres, tags, and Metacritic scores.
Get a free API key at: https://rawg.io/apidocs
"""

import os
import time
import requests
from typing import Optional
from .models import GameRating


RAWG_API_BASE = "https://api.rawg.io/api"
_SEARCH_CACHE: dict[str, Optional[GameRating]] = {}


def get_game_rating(
    game_name: str,
    api_key: Optional[str] = None,
    *,
    delay: float = 0.25,
) -> Optional[GameRating]:
    """
    Fetch rating and metadata for a game from RAWG.

    Args:
        game_name: The name of the game to look up.
        api_key: RAWG API key. Falls back to RAWG_API_KEY env var.
        delay: Seconds to wait between requests (be polite to the API).

    Returns:
        GameRating if found, None otherwise.
    """
    cache_key = game_name.lower()
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]

    api_key = api_key or os.environ.get("RAWG_API_KEY")
    if not api_key:
        raise ValueError(
            "RAWG API key is required. Set RAWG_API_KEY in your .env file.\n"
            "Get a free key at: https://rawg.io/apidocs"
        )

    time.sleep(delay)

    response = requests.get(
        f"{RAWG_API_BASE}/games",
        params={"key": api_key, "search": game_name, "page_size": 1},
        timeout=10,
    )
    response.raise_for_status()

    results = response.json().get("results", [])
    if not results:
        _SEARCH_CACHE[cache_key] = None
        return None

    r = results[0]
    rating = GameRating(
        name=r.get("name", game_name),
        rawg_rating=r.get("rating"),           # out of 5
        rawg_ratings_count=r.get("ratings_count"),
        metacritic_score=r.get("metacritic"),  # out of 100
        genres=[g["name"] for g in r.get("genres", [])],
        tags=[t["name"] for t in r.get("tags", [])[:10]],  # top 10 tags
        released=r.get("released"),
        background_image=r.get("background_image"),
    )
    _SEARCH_CACHE[cache_key] = rating
    return rating


def enrich_games(games, api_key: Optional[str] = None, progress_callback=None):
    """
    Fetch ratings for a list of Game objects.

    Args:
        games: Iterable of Game objects.
        api_key: RAWG API key.
        progress_callback: Optional callable(current, total, game_name) for progress.

    Returns:
        List of GameWithRating objects.
    """
    from .models import GameWithRating

    enriched = []
    total = len(games)
    for i, game in enumerate(games):
        if progress_callback:
            progress_callback(i + 1, total, game.name)
        rating = get_game_rating(game.name, api_key=api_key)
        enriched.append(GameWithRating(game=game, rating=rating))
    return enriched
