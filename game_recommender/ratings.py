"""
RAWG game ratings fetcher.

RAWG (rawg.io) is a free game database similar to IMDb but for games.
It aggregates community ratings, Metacritic scores, genres, and tags for
tens of thousands of titles across all platforms.

Get a free API key at: https://rawg.io/apidocs
The free tier allows up to 20,000 requests/month — more than enough for
personal use.

Rate limiting: RAWG asks that clients be "polite." We enforce a default
0.25-second delay between requests. Tests pass delay=0 to keep the suite fast.
"""

import os
import time
import requests
from requests.exceptions import HTTPError
from typing import Optional
from .models import GameRating


RAWG_API_BASE = "https://api.rawg.io/api"

# Module-level cache: maps lowercase game name → GameRating (or None if not found).
# Persists for the lifetime of the process, so repeated calls within a session
# (e.g. library reload, then recommendations) skip redundant HTTP round-trips.
# Tests clear this between each test case using the clear_ratings_cache fixture.
_SEARCH_CACHE: dict[str, Optional[GameRating]] = {}


def get_game_rating(
    game_name: str,
    api_key: Optional[str] = None,
    *,
    delay: float = 0.25,
) -> Optional[GameRating]:
    """Fetch rating and metadata for a single game from RAWG.

    The first result from a RAWG fuzzy-search is used. This works well for
    exact or near-exact title matches but may return an unrelated game for
    short, ambiguous names. The LLM prompt includes the RAWG name alongside
    the library name, so any mismatch is usually visible.

    Caching: results are cached by lowercased game name, so "The Witcher 3",
    "the witcher 3", and "THE WITCHER 3" all share one cache entry.
    None results are also cached to avoid re-querying for games RAWG doesn't know.

    Args:
        game_name: The game title to search for.
        api_key: RAWG API key. Falls back to RAWG_API_KEY env var.
        delay: Seconds to sleep before the request (rate-limit courtesy).
               Pass 0 in tests to keep them fast.

    Returns:
        GameRating if a match was found, None otherwise.

    Raises:
        ValueError: If no API key is available after env-var fallback.
        requests.HTTPError: On non-2xx responses (e.g. 429 rate-limited).
        requests.ConnectionError / requests.Timeout: On network failure.
    """
    # Check cache first — avoids both the delay and the HTTP call
    cache_key = game_name.lower()
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]

    # Resolve API key (explicit arg wins over environment variable)
    api_key = api_key or os.environ.get("RAWG_API_KEY")
    if not api_key:
        raise ValueError(
            "RAWG API key is required. Set RAWG_API_KEY in your .env file.\n"
            "Get a free key at: https://rawg.io/apidocs"
        )

    # Polite delay before every real HTTP request.
    # This runs AFTER the cache check so cached hits have no delay.
    time.sleep(delay)

    # Search strategy: try precise (non-fuzzy) first so "Ball X Pit" doesn't
    # incorrectly resolve to "Ball Pit Simulator" or another similar title.
    # Fall back to RAWG's default fuzzy search if the precise pass finds nothing.
    # Each attempt retries up to 3 times on transient 5xx errors.
    for precise in (True, False):
        params: dict = {"key": api_key, "search": game_name, "page_size": 1}
        if precise:
            params["search_precise"] = "true"

        for attempt in range(3):
            response = requests.get(
                f"{RAWG_API_BASE}/games",
                params=params,
                timeout=10,
            )
            if response.status_code < 500:
                break
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, then 2s before the third try

        try:
            response.raise_for_status()
        except HTTPError:
            if response.status_code >= 500:
                # RAWG server error — treat as no rating rather than aborting the batch
                _SEARCH_CACHE[cache_key] = None
                return None
            raise  # Re-raise 4xx errors (bad key, rate limit) so the caller sees them

        results = response.json().get("results", [])
        if results:
            break  # Precise search found something — no need for the fuzzy pass
    if not results:
        # Cache the miss so we don't query the same unknown title again
        _SEARCH_CACHE[cache_key] = None
        return None

    r = results[0]

    # Build the GameRating from the first (best-matching) result
    rating = GameRating(
        # RAWG returns its own canonical name — may include subtitles the
        # library name doesn't have (e.g. "The Witcher 3: Wild Hunt")
        name=r.get("name", game_name),  # Fall back to query name if missing
        rawg_rating=r.get("rating"),             # Community score 0–5
        rawg_ratings_count=r.get("ratings_count"),
        metacritic_score=r.get("metacritic"),    # Press aggregate 0–100
        genres=[g["name"] for g in r.get("genres", [])],
        # Tags are user-generated and can be very numerous; cap at 10 to keep
        # the prompt concise and avoid token waste.
        tags=[t["name"] for t in r.get("tags", [])[:10]],
        released=r.get("released"),              # ISO date string
        background_image=r.get("background_image"),  # CDN hero image URL
    )

    # Cache the successful result for future calls in this process
    _SEARCH_CACHE[cache_key] = rating
    return rating


def enrich_games(games, api_key: Optional[str] = None, progress_callback=None):
    """Fetch RAWG ratings for a list of Game objects.

    Wraps each Game in a GameWithRating, pairing it with its RAWG metadata
    (or None if RAWG returned no match). Called by both the CLI (with a Rich
    progress bar) and the web API (with an SSE status callback).

    Args:
        games: Iterable of Game objects to enrich.
        api_key: RAWG API key (falls back to env var inside get_game_rating).
        progress_callback: Optional callable(current: int, total: int, name: str).
                           Called before each lookup so the caller can update a
                           progress bar or send status events.

    Returns:
        List of GameWithRating objects in the same order as the input.
    """
    # Import here to avoid a circular import: models imports nothing;
    # ratings imports models; this lazy import keeps the dependency graph clean.
    from .models import GameWithRating

    enriched = []
    total = len(games)

    for i, game in enumerate(games):
        # Notify the caller before each lookup, not after, so the UI shows
        # which game is currently being fetched (helpful for slow networks).
        if progress_callback:
            progress_callback(i + 1, total, game.name)

        # get_game_rating handles its own caching and delay internally
        rating = get_game_rating(game.name, api_key=api_key)
        enriched.append(GameWithRating(game=game, rating=rating))

    return enriched
