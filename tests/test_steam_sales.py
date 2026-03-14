"""
Tests for game_recommender/steam_sales.py — Steam sale data fetching.
"""

import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock, call

from game_recommender.steam_sales import (
    SaleGame,
    get_featured_specials,
    get_wishlist_on_sale,
    get_all_sales,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resp(json_body: dict) -> MagicMock:
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = json_body
    return r


def _http_error(code: int = 403) -> MagicMock:
    r = MagicMock()
    r.raise_for_status.side_effect = req_lib.HTTPError(str(code))
    return r


def _featured_body(items: list) -> dict:
    return {"specials": {"items": items}}


def _wishlist_body(items: list) -> dict:
    return {"response": {"items": items}}


def _appdetails_body(app_id: str, discount: int, orig: int, final: int,
                     name: str = "Test Game", genres: list | None = None) -> dict:
    return {
        app_id: {
            "success": True,
            "data": {
                "name": name,
                "price_overview": {
                    "discount_percent": discount,
                    "initial": orig,
                    "final": final,
                },
                "genres": [{"description": g} for g in (genres or [])],
            },
        }
    }


# ── get_featured_specials ─────────────────────────────────────────────────────

class TestGetFeaturedSpecials:

    def test_returns_items_at_or_above_min_discount(self):
        body = _featured_body([
            {"id": 1, "name": "Game A", "discount_percent": 75, "original_price": 5999, "final_price": 1499},
            {"id": 2, "name": "Game B", "discount_percent": 50, "original_price": 3999, "final_price": 1999},
            {"id": 3, "name": "Game C", "discount_percent": 25, "original_price": 1999, "final_price": 1499},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=50)

        assert len(games) == 2
        names = {g.name for g in games}
        assert "Game A" in names
        assert "Game B" in names
        assert "Game C" not in names

    def test_sorted_by_discount_descending(self):
        body = _featured_body([
            {"id": 1, "name": "A", "discount_percent": 30, "original_price": 1000, "final_price": 700},
            {"id": 2, "name": "B", "discount_percent": 80, "original_price": 1000, "final_price": 200},
            {"id": 3, "name": "C", "discount_percent": 50, "original_price": 1000, "final_price": 500},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=10)

        assert [g.discount_percent for g in games] == [80, 50, 30]

    def test_price_properties_formatted_correctly(self):
        body = _featured_body([
            {"id": 730, "name": "CS2", "discount_percent": 40, "original_price": 4999, "final_price": 2999},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=40)

        assert games[0].original_price == "$49.99"
        assert games[0].sale_price == "$29.99"

    def test_empty_specials_returns_empty_list(self):
        body = _featured_body([])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials()

        assert games == []

    def test_all_below_threshold_returns_empty_list(self):
        body = _featured_body([
            {"id": 1, "name": "Cheap", "discount_percent": 10, "original_price": 1000, "final_price": 900},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=50)

        assert games == []

    def test_skips_items_with_no_id(self):
        body = _featured_body([
            {"name": "No ID Game", "discount_percent": 60, "original_price": 1000, "final_price": 400},
            {"id": 42, "name": "Has ID", "discount_percent": 60, "original_price": 1000, "final_price": 400},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=50)

        assert len(games) == 1
        assert games[0].name == "Has ID"

    def test_http_error_propagates(self):
        with patch("game_recommender.steam_sales.requests.get", return_value=_http_error(503)):
            with pytest.raises(req_lib.HTTPError):
                get_featured_specials()


# ── get_wishlist_on_sale ──────────────────────────────────────────────────────

class TestGetWishlistOnSale:

    def _patch_get(self, *responses):
        return patch("game_recommender.steam_sales.requests.get", side_effect=list(responses))

    def test_returns_wishlist_items_on_sale(self):
        wishlist = _resp(_wishlist_body([{"appid": 292030, "date_added": 100}]))
        details = _resp(_appdetails_body("292030", 50, 5999, 2999, "The Witcher 3"))

        with self._patch_get(wishlist, details):
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=20)

        assert len(games) == 1
        g = games[0]
        assert g.name == "The Witcher 3"
        assert g.discount_percent == 50
        assert g.from_wishlist is True
        assert g.app_id == "292030"

    def test_filters_below_min_discount(self):
        wishlist = _resp(_wishlist_body([{"appid": 1, "date_added": 100}]))
        details = _resp(_appdetails_body("1", 10, 2000, 1800, "Barely Discounted"))

        with self._patch_get(wishlist, details):
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=20)

        assert games == []

    def test_empty_wishlist_returns_empty_list(self):
        wishlist = _resp(_wishlist_body([]))
        with patch("game_recommender.steam_sales.requests.get", return_value=wishlist):
            games = get_wishlist_on_sale(api_key="key", user_id="123")

        assert games == []

    def test_sorted_by_discount_descending(self):
        wishlist = _resp(_wishlist_body([
            {"appid": 1, "date_added": 200},
            {"appid": 2, "date_added": 100},
        ]))
        details = _resp({
            "1": {"success": True, "data": {
                "name": "Low Deal", "price_overview": {"discount_percent": 30, "initial": 1000, "final": 700}, "genres": []}},
            "2": {"success": True, "data": {
                "name": "Great Deal", "price_overview": {"discount_percent": 70, "initial": 1000, "final": 300}, "genres": []}},
        })
        with self._patch_get(wishlist, details):
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=20)

        assert games[0].discount_percent == 70
        assert games[1].discount_percent == 30

    def test_most_recently_wishlisted_checked_first(self):
        """Items with higher date_added should be in the batch that gets checked."""
        wishlist = _resp(_wishlist_body([
            {"appid": 1, "date_added": 1000},  # oldest
            {"appid": 2, "date_added": 9000},  # newest
            {"appid": 3, "date_added": 5000},
        ]))
        # max_check=1 means only the most recent item is checked
        checked_ids = []

        def fake_get(url, **kwargs):
            if "IWishlistService" in url:
                return wishlist
            # capture which appids were requested
            app_ids = kwargs.get("params", {}).get("appids", "").split(",")
            checked_ids.extend(app_ids)
            r = MagicMock()
            r.raise_for_status.return_value = None
            r.json.return_value = {}
            return r

        with patch("game_recommender.steam_sales.requests.get", side_effect=fake_get):
            get_wishlist_on_sale(api_key="key", user_id="123", max_check=1)

        assert "2" in checked_ids   # newest (date_added=9000) was checked
        assert "1" not in checked_ids  # oldest was skipped (max_check=1)

    def test_calls_progress_callback(self):
        wishlist = _resp(_wishlist_body([{"appid": 1, "date_added": 100}]))
        details = _resp(_appdetails_body("1", 50, 1000, 500))
        progress_calls = []

        with self._patch_get(wishlist, details):
            get_wishlist_on_sale(
                api_key="key",
                user_id="123",
                min_discount=20,
                progress_callback=lambda cur, tot: progress_calls.append((cur, tot)),
            )

        # Should have called with (0, 1) at start and (1, 1) at end
        assert len(progress_calls) >= 2
        assert progress_calls[-1] == (1, 1)

    def test_failed_appdetails_batch_is_skipped_non_fatal(self):
        """A failed appdetails call should be skipped rather than raising."""
        wishlist = _resp(_wishlist_body([{"appid": 1, "date_added": 100}]))
        details = MagicMock()
        details.raise_for_status.side_effect = req_lib.HTTPError("429")

        with self._patch_get(wishlist, details):
            # Should not raise — failed batch is silently skipped
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=10)

        assert games == []

    def test_http_error_on_wishlist_fetch_propagates(self):
        with patch("game_recommender.steam_sales.requests.get", return_value=_http_error(401)):
            with pytest.raises(req_lib.HTTPError):
                get_wishlist_on_sale(api_key="bad_key", user_id="123")

    def test_genres_extracted_correctly(self):
        wishlist = _resp(_wishlist_body([{"appid": 1, "date_added": 100}]))
        details = _resp(_appdetails_body("1", 40, 2000, 1200, "RPG Game", genres=["RPG", "Adventure"]))

        with self._patch_get(wishlist, details):
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=20)

        assert "RPG" in games[0].genres
        assert "Adventure" in games[0].genres


# ── get_all_sales ─────────────────────────────────────────────────────────────

class TestGetAllSales:

    def test_merges_featured_and_wishlist(self):
        featured = [SaleGame(name="Featured Game", app_id="1", discount_percent=60,
                             original_price_cents=5000, sale_price_cents=2000)]
        wishlist = [SaleGame(name="Wishlist Game", app_id="2", discount_percent=40,
                             original_price_cents=3000, sale_price_cents=1800, from_wishlist=True)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=wishlist):
            games = get_all_sales(
                min_discount=30,
                steam_api_key="key",
                steam_user_id="uid",
                include_wishlist=True,
            )

        assert len(games) == 2

    def test_wishlist_items_appear_before_featured(self):
        featured = [SaleGame(name="Featured", app_id="F1", discount_percent=60,
                             original_price_cents=5000, sale_price_cents=2000)]
        wishlist = [SaleGame(name="Wishlist", app_id="W1", discount_percent=40,
                             original_price_cents=3000, sale_price_cents=1800, from_wishlist=True)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=wishlist):
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        assert games[0].from_wishlist is True
        assert games[0].name == "Wishlist"

    def test_deduplicates_by_app_id(self):
        """Same game in both featured and wishlist should only appear once."""
        game = SaleGame(name="Shared Game", app_id="42", discount_percent=50,
                        original_price_cents=4000, sale_price_cents=2000)
        wishlist_game = SaleGame(name="Shared Game", app_id="42", discount_percent=50,
                                 original_price_cents=4000, sale_price_cents=2000, from_wishlist=True)

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=[game]), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[wishlist_game]):
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        assert len(games) == 1

    def test_filters_out_owned_games(self):
        featured = [
            SaleGame(name="Owned",   app_id="10", discount_percent=50, original_price_cents=1000, sale_price_cents=500),
            SaleGame(name="Unowned", app_id="20", discount_percent=50, original_price_cents=1000, sale_price_cents=500),
        ]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[]):
            games = get_all_sales(
                steam_api_key="key",
                steam_user_id="uid",
                owned_app_ids={"10"},
            )

        assert len(games) == 1
        assert games[0].name == "Unowned"

    def test_skips_wishlist_when_include_wishlist_false(self):
        featured = [SaleGame(name="F", app_id="1", discount_percent=50,
                             original_price_cents=1000, sale_price_cents=500)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured) as mock_feat, \
             patch("game_recommender.steam_sales.get_wishlist_on_sale") as mock_wish:
            games = get_all_sales(include_wishlist=False)

        mock_wish.assert_not_called()
        assert len(games) == 1

    def test_skips_wishlist_when_no_steam_credentials(self):
        featured = [SaleGame(name="F", app_id="1", discount_percent=50,
                             original_price_cents=1000, sale_price_cents=500)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale") as mock_wish:
            # No api_key or user_id provided
            games = get_all_sales(steam_api_key=None, steam_user_id=None, include_wishlist=True)

        mock_wish.assert_not_called()

    def test_handles_featured_failure_gracefully(self):
        with patch("game_recommender.steam_sales.get_featured_specials",
                   side_effect=Exception("Steam down")), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[]):
            # Should not raise — returns empty list if both sources fail
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        assert games == []

    def test_handles_wishlist_failure_gracefully(self):
        featured = [SaleGame(name="F", app_id="1", discount_percent=50,
                             original_price_cents=1000, sale_price_cents=500)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale",
                   side_effect=Exception("wishlist error")):
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        # Featured games still returned even when wishlist fails
        assert len(games) == 1
        assert games[0].name == "F"

    def test_calls_progress_callback(self):
        status_msgs = []

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=[]), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[]):
            get_all_sales(
                steam_api_key="key",
                steam_user_id="uid",
                progress_callback=lambda msg: status_msgs.append(msg),
            )

        assert any("deal" in m.lower() or "featured" in m.lower() for m in status_msgs)
