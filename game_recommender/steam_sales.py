"""
Game sale data fetchers.

Steam sources (no extra API key beyond what's configured):
1. Featured specials — Steam's front-page highlighted deals (always available)
2. Wishlist on sale  — user's wishlisted games that are currently discounted
                       (requires the Steam API key + user ID already set up)

Non-Steam sources live in other_sales.py. The SaleGame dataclass is defined
here so both modules share the same type.
"""

import time
import requests
from dataclasses import dataclass, field
from typing import Optional, Callable


@dataclass
class SaleGame:
    """A game that is currently on sale on Steam.

    Prices are stored in US cents (integers) to avoid floating-point rounding
    issues. The formatted string properties convert on demand.
    """

    name: str
    app_id: str
    discount_percent: int        # Discount percentage 0–100
    original_price_cents: int    # Original price in US cents (0 = unknown or free)
    sale_price_cents: int        # Current sale price in US cents
    store: str = "Steam"         # Which store this deal is from
    from_wishlist: bool = False  # True if this game was found in the user's Steam wishlist
    genres: list[str] = field(default_factory=list)  # e.g. ["Action", "RPG"]

    @property
    def original_price(self) -> str:
        """Human-readable original price, e.g. '$19.99'. Returns 'unknown' if price is 0."""
        # 0 cents is falsy in Python; we treat it as "unknown" rather than "$0.00"
        # because Steam sometimes omits the price for free games in the featured API
        return f"${self.original_price_cents / 100:.2f}" if self.original_price_cents else "unknown"

    @property
    def sale_price(self) -> str:
        """Human-readable sale price, e.g. '$9.99'. Returns 'free' if price is 0."""
        # A final_price of 0 genuinely means "free during this promotion"
        return f"${self.sale_price_cents / 100:.2f}" if self.sale_price_cents else "free"


# ── Source 1: Steam featured specials ─────────────────────────────────────────

def get_featured_specials(min_discount: int = 40) -> list[SaleGame]:
    """Fetch games currently highlighted as specials on Steam's store front page.

    Uses the undocumented featuredcategories endpoint that powers Steam's
    front page. No API key or login is required. Returns the 10–30 games that
    Steam is actively promoting as discounted picks.

    Args:
        min_discount: Minimum discount percentage to include (0–100).
                      Default 40% keeps the list focused on meaningful deals.

    Returns:
        List of SaleGame objects sorted by discount percentage descending.

    Raises:
        requests.HTTPError: If the Steam store returns an error (e.g. 503 during maintenance).
    """
    resp = requests.get(
        "https://store.steampowered.com/api/featuredcategories/",
        params={"cc": "us", "l": "en"},   # cc=us ensures USD prices
        headers={"Accept-Language": "en-US,en;q=0.9"},
        timeout=10,
    )
    resp.raise_for_status()

    games = []
    # The "specials" key contains the featured deals section of the Steam front page
    for item in resp.json().get("specials", {}).get("items", []):
        discount = item.get("discount_percent", 0)
        if discount < min_discount:
            continue  # Skip items below the threshold

        # Items without an id are promotions or banners, not purchasable games
        app_id = str(item.get("id", ""))
        if not app_id:
            continue

        games.append(SaleGame(
            name=item.get("name", ""),
            app_id=app_id,
            discount_percent=discount,
            original_price_cents=item.get("original_price", 0),  # Field name in featured API
            sale_price_cents=item.get("final_price", 0),          # Field name in featured API
            # genres not available from the featured endpoint; stays as empty list
        ))

    # Highest discount first so the best deals appear at the top of the LLM prompt
    return sorted(games, key=lambda g: g.discount_percent, reverse=True)


# ── Source 2: User's wishlist on sale ─────────────────────────────────────────

def get_wishlist_on_sale(
    api_key: str,
    user_id: str,
    min_discount: int = 20,
    max_check: int = 300,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[SaleGame]:
    """Fetch the user's Steam wishlist and return items that are currently on sale.

    Strategy:
      1. Fetch the full wishlist via IWishlistService/GetWishlist (requires the
         API key and user ID already stored in the session).
      2. Sort by date_added descending so the most recently wishlisted games
         (which the user is most likely actively interested in) are checked first.
      3. Cap at max_check items to bound the number of HTTP calls.
      4. Batch the app IDs into groups of 20 and call store.steampowered.com/api/appdetails
         for each batch, sleeping 0.4 seconds between batches to avoid rate-limiting.
      5. Collect items whose current discount meets or exceeds min_discount.

    appdetails accepts comma-separated appids (undocumented but stable behavior
    used by many third-party Steam tools). This reduces network round-trips by
    20× compared to fetching each game individually.

    Args:
        api_key: Steam Web API key (from the session, already validated).
        user_id: Steam 64-bit user ID.
        min_discount: Minimum discount percentage to include (typically lower than
                      the featured threshold because wishlist games are pre-selected).
        max_check: Maximum number of wishlist items to check. Bounds execution time.
        progress_callback: Optional callable(checked: int, total: int) for UI updates.

    Returns:
        List of SaleGame objects (all with from_wishlist=True) sorted by discount descending.

    Raises:
        requests.HTTPError: If the wishlist fetch itself fails (batch failures are non-fatal).
    """
    # Step 1: Fetch the raw wishlist
    resp = requests.get(
        "https://api.steampowered.com/IWishlistService/GetWishlist/v1/",
        params={"key": api_key, "steamid": user_id, "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()  # Propagate auth errors immediately (non-recoverable)
    items = resp.json().get("response", {}).get("items", [])
    if not items:
        return []  # Empty wishlist — nothing to check

    # Step 2: Sort by recency and cap at max_check
    # date_added is a Unix timestamp; higher = more recently wishlisted.
    # We check recent items first because the user is likely more interested in them.
    items.sort(key=lambda x: x.get("date_added", 0), reverse=True)
    app_ids = [str(item["appid"]) for item in items[:max_check]]

    # Step 3: Check prices in batches of 20
    # Steam's appdetails API accepts up to ~20 app IDs per call before responses
    # become unreliable. We process in strict batches and sleep between them.
    on_sale: list[SaleGame] = []
    batch_size = 20

    for batch_start in range(0, len(app_ids), batch_size):
        batch = app_ids[batch_start : batch_start + batch_size]

        # Notify the caller before each batch so the UI can show progress
        if progress_callback:
            progress_callback(batch_start, len(app_ids))

        try:
            resp = requests.get(
                "https://store.steampowered.com/api/appdetails",
                params={
                    "appids":   ",".join(batch),              # Comma-separated list
                    "filters":  "basic,price_overview,genres",  # Only fetch what we need
                    "cc": "us",
                    "l":  "en",
                },
                timeout=12,  # Slightly longer timeout for batch requests
            )
            resp.raise_for_status()
            data = resp.json()

            for app_id in batch:
                result = data.get(app_id, {})
                if not result.get("success"):
                    continue  # Game may be unreleased, removed, or region-locked

                info  = result["data"]
                price = info.get("price_overview")
                if not price:
                    continue  # Game is free-to-play or has no price data

                discount = price.get("discount_percent", 0)
                if discount < min_discount:
                    continue  # On sale but below our threshold

                on_sale.append(SaleGame(
                    name=info.get("name", f"App {app_id}"),
                    app_id=app_id,
                    discount_percent=discount,
                    original_price_cents=price.get("initial", 0),  # Field name in appdetails API
                    sale_price_cents=price.get("final", 0),         # Field name in appdetails API
                    from_wishlist=True,  # Always True for items from this function
                    genres=[g["description"] for g in info.get("genres", [])],
                ))
        except Exception:
            # A failed batch is non-fatal — we skip it and continue checking
            # the next batch. Steam occasionally rate-limits or returns errors
            # for specific batches; partial results are better than no results.
            pass

        # Polite delay between batches — skip for the last batch
        if batch_start + batch_size < len(app_ids):
            time.sleep(0.4)

    # Final progress callback to signal completion
    if progress_callback:
        progress_callback(len(app_ids), len(app_ids))

    # Highest discount first, matching the ordering of get_featured_specials
    return sorted(on_sale, key=lambda g: g.discount_percent, reverse=True)


# ── Combined helper ────────────────────────────────────────────────────────────

_ALL_SOURCES = {"steam_featured", "steam_wishlist", "gog", "humble", "fanatical", "gmg", "epic_deals", "epic_free"}


def get_all_sales(
    min_discount: int = 40,
    steam_api_key: Optional[str] = None,
    steam_user_id: Optional[str] = None,
    sources: Optional[set[str]] = None,
    owned_app_ids: Optional[set[str]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> list[SaleGame]:
    """Fetch and merge game deals from all requested sources.

    Ordering: wishlist items first (personally selected), then Steam featured,
    then third-party stores, then Epic free games. Within each group items are
    sorted by discount descending. Duplicates are removed, keeping the first
    (highest-priority) occurrence. Owned Steam games are excluded.

    Args:
        min_discount: Minimum discount percentage for Steam and CheapShark sources.
        steam_api_key / steam_user_id: Required for the wishlist source.
        sources: Which sources to query. Defaults to all available sources.
                 Valid values: "steam_featured", "steam_wishlist", "gog",
                 "humble", "fanatical", "gmg", "epic_deals", "epic_free".
        owned_app_ids: Steam app IDs the user already owns; excluded from results.
        progress_callback: Optional callable(status_message: str) for UI status.

    Returns:
        Merged, deduplicated list sorted by source priority then discount.
    """
    from game_recommender.other_sales import get_cheapshark_deals, get_epic_free_games

    if sources is None:
        sources = _ALL_SOURCES
    owned = owned_app_ids or set()

    all_games: list[SaleGame] = []

    # ── Steam wishlist (most personally relevant → first) ────────────────────
    if "steam_wishlist" in sources and steam_api_key and steam_user_id:
        if progress_callback:
            progress_callback("Checking your Steam wishlist for discounts…")
        try:
            all_games += [
                g for g in get_wishlist_on_sale(steam_api_key, steam_user_id, min_discount)
                if g.app_id not in owned
            ]
        except Exception:
            pass

    # ── Steam featured specials ───────────────────────────────────────────────
    if "steam_featured" in sources:
        if progress_callback:
            progress_callback("Fetching Steam featured deals…")
        try:
            all_games += [g for g in get_featured_specials(min_discount) if g.app_id not in owned]
        except Exception:
            pass

    # ── CheapShark stores (GOG, Humble, Fanatical, GMG, Epic deals) ──────────
    _cs_map = {"gog": "7", "humble": "11", "fanatical": "13", "gmg": "35", "epic_deals": "25"}
    cs_store_ids = [_cs_map[s] for s in sources if s in _cs_map]
    if cs_store_ids:
        if progress_callback:
            progress_callback("Fetching deals from other stores…")
        try:
            all_games += get_cheapshark_deals(cs_store_ids, min_discount)
        except Exception:
            pass

    # ── Epic free games ───────────────────────────────────────────────────────
    if "epic_free" in sources:
        if progress_callback:
            progress_callback("Fetching Epic free games…")
        try:
            all_games += get_epic_free_games()
        except Exception:
            pass

    # Deduplicate by (store, app_id) — first occurrence wins
    seen: set[tuple[str, str]] = set()
    merged: list[SaleGame] = []
    for game in all_games:
        key = (game.store, game.app_id)
        if key not in seen:
            seen.add(key)
            merged.append(game)

    return merged
