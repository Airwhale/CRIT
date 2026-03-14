"""
Tests for game_recommender/ratings.py — RAWG game ratings.

Covers:
  - get_game_rating: HTTP request, caching, field mapping, edge cases
  - enrich_games: batch enrichment with progress callback

All HTTP calls are mocked. The module-level _SEARCH_CACHE is cleared before
each test by the autouse clear_ratings_cache fixture — without this, a cached
result from one test would affect subsequent tests.
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock, call

import game_recommender.ratings as ratings_module
from game_recommender.ratings import get_game_rating, enrich_games
from game_recommender.models import Game, GameRating


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_ratings_cache():
    """Clear the module-level RAWG search cache before AND after each test.

    autouse=True means this fixture applies to every test in this file without
    needing to declare it as an argument. Clearing before the test prevents
    state from a previous test; clearing after (in the teardown via yield)
    prevents leakage to tests in other modules.
    """
    ratings_module._SEARCH_CACHE.clear()
    yield
    ratings_module._SEARCH_CACHE.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rawg_resp(results: list) -> MagicMock:
    """Build a mock RAWG API response with the given results list."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"results": results}
    return resp


# A realistic RAWG result object for The Witcher 3, used across multiple tests
WITCHER_RESULT = {
    "name": "The Witcher 3: Wild Hunt",
    "rating": 4.67,
    "ratings_count": 5432,
    "metacritic": 93,
    "genres": [{"name": "RPG"}, {"name": "Adventure"}],
    "tags": [{"name": "Open World"}, {"name": "Fantasy"}],
    "released": "2015-05-19",
    "background_image": "https://media.rawg.io/witcher3.jpg",
}


# ── get_game_rating success ───────────────────────────────────────────────────

class TestGetGameRatingSuccess:

    def test_returns_game_rating_with_all_fields(self):
        """All fields from a full RAWG result are mapped correctly to GameRating."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])):
            rating = get_game_rating("The Witcher 3", api_key="rawg_key")

        assert isinstance(rating, GameRating)
        assert rating.name == "The Witcher 3: Wild Hunt"
        assert rating.rawg_rating == pytest.approx(4.67)  # approx handles float precision
        assert rating.rawg_ratings_count == 5432
        assert rating.metacritic_score == 93
        assert "RPG" in rating.genres
        assert "Adventure" in rating.genres
        assert "Open World" in rating.tags
        assert rating.released == "2015-05-19"

    def test_returns_none_when_no_results(self):
        """An empty results list means RAWG found nothing — return None."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([])):
            rating = get_game_rating("Totally Fake Game XYZ", api_key="rawg_key")

        assert rating is None

    def test_result_is_cached_on_second_call(self):
        """Calling get_game_rating twice for the same name should only make one HTTP request."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])) as mock_get:
            get_game_rating("The Witcher 3", api_key="rawg_key")
            get_game_rating("The Witcher 3", api_key="rawg_key")  # second call — should hit cache

        assert mock_get.call_count == 1  # Only one HTTP call despite two function calls

    def test_cache_is_case_insensitive(self):
        """Different capitalizations of the same game name share one cache entry."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])) as mock_get:
            get_game_rating("The Witcher 3", api_key="key")
            get_game_rating("the witcher 3", api_key="key")   # lowercase
            get_game_rating("THE WITCHER 3", api_key="key")   # uppercase

        # All three queries hit the same cache key (lowercased) — only one HTTP call
        assert mock_get.call_count == 1

    def test_none_result_is_also_cached(self):
        """A "not found" result (None) is cached to prevent re-querying unknown games."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([])) as mock_get:
            r1 = get_game_rating("Nonexistent Game", api_key="key")
            r2 = get_game_rating("Nonexistent Game", api_key="key")

        assert r1 is None
        assert r2 is None
        assert mock_get.call_count == 1  # Second call served from cache

    def test_tags_capped_at_10(self):
        """RAWG can return many tags; we cap at 10 to avoid prompt bloat."""
        result = {**WITCHER_RESULT, "tags": [{"name": f"Tag{i}"} for i in range(20)]}
        with patch("game_recommender.ratings.requests.get", return_value=_rawg_resp([result])):
            rating = get_game_rating("Game", api_key="key")

        assert len(rating.tags) == 10  # Capped, not 20

    def test_uses_rawg_api_key_env_var(self, monkeypatch):
        """When no api_key arg is given, the key is read from RAWG_API_KEY env var."""
        monkeypatch.setenv("RAWG_API_KEY", "env_rawg_key")
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])) as mock_get:
            get_game_rating("The Witcher 3")  # no explicit api_key

        # Verify the env var key was passed to the RAWG API request
        params = mock_get.call_args[1]["params"]
        assert params["key"] == "env_rawg_key"


# ── get_game_rating failure ───────────────────────────────────────────────────

class TestGetGameRatingFailure:

    def test_missing_api_key_raises_value_error(self, monkeypatch):
        """No API key in args or env should raise ValueError before any HTTP call."""
        monkeypatch.delenv("RAWG_API_KEY", raising=False)
        with pytest.raises(ValueError, match="RAWG API key"):
            get_game_rating("Some Game", api_key=None)

    def test_http_error_propagates(self):
        """A 429 (rate limited) response should propagate as HTTPError."""
        resp = MagicMock()
        resp.raise_for_status.side_effect = req_lib.HTTPError("429")
        with patch("game_recommender.ratings.requests.get", return_value=resp):
            with pytest.raises(req_lib.HTTPError):
                get_game_rating("Game", api_key="key")

    def test_network_error_propagates(self):
        """A network failure should propagate as ConnectionError."""
        with patch("game_recommender.ratings.requests.get",
                   side_effect=req_lib.ConnectionError("no route")):
            with pytest.raises(req_lib.ConnectionError):
                get_game_rating("Game", api_key="key")


# ── enrich_games ─────────────────────────────────────────────────────────────

class TestEnrichGames:
    """Tests for the batch-enrichment helper that wraps get_game_rating."""

    def _make_games(self, names: list[str]) -> list[Game]:
        """Helper: create a list of minimal Game objects with the given names."""
        return [Game(name=n, platform="steam") for n in names]

    def test_success_returns_enriched_list(self):
        """A matched game is wrapped in GameWithRating with the rating attached."""
        with patch("game_recommender.ratings.get_game_rating",
                   return_value=GameRating(name="Hades", rawg_rating=4.5)):
            result = enrich_games(self._make_games(["Hades"]), api_key="key")

        assert len(result) == 1
        assert result[0].game.name == "Hades"
        assert result[0].rating.rawg_rating == 4.5

    def test_handles_none_rating_gracefully(self):
        """When RAWG finds no match, rating is None — not an error."""
        with patch("game_recommender.ratings.get_game_rating", return_value=None):
            result = enrich_games(self._make_games(["Obscure Game 9999"]), api_key="key")

        assert len(result) == 1
        assert result[0].rating is None

    def test_calls_progress_callback(self):
        """Progress callback is called once per game with (current, total, name)."""
        calls = []
        with patch("game_recommender.ratings.get_game_rating", return_value=None):
            enrich_games(
                self._make_games(["A", "B", "C"]),
                api_key="key",
                progress_callback=lambda cur, tot, name: calls.append((cur, tot, name)),
            )

        # Three games → three calls, 1-indexed, with game names
        assert calls == [(1, 3, "A"), (2, 3, "B"), (3, 3, "C")]

    def test_empty_input_returns_empty_list(self):
        """An empty games list should return an empty enriched list."""
        result = enrich_games([], api_key="key")
        assert result == []

    def test_enriches_multiple_games_in_order(self):
        """Games are enriched in input order and returned in the same order."""
        ratings = {
            "Alpha": GameRating(name="Alpha", rawg_rating=4.0),
            "Beta":  GameRating(name="Beta",  rawg_rating=3.5),
        }
        with patch("game_recommender.ratings.get_game_rating",
                   side_effect=lambda name, **_: ratings.get(name)):
            result = enrich_games(self._make_games(["Alpha", "Beta"]), api_key="key")

        # Order preserved; ratings correctly assigned
        assert result[0].rating.rawg_rating == 4.0
        assert result[1].rating.rawg_rating == 3.5


# ── Corner cases ──────────────────────────────────────────────────────────────

class TestGetGameRatingCornerCases:

    def test_minimal_result_object_returns_rating_with_none_fields(self):
        """A result with only a 'name' key yields a valid GameRating; extras are None/empty."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([{"name": "Sparse Game"}])):
            rating = get_game_rating("Sparse Game", api_key="key")

        assert rating is not None
        assert rating.name == "Sparse Game"
        assert rating.rawg_rating is None       # Missing field → None
        assert rating.metacritic_score is None  # Missing field → None
        assert rating.genres == []              # Missing field → empty list
        assert rating.tags == []               # Missing field → empty list

    def test_result_missing_name_falls_back_to_query_name(self):
        """If the result has no 'name' key, the original query string is used as the name."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([{"rating": 4.0}])):  # no "name" key
            rating = get_game_rating("My Query", api_key="key")

        assert rating.name == "My Query"  # Falls back to the argument passed in

    def test_fewer_than_10_tags_all_returned(self):
        """[:10] slice on a list shorter than 10 returns all elements — no truncation."""
        result = {**WITCHER_RESULT, "tags": [{"name": f"Tag{i}"} for i in range(5)]}
        with patch("game_recommender.ratings.requests.get", return_value=_rawg_resp([result])):
            rating = get_game_rating("Game", api_key="key")

        assert len(rating.tags) == 5  # All 5 returned, not capped

    def test_zero_tags_returns_empty_list(self):
        """A result with an empty tags list should produce an empty list (not None)."""
        result = {**WITCHER_RESULT, "tags": []}
        with patch("game_recommender.ratings.requests.get", return_value=_rawg_resp([result])):
            rating = get_game_rating("Game", api_key="key")

        assert rating.tags == []

    def test_delay_zero_is_valid(self):
        """delay=0 passes time.sleep(0) without error — used in tests to speed things up."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])), \
             patch("game_recommender.ratings.time.sleep") as mock_sleep:
            get_game_rating("The Witcher 3", api_key="key", delay=0)

        mock_sleep.assert_called_once_with(0)  # sleep IS called, just with 0

    def test_empty_genres_list_in_result(self):
        """genres: [] in API result should produce an empty genres list (not None)."""
        result = {**WITCHER_RESULT, "genres": []}
        with patch("game_recommender.ratings.requests.get", return_value=_rawg_resp([result])):
            rating = get_game_rating("Game", api_key="key")

        assert rating.genres == []

    def test_different_game_names_cached_independently(self):
        """Two different game names create two separate cache entries — each requires one HTTP call."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])) as mock_get:
            get_game_rating("Witcher 3", api_key="key")
            get_game_rating("Hades", api_key="key")  # Different name → different cache key

        # Both names required a network call (different cache keys)
        assert mock_get.call_count == 2
