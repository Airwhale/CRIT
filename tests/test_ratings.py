"""
Tests for game_recommender/ratings.py — RAWG game ratings.
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock, call

import game_recommender.ratings as ratings_module
from game_recommender.ratings import get_game_rating, enrich_games
from game_recommender.models import Game, GameRating


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_ratings_cache():
    """Clear the module-level RAWG search cache before each test."""
    ratings_module._SEARCH_CACHE.clear()
    yield
    ratings_module._SEARCH_CACHE.clear()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _rawg_resp(results: list) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"results": results}
    return resp


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
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])):
            rating = get_game_rating("The Witcher 3", api_key="rawg_key")

        assert isinstance(rating, GameRating)
        assert rating.name == "The Witcher 3: Wild Hunt"
        assert rating.rawg_rating == pytest.approx(4.67)
        assert rating.rawg_ratings_count == 5432
        assert rating.metacritic_score == 93
        assert "RPG" in rating.genres
        assert "Adventure" in rating.genres
        assert "Open World" in rating.tags
        assert rating.released == "2015-05-19"

    def test_returns_none_when_no_results(self):
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([])):
            rating = get_game_rating("Totally Fake Game XYZ", api_key="rawg_key")

        assert rating is None

    def test_result_is_cached_on_second_call(self):
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])) as mock_get:
            get_game_rating("The Witcher 3", api_key="rawg_key")
            get_game_rating("The Witcher 3", api_key="rawg_key")

        # HTTP should only be called once; second is served from cache
        assert mock_get.call_count == 1

    def test_cache_is_case_insensitive(self):
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])) as mock_get:
            get_game_rating("The Witcher 3", api_key="key")
            get_game_rating("the witcher 3", api_key="key")   # different case
            get_game_rating("THE WITCHER 3", api_key="key")

        assert mock_get.call_count == 1

    def test_none_result_is_also_cached(self):
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([])) as mock_get:
            r1 = get_game_rating("Nonexistent Game", api_key="key")
            r2 = get_game_rating("Nonexistent Game", api_key="key")

        assert r1 is None
        assert r2 is None
        assert mock_get.call_count == 1

    def test_tags_capped_at_10(self):
        result = {**WITCHER_RESULT, "tags": [{"name": f"Tag{i}"} for i in range(20)]}
        with patch("game_recommender.ratings.requests.get", return_value=_rawg_resp([result])):
            rating = get_game_rating("Game", api_key="key")

        assert len(rating.tags) == 10

    def test_uses_rawg_api_key_env_var(self, monkeypatch):
        monkeypatch.setenv("RAWG_API_KEY", "env_rawg_key")
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])) as mock_get:
            get_game_rating("The Witcher 3")  # no explicit api_key

        params = mock_get.call_args[1]["params"]
        assert params["key"] == "env_rawg_key"


# ── get_game_rating failure ───────────────────────────────────────────────────

class TestGetGameRatingFailure:

    def test_missing_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("RAWG_API_KEY", raising=False)
        with pytest.raises(ValueError, match="RAWG API key"):
            get_game_rating("Some Game", api_key=None)

    def test_http_error_propagates(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = req_lib.HTTPError("429")
        with patch("game_recommender.ratings.requests.get", return_value=resp):
            with pytest.raises(req_lib.HTTPError):
                get_game_rating("Game", api_key="key")

    def test_network_error_propagates(self):
        with patch("game_recommender.ratings.requests.get",
                   side_effect=req_lib.ConnectionError("no route")):
            with pytest.raises(req_lib.ConnectionError):
                get_game_rating("Game", api_key="key")


# ── enrich_games ─────────────────────────────────────────────────────────────

class TestEnrichGames:

    def _make_games(self, names: list[str]) -> list[Game]:
        return [Game(name=n, platform="steam") for n in names]

    def test_success_returns_enriched_list(self):
        with patch("game_recommender.ratings.get_game_rating",
                   return_value=GameRating(name="Hades", rawg_rating=4.5)):
            result = enrich_games(self._make_games(["Hades"]), api_key="key")

        assert len(result) == 1
        assert result[0].game.name == "Hades"
        assert result[0].rating.rawg_rating == 4.5

    def test_handles_none_rating_gracefully(self):
        with patch("game_recommender.ratings.get_game_rating", return_value=None):
            result = enrich_games(self._make_games(["Obscure Game 9999"]), api_key="key")

        assert len(result) == 1
        assert result[0].rating is None

    def test_calls_progress_callback(self):
        calls = []
        with patch("game_recommender.ratings.get_game_rating", return_value=None):
            enrich_games(
                self._make_games(["A", "B", "C"]),
                api_key="key",
                progress_callback=lambda cur, tot, name: calls.append((cur, tot, name)),
            )

        assert calls == [(1, 3, "A"), (2, 3, "B"), (3, 3, "C")]

    def test_empty_input_returns_empty_list(self):
        result = enrich_games([], api_key="key")
        assert result == []

    def test_enriches_multiple_games_in_order(self):
        ratings = {
            "Alpha": GameRating(name="Alpha", rawg_rating=4.0),
            "Beta":  GameRating(name="Beta",  rawg_rating=3.5),
        }
        with patch("game_recommender.ratings.get_game_rating",
                   side_effect=lambda name, **_: ratings.get(name)):
            result = enrich_games(self._make_games(["Alpha", "Beta"]), api_key="key")

        assert result[0].rating.rawg_rating == 4.0
        assert result[1].rating.rawg_rating == 3.5


# ── Corner cases ──────────────────────────────────────────────────────────────

class TestGetGameRatingCornerCases:

    def test_minimal_result_object_returns_rating_with_none_fields(self):
        """A result with only a name key yields a valid GameRating; extras are None/empty."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([{"name": "Sparse Game"}])):
            rating = get_game_rating("Sparse Game", api_key="key")

        assert rating is not None
        assert rating.name == "Sparse Game"
        assert rating.rawg_rating is None
        assert rating.metacritic_score is None
        assert rating.genres == []
        assert rating.tags == []

    def test_result_missing_name_falls_back_to_query_name(self):
        """If the result has no 'name' key, falls back to the original query string."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([{"rating": 4.0}])):
            rating = get_game_rating("My Query", api_key="key")

        assert rating.name == "My Query"

    def test_fewer_than_10_tags_all_returned(self):
        """[:10] slice on a 5-element list returns all 5 — no truncation."""
        result = {**WITCHER_RESULT, "tags": [{"name": f"Tag{i}"} for i in range(5)]}
        with patch("game_recommender.ratings.requests.get", return_value=_rawg_resp([result])):
            rating = get_game_rating("Game", api_key="key")

        assert len(rating.tags) == 5

    def test_zero_tags_returns_empty_list(self):
        result = {**WITCHER_RESULT, "tags": []}
        with patch("game_recommender.ratings.requests.get", return_value=_rawg_resp([result])):
            rating = get_game_rating("Game", api_key="key")

        assert rating.tags == []

    def test_delay_zero_is_valid(self):
        """delay=0 passes time.sleep(0) without error."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])), \
             patch("game_recommender.ratings.time.sleep") as mock_sleep:
            get_game_rating("The Witcher 3", api_key="key", delay=0)

        mock_sleep.assert_called_once_with(0)

    def test_empty_genres_list_in_result(self):
        """genres: [] in API result should produce an empty genres list."""
        result = {**WITCHER_RESULT, "genres": []}
        with patch("game_recommender.ratings.requests.get", return_value=_rawg_resp([result])):
            rating = get_game_rating("Game", api_key="key")

        assert rating.genres == []

    def test_different_game_names_cached_independently(self):
        """Separate cache entries are created for different game names."""
        with patch("game_recommender.ratings.requests.get",
                   return_value=_rawg_resp([WITCHER_RESULT])) as mock_get:
            get_game_rating("Witcher 3", api_key="key")
            get_game_rating("Hades", api_key="key")

        # Both names required a network call (different cache keys)
        assert mock_get.call_count == 2
