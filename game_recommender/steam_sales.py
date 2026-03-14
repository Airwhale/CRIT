"""
Steam sale data fetcher.

Two sources, both require no additional API keys beyond what's already configured:

1. Featured specials — Steam's front-page highlighted deals (always available)
2. Wishlist on sale  — user's wishlisted games that are currently discounted
                       (requires the Steam API key + user ID already set up)

The wishlist source is far more valuable: these are games the user explicitly
wants that happen to be on sale right now. We check batches of 20 at a time
via appdetails to stay polite to Steam's servers.
"""

import time
import requests
from dataclasses import dataclass, field
from typing import Optional, Callable


@dataclass
class SaleGame:
    name: str
    app_id: str
    discount_percent: int        # 0–100
    original_price_cents: int    # USD cents (0 = free / unknown)
    sale_price_cents: int        # USD cents
    from_wishlist: bool = False  # True if found in the user's wishlist
    genres: list[str] = field(default_factory=list)

    @property
    def original_price(self) -> str:
        return f"${self.original_price_cents / 100:.2f}" if self.original_price_cents else "unknown"

    @property
    def sale_price(self) -> str:
        return f"${self.sale_price_cents / 100:.2f}" if self.sale_price_cents else "free"


# ── Source 1: Steam featured specials ─────────────────────────────────────────

def get_featured_specials(min_discount: int = 40) -> list[SaleGame]:
    """
    Fetch games currently highlighted as specials on Steam's store front page.
    No API key required. Returns 10–30 featured sale items.
    """
    resp = requests.get(
        "https://store.steampowered.com/api/featuredcategories/",
        params={"cc": "us", "l": "en"},
        headers={"Accept-Language": "en-US,en;q=0.9"},
        timeout=10,
    )
    resp.raise_for_status()

    games = []
    for item in resp.json().get("specials", {}).get("items", []):
        discount = item.get("discount_percent", 0)
        if discount < min_discount:
            continue
        app_id = str(item.get("id", ""))
        if not app_id:
            continue
        games.append(SaleGame(
            name=item.get("name", ""),
            app_id=app_id,
            discount_percent=discount,
            original_price_cents=item.get("original_price", 0),
            sale_price_cents=item.get("final_price", 0),
        ))

    return sorted(games, key=lambda g: g.discount_percent, reverse=True)


# ── Source 2: User's wishlist on sale ─────────────────────────────────────────

def get_wishlist_on_sale(
    api_key: str,
    user_id: str,
    min_discount: int = 20,
    max_check: int = 100,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[SaleGame]:
    """
    Fetch the user's Steam wishlist and return items currently on sale.

    Checks up to max_check items sorted by most-recently-wishlisted first,
    so the items the user added recently (and probably cares most about) are
    checked even when the wishlist is large.

    Args:
        api_key: Steam Web API key (already stored in session).
        user_id: Steam 64-bit user ID.
        min_discount: Minimum discount % to include.
        max_check: Cap on wishlist items to check (rate-limit guard).
        progress_callback: Optional callable(checked, total) for UI progress.

    Returns:
        SaleGame list sorted by discount % descending.
    """
    # Step 1: fetch wishlist
    resp = requests.get(
        "https://api.steampowered.com/IWishlistService/GetWishlist/v1/",
        params={"key": api_key, "steamid": user_id, "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get("response", {}).get("items", [])
    if not items:
        return []

    # Most-recently-wishlisted first, then cap
    items.sort(key=lambda x: x.get("date_added", 0), reverse=True)
    app_ids = [str(item["appid"]) for item in items[:max_check]]

    # Step 2: check prices in batches of 20
    # appdetails accepts comma-separated appids (undocumented but stable)
    on_sale: list[SaleGame] = []
    batch_size = 20

    for batch_start in range(0, len(app_ids), batch_size):
        batch = app_ids[batch_start : batch_start + batch_size]

        if progress_callback:
            progress_callback(batch_start, len(app_ids))

        try:
            resp = requests.get(
                "https://store.steampowered.com/api/appdetails",
                params={
                    "appids": ",".join(batch),
                    "filters": "basic,price_overview,genres",
                    "cc": "us",
                    "l":  "en",
                },
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json()

            for app_id in batch:
                result = data.get(app_id, {})
                if not result.get("success"):
                    continue
                info  = result["data"]
                price = info.get("price_overview")
                if not price:
                    continue
                discount = price.get("discount_percent", 0)
                if discount < min_discount:
                    continue
                on_sale.append(SaleGame(
                    name=info.get("name", f"App {app_id}"),
                    app_id=app_id,
                    discount_percent=discount,
                    original_price_cents=price.get("initial", 0),
                    sale_price_cents=price.get("final", 0),
                    from_wishlist=True,
                    genres=[g["description"] for g in info.get("genres", [])],
                ))
        except Exception:
            pass  # non-fatal — skip the batch, keep going

        if batch_start + batch_size < len(app_ids):
            time.sleep(0.4)  # polite delay between batches

    if progress_callback:
        progress_callback(len(app_ids), len(app_ids))

    return sorted(on_sale, key=lambda g: g.discount_percent, reverse=True)


# ── Combined helper ────────────────────────────────────────────────────────────

def get_all_sales(
    min_discount: int = 40,
    steam_api_key: Optional[str] = None,
    steam_user_id: Optional[str] = None,
    include_wishlist: bool = True,
    owned_app_ids: Optional[set[str]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> list[SaleGame]:
    """
    Fetch and merge featured specials + wishlist sales.

    Deduplicates by app_id and removes any games already owned
    (provided via owned_app_ids).

    Args:
        min_discount: Minimum discount % to include.
        steam_api_key / steam_user_id: Required for wishlist source.
        include_wishlist: Whether to check the wishlist at all.
        owned_app_ids: Set of app_id strings to exclude (already owned).
        progress_callback: Optional callable(status_message) for UI updates.

    Returns:
        Merged, deduplicated list sorted so wishlist hits come first,
        then featured specials, all sorted by discount % within each group.
    """
    owned = owned_app_ids or set()

    if progress_callback:
        progress_callback("Fetching Steam featured deals…")

    featured: list[SaleGame] = []
    try:
        featured = [g for g in get_featured_specials(min_discount) if g.app_id not in owned]
    except Exception:
        pass

    wishlist: list[SaleGame] = []
    if include_wishlist and steam_api_key and steam_user_id:
        if progress_callback:
            progress_callback("Checking your wishlist for discounts…")
        try:
            wishlist = [
                g for g in get_wishlist_on_sale(steam_api_key, steam_user_id, min_discount)
                if g.app_id not in owned
            ]
        except Exception:
            pass

    # Merge: wishlist items first (most relevant), then featured
    # Deduplicate by app_id
    seen: set[str] = set()
    merged: list[SaleGame] = []
    for game in wishlist + featured:
        if game.app_id not in seen:
            seen.add(game.app_id)
            merged.append(game)

    return merged
