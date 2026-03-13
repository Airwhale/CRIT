"""Shared data models for the game recommendation system."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Game:
    """Represents a game in a user's library."""
    name: str
    platform: str  # "steam", "epic", "gog", "other"
    app_id: Optional[str] = None
    playtime_minutes: int = 0
    last_played: Optional[str] = None  # ISO date string

    @property
    def playtime_hours(self) -> float:
        return round(self.playtime_minutes / 60, 1)


@dataclass
class GameRating:
    """Ratings data for a game from various sources."""
    name: str
    rawg_rating: Optional[float] = None        # RAWG score out of 5
    rawg_ratings_count: Optional[int] = None
    metacritic_score: Optional[int] = None     # Metacritic out of 100
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    released: Optional[str] = None
    background_image: Optional[str] = None


@dataclass
class GameWithRating:
    """A library game combined with its ratings data."""
    game: Game
    rating: Optional[GameRating] = None

    @property
    def name(self) -> str:
        return self.game.name

    @property
    def playtime_hours(self) -> float:
        return self.game.playtime_hours
