"""
Tests for game_recommender/steam_sales.py — Steam sale data fetching.

Covers:
  - get_featured_specials: featured deals with discount filtering and price formatting
  - get_wishlist_on_sale: wishlist fetching, batch price checking, progress callbacks
  - get_all_sales: merging both sources, deduplication, ownership filtering

All HTTP calls and time.sleep are mocked (to keep tests fast and deterministic).
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resp(json_body: dict) -> MagicMock:
    """Mock a successful HTTP response."""
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = json_body
    return r


def _http_error(code: int = 403) -> MagicMock:
    """Mock a response that raises HTTPError on raise_for_status."""
    r = MagicMock()
    r.raise_for_status.side_effect = req_lib.HTTPError(str(code))
    return r


def _featured_body(items: list) -> dict:
    """Build the Steam featuredcategories API response body."""
    return {"specials": {"items": items}}


def _wishlist_body(items: list) -> dict:
    """Build the Steam IWishlistService/GetWishlist API response body."""
    return {"response": {"items": items}}


def _appdetails_body(app_id: str, discount: int, orig: int, final: int,
                     name: str = "Test Game", genres: list | None = None) -> dict:
    """Build a Steam appdetails API response body for a single app.

    The appdetails endpoint returns a dict keyed by app_id, containing
    "success" and "data" sub-keys with pricing and genre info.
    """
    return {
        app_id: {
            "success": True,
            "data": {
                "name": name,
                "price_overview": {
                    "discount_percent": discount,
                    "initial": orig,    # Original price in cents
                    "final": final,     # Sale price in cents
                },
                "genres": [{"description": g} for g in (genres or [])],
            },
        }
    }


# ── get_featured_specials ─────────────────────────────────────────────────────

class TestGetFeaturedSpecials:

    def test_returns_items_at_or_above_min_discount(self):
        """Items below min_discount are filtered out; items at or above pass through."""
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
        assert "Game C" not in names  # 25% < 50% — filtered out

    def test_sorted_by_discount_descending(self):
        """Results are returned highest-discount first."""
        body = _featured_body([
            {"id": 1, "name": "A", "discount_percent": 30, "original_price": 1000, "final_price": 700},
            {"id": 2, "name": "B", "discount_percent": 80, "original_price": 1000, "final_price": 200},
            {"id": 3, "name": "C", "discount_percent": 50, "original_price": 1000, "final_price": 500},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=10)

        assert [g.discount_percent for g in games] == [80, 50, 30]

    def test_price_properties_formatted_correctly(self):
        """original_price and sale_price properties format cents as '$X.XX'."""
        body = _featured_body([
            {"id": 730, "name": "CS2", "discount_percent": 40, "original_price": 4999, "final_price": 2999},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=40)

        assert games[0].original_price == "$49.99"  # 4999 cents
        assert games[0].sale_price == "$29.99"       # 2999 cents

    def test_empty_specials_returns_empty_list(self):
        """Steam returning an empty items list should produce an empty result."""
        body = _featured_body([])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials()

        assert games == []

    def test_all_below_threshold_returns_empty_list(self):
        """When no items meet the discount threshold, an empty list is returned."""
        body = _featured_body([
            {"id": 1, "name": "Cheap", "discount_percent": 10, "original_price": 1000, "final_price": 900},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=50)

        assert games == []

    def test_skips_items_with_no_id(self):
        """Items without an 'id' field are not purchasable games and are skipped."""
        body = _featured_body([
            {"name": "No ID Game", "discount_percent": 60, "original_price": 1000, "final_price": 400},
            {"id": 42, "name": "Has ID", "discount_percent": 60, "original_price": 1000, "final_price": 400},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=50)

        assert len(games) == 1
        assert games[0].name == "Has ID"

    def test_http_error_propagates(self):
        """A 503 from the Steam store propagates as HTTPError."""
        with patch("game_recommender.steam_sales.requests.get", return_value=_http_error(503)):
            with pytest.raises(req_lib.HTTPError):
                get_featured_specials()


# ── get_wishlist_on_sale ──────────────────────────────────────────────────────

class TestGetWishlistOnSale:
    """Tests for wishlist-based sale detection.

    Each test patches requests.get as a side_effect list to simulate
    multiple sequential HTTP calls: first the wishlist fetch, then
    one or more appdetails batch calls.
    """

    def _patch_get(self, *responses):
        """Shorthand for patching requests.get with a sequence of responses."""
        return patch("game_recommender.steam_sales.requests.get", side_effect=list(responses))

    def test_returns_wishlist_items_on_sale(self):
        """A wishlisted game that's on sale appears in the results with from_wishlist=True."""
        wishlist = _resp(_wishlist_body([{"appid": 292030, "date_added": 100}]))
        details = _resp(_appdetails_body("292030", 50, 5999, 2999, "The Witcher 3"))

        with self._patch_get(wishlist, details):
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=20)

        assert len(games) == 1
        g = games[0]
        assert g.name == "The Witcher 3"
        assert g.discount_percent == 50
        assert g.from_wishlist is True  # Must be True for wishlist-sourced items
        assert g.app_id == "292030"

    def test_filters_below_min_discount(self):
        """A game with a discount below min_discount is not included."""
        wishlist = _resp(_wishlist_body([{"appid": 1, "date_added": 100}]))
        details = _resp(_appdetails_body("1", 10, 2000, 1800, "Barely Discounted"))

        with self._patch_get(wishlist, details):
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=20)

        assert games == []  # 10% < 20% min_discount

    def test_empty_wishlist_returns_empty_list(self):
        """An empty wishlist returns immediately with no appdetails calls."""
        wishlist = _resp(_wishlist_body([]))
        with patch("game_recommender.steam_sales.requests.get", return_value=wishlist):
            games = get_wishlist_on_sale(api_key="key", user_id="123")

        assert games == []

    def test_sorted_by_discount_descending(self):
        """Results are sorted highest-discount first (same as featured specials)."""
        wishlist = _resp(_wishlist_body([
            {"appid": 1, "date_added": 200},
            {"appid": 2, "date_added": 100},
        ]))
        details = _resp({
            "1": {"success": True, "data": {
                "name": "Low Deal",   "price_overview": {"discount_percent": 30, "initial": 1000, "final": 700}, "genres": []}},
            "2": {"success": True, "data": {
                "name": "Great Deal", "price_overview": {"discount_percent": 70, "initial": 1000, "final": 300}, "genres": []}},
        })
        with self._patch_get(wishlist, details):
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=20)

        assert games[0].discount_percent == 70  # Best deal first
        assert games[1].discount_percent == 30

    def test_most_recently_wishlisted_checked_first(self):
        """Items are checked in date_added descending order — newest wishlist additions first.

        With max_check=1, only the most recently wishlisted item is checked.
        We verify that the correct app_id (newest) was included in the request.
        """
        wishlist = _resp(_wishlist_body([
            {"appid": 1, "date_added": 1000},  # oldest
            {"appid": 2, "date_added": 9000},  # newest — should be checked
            {"appid": 3, "date_added": 5000},
        ]))
        checked_ids = []

        def fake_get(url, **kwargs):
            if "IWishlistService" in url:
                return wishlist
            # Capture which appids were requested in the batch
            app_ids = kwargs.get("params", {}).get("appids", "").split(",")
            checked_ids.extend(app_ids)
            r = MagicMock()
            r.raise_for_status.return_value = None
            r.json.return_value = {}
            return r

        with patch("game_recommender.steam_sales.requests.get", side_effect=fake_get):
            get_wishlist_on_sale(api_key="key", user_id="123", max_check=1)

        assert "2" in checked_ids     # Newest item (date_added=9000) was checked
        assert "1" not in checked_ids  # Oldest was excluded by max_check=1

    def test_calls_progress_callback(self):
        """Progress callback is called at the start of each batch and at completion."""
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

        # At minimum: one call at start (0, 1) and one at end (1, 1)
        assert len(progress_calls) >= 2
        assert progress_calls[-1] == (1, 1)  # Final call signals completion

    def test_failed_appdetails_batch_is_skipped_non_fatal(self):
        """A failed appdetails HTTP call is caught and the batch is silently skipped.

        This allows partial results when Steam is rate-limiting specific batches.
        """
        wishlist = _resp(_wishlist_body([{"appid": 1, "date_added": 100}]))
        details = MagicMock()
        details.raise_for_status.side_effect = req_lib.HTTPError("429")  # Rate limited

        with self._patch_get(wishlist, details):
            # Should NOT raise — failed batch is silently skipped
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=10)

        assert games == []  # Batch failed → no results, but no exception

    def test_http_error_on_wishlist_fetch_propagates(self):
        """A failure on the initial wishlist GET propagates — it's a fatal error."""
        with patch("game_recommender.steam_sales.requests.get", return_value=_http_error(401)):
            with pytest.raises(req_lib.HTTPError):
                get_wishlist_on_sale(api_key="bad_key", user_id="123")

    def test_genres_extracted_correctly(self):
        """Genre descriptions from appdetails are stored on the SaleGame object."""
        wishlist = _resp(_wishlist_body([{"appid": 1, "date_added": 100}]))
        details = _resp(_appdetails_body("1", 40, 2000, 1200, "RPG Game", genres=["RPG", "Adventure"]))

        with self._patch_get(wishlist, details):
            games = get_wishlist_on_sale(api_key="key", user_id="123", min_discount=20)

        assert "RPG" in games[0].genres
        assert "Adventure" in games[0].genres


# ── get_all_sales ─────────────────────────────────────────────────────────────

class TestGetAllSales:
    """Tests for the combined featured + wishlist sale aggregator."""

    def test_merges_featured_and_wishlist(self):
        """Results from both sources appear in the merged output."""
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
                sources={"steam_featured", "steam_wishlist"},
            )

        assert len(games) == 2

    def test_wishlist_items_appear_before_featured(self):
        """Wishlist items (personally curated by the user) are listed before featured deals."""
        featured = [SaleGame(name="Featured", app_id="F1", discount_percent=60,
                             original_price_cents=5000, sale_price_cents=2000)]
        wishlist = [SaleGame(name="Wishlist", app_id="W1", discount_percent=40,
                             original_price_cents=3000, sale_price_cents=1800, from_wishlist=True)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=wishlist):
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        assert games[0].from_wishlist is True   # Wishlist item is first
        assert games[0].name == "Wishlist"

    def test_deduplicates_by_app_id(self):
        """The same app_id appearing in both sources results in exactly one entry."""
        game = SaleGame(name="Shared Game", app_id="42", discount_percent=50,
                        original_price_cents=4000, sale_price_cents=2000)
        wishlist_game = SaleGame(name="Shared Game", app_id="42", discount_percent=50,
                                 original_price_cents=4000, sale_price_cents=2000, from_wishlist=True)

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=[game]), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[wishlist_game]):
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        assert len(games) == 1  # Deduplicated

    def test_filters_out_owned_games(self):
        """Games whose app_id is in owned_app_ids are excluded from results."""
        featured = [
            SaleGame(name="Owned",   app_id="10", discount_percent=50, original_price_cents=1000, sale_price_cents=500),
            SaleGame(name="Unowned", app_id="20", discount_percent=50, original_price_cents=1000, sale_price_cents=500),
        ]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[]):
            games = get_all_sales(
                steam_api_key="key",
                steam_user_id="uid",
                owned_app_ids={"10"},  # app_id "10" is already owned
            )

        assert len(games) == 1
        assert games[0].name == "Unowned"

    def test_skips_wishlist_when_include_wishlist_false(self):
        """get_wishlist_on_sale is never called when include_wishlist=False."""
        featured = [SaleGame(name="F", app_id="1", discount_percent=50,
                             original_price_cents=1000, sale_price_cents=500)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale") as mock_wish:
            games = get_all_sales(sources={"steam_featured"})

        mock_wish.assert_not_called()
        assert len(games) == 1

    def test_skips_wishlist_when_no_steam_credentials(self):
        """Without steam_api_key or steam_user_id, the wishlist fetch is skipped entirely."""
        featured = [SaleGame(name="F", app_id="1", discount_percent=50,
                             original_price_cents=1000, sale_price_cents=500)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale") as mock_wish:
            # No api_key or user_id — wishlist requires credentials and is skipped silently
            games = get_all_sales(
                steam_api_key=None,
                steam_user_id=None,
                sources={"steam_featured", "steam_wishlist"},
            )

        mock_wish.assert_not_called()  # Credentials are required for wishlist

    def test_handles_featured_failure_gracefully(self):
        """A featured specials exception is caught — falls back to wishlist only (or empty)."""
        with patch("game_recommender.steam_sales.get_featured_specials",
                   side_effect=Exception("Steam down")), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[]):
            # Should not raise — returns empty list when both sources fail
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        assert games == []

    def test_handles_wishlist_failure_gracefully(self):
        """A wishlist fetch exception is caught — featured games still returned."""
        featured = [SaleGame(name="F", app_id="1", discount_percent=50,
                             original_price_cents=1000, sale_price_cents=500)]

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale",
                   side_effect=Exception("wishlist error")):
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        # Featured games are returned even when wishlist fails
        assert len(games) == 1
        assert games[0].name == "F"

    def test_calls_progress_callback(self):
        """progress_callback receives at least one status message (featured deals)."""
        status_msgs = []

        with patch("game_recommender.steam_sales.get_featured_specials", return_value=[]), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[]):
            get_all_sales(
                steam_api_key="key",
                steam_user_id="uid",
                progress_callback=lambda msg: status_msgs.append(msg),
            )

        # At minimum, the "fetching featured deals" status message is sent
        assert any("deal" in m.lower() or "featured" in m.lower() for m in status_msgs)


# ── Corner cases ──────────────────────────────────────────────────────────────

class TestGetFeaturedSpecialsCornerCases:

    def test_discount_exactly_at_threshold_is_included(self):
        """The filter is `discount < min_discount` — equality passes through (inclusive)."""
        body = _featured_body([
            {"id": 1, "name": "Right-at-threshold", "discount_percent": 50,
             "original_price": 2000, "final_price": 1000},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=50)

        assert len(games) == 1
        assert games[0].name == "Right-at-threshold"  # 50 == 50, not < 50 → included

    def test_min_discount_zero_includes_zero_percent_items(self):
        """min_discount=0: `discount < 0` is never true, so all items pass through."""
        body = _featured_body([
            {"id": 1, "name": "Not Discounted", "discount_percent": 0,
             "original_price": 0, "final_price": 0},
            {"id": 2, "name": "Big Deal", "discount_percent": 90,
             "original_price": 5000, "final_price": 500},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=0)

        assert len(games) == 2  # Both included at threshold 0

    def test_original_price_cents_zero_shows_unknown(self):
        """original_price_cents=0 is falsy — the property returns 'unknown' not '$0.00'."""
        body = _featured_body([
            {"id": 1, "name": "Freebie", "discount_percent": 100,
             "original_price": 0, "final_price": 0},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=0)

        assert games[0].original_price == "unknown"

    def test_sale_price_cents_zero_shows_free(self):
        """sale_price_cents=0 means free during this promotion — shows 'free'."""
        body = _featured_body([
            {"id": 1, "name": "Free Weekend", "discount_percent": 100,
             "original_price": 5000, "final_price": 0},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=50)

        assert games[0].sale_price == "free"
        assert games[0].original_price == "$50.00"  # Original price still shown correctly

    def test_app_id_zero_is_kept_not_filtered(self):
        """str(0) == '0' which is truthy — id=0 is treated as a valid app_id."""
        body = _featured_body([
            {"id": 0, "name": "Zero ID", "discount_percent": 60,
             "original_price": 2000, "final_price": 800},
        ])
        with patch("game_recommender.steam_sales.requests.get", return_value=_resp(body)):
            games = get_featured_specials(min_discount=50)

        assert len(games) == 1
        assert games[0].app_id == "0"  # Not filtered — "0" is truthy


class TestGetAllSalesCornerCases:

    def test_dedup_keeps_wishlist_version_when_same_app_in_both(self):
        """Merge order is: wishlist first, then featured.
        First occurrence wins, so when the same app_id appears in both,
        the wishlist version (from_wishlist=True) is preserved.
        """
        wishlist_entry = SaleGame(
            name="Portal 2", app_id="620", discount_percent=75,
            original_price_cents=999, sale_price_cents=249, from_wishlist=True,
        )
        featured_entry = SaleGame(
            name="Portal 2", app_id="620", discount_percent=75,
            original_price_cents=999, sale_price_cents=249, from_wishlist=False,
        )
        with patch("game_recommender.steam_sales.get_featured_specials",
                   return_value=[featured_entry]), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale",
                   return_value=[wishlist_entry]):
            games = get_all_sales(steam_api_key="key", steam_user_id="uid")

        assert len(games) == 1
        assert games[0].from_wishlist is True  # Wishlist version kept, not featured

    def test_owned_ids_set_none_treated_as_empty(self):
        """owned_app_ids=None defaults to empty set — no games are filtered out."""
        featured = [SaleGame(name="X", app_id="1", discount_percent=50,
                             original_price_cents=1000, sale_price_cents=500)]
        with patch("game_recommender.steam_sales.get_featured_specials",
                   return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[]):
            games = get_all_sales(owned_app_ids=None, steam_api_key="k", steam_user_id="u")

        assert len(games) == 1  # Not filtered — None treated as empty set

    def test_all_featured_owned_returns_empty(self):
        """When every featured game is already owned, the result is empty."""
        featured = [
            SaleGame(name="A", app_id="1", discount_percent=50,
                     original_price_cents=1000, sale_price_cents=500),
            SaleGame(name="B", app_id="2", discount_percent=60,
                     original_price_cents=2000, sale_price_cents=800),
        ]
        with patch("game_recommender.steam_sales.get_featured_specials",
                   return_value=featured), \
             patch("game_recommender.steam_sales.get_wishlist_on_sale", return_value=[]):
            games = get_all_sales(owned_app_ids={"1", "2"}, steam_api_key="k", steam_user_id="u")

        assert games == []  # All featured games are owned — nothing to recommend
