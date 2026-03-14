"""Shared data models for the game recommendation system.

These dataclasses are the core data types passed between every module.
Keeping them here (rather than inline in each module) avoids circular imports
and gives a single place to evolve the schema.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Game:
    """Represents a single game in a user's library.

    This is the lowest-level object — it holds only what the platform API
    tells us directly, without any enrichment from third-party sources.
    """

    name: str                       # Display title of the game
    platform: str                   # Source platform: "steam", "epic", "gog", or "other"
    app_id: Optional[str] = None    # Platform-specific numeric/string identifier
    playtime_minutes: int = 0       # Total minutes played (0 = unplayed or unknown)
    last_played: Optional[str] = None  # Unix timestamp string from Steam; None for Epic/GOG

    @property
    def playtime_hours(self) -> float:
        """Convert playtime_minutes to hours, rounded to one decimal place.

        Computed on the fly so the stored value (minutes) stays an integer
        and we don't need to keep two fields in sync.
        """
        return round(self.playtime_minutes / 60, 1)


@dataclass
class GameRating:
    """Ratings and metadata for a game, sourced from the RAWG database.

    RAWG (rawg.io) aggregates crowd-sourced ratings, Metacritic scores,
    genres, and tags. All fields are Optional because search results may
    be incomplete or the game may not exist in RAWG at all.
    """

    name: str                                     # Title as returned by RAWG (may differ slightly from library name)
    rawg_rating: Optional[float] = None           # Community score 0–5 (e.g. 4.67)
    rawg_ratings_count: Optional[int] = None      # Number of RAWG ratings that produced the score
    metacritic_score: Optional[int] = None        # Press review aggregate 0–100
    genres: list[str] = field(default_factory=list)  # e.g. ["RPG", "Adventure"]
    tags: list[str] = field(default_factory=list)    # User-generated tags, capped at 10 (see ratings.py)
    released: Optional[str] = None                # ISO date string, e.g. "2015-05-19"
    background_image: Optional[str] = None        # Hero image URL from RAWG CDN


@dataclass
class GameWithRating:
    """A library game bundled with its RAWG ratings data.

    Separating the pairing into its own class (rather than adding rating
    fields directly to Game) keeps the two concerns clean:
      - Game = what the platform API returned
      - GameRating = what RAWG returned
    rating is None when RAWG returned no match or enrichment was skipped.
    """

    game: Game
    rating: Optional[GameRating] = None  # None if not found in RAWG or enrichment was skipped

    # Convenience delegates so callers don't need to reach into .game every time

    @property
    def name(self) -> str:
        """Delegate to the underlying Game's name."""
        return self.game.name

    @property
    def playtime_hours(self) -> float:
        """Delegate to the underlying Game's computed playtime_hours property."""
        return self.game.playtime_hours
