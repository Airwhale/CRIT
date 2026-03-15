"""
Non-Steam game deal fetchers — all use public APIs with no key required.

Sources:
  CheapShark — aggregates deals from GOG, Humble Store, Fanatical,
                GreenManGaming, and the Epic Games Store discount catalog.
  Epic Free  — Epic's rotating weekly free game promotions (always 100% off).
"""

import requests
from game_recommender.steam_sales import SaleGame


# CheapShark store IDs → human-readable names
_CS_STORE_NAMES: dict[str, str] = {
    "7":  "GOG",
    "11": "Humble Store",
    "13": "Fanatical",
    "25": "Epic Games Store",
    "35": "GreenManGaming",
}


def get_cheapshark_deals(
    store_ids: list[str],
    min_discount: int = 40,
    page_size: int = 60,
) -> list[SaleGame]:
    """Fetch top deals from CheapShark for the requested store IDs.

    CheapShark's /deals endpoint accepts a single storeID at a time, so we
    make one call per store and merge the results. Each store returns up to
    page_size deals sorted by savings descending; we then filter locally by
    min_discount and re-sort the merged list.

    Args:
        store_ids: List of CheapShark store ID strings to query.
        min_discount: Minimum discount percentage to include (0–100).
        page_size: Deals to request per store (CheapShark max is 60).

    Returns:
        Merged list of SaleGame objects sorted by discount descending.
    """
    games: list[SaleGame] = []
    for store_id in store_ids:
        store_name = _CS_STORE_NAMES.get(store_id, "Other")
        try:
            resp = requests.get(
                "https://www.cheapshark.com/api/1.0/deals",
                params={
                    "storeID":   store_id,
                    "sortBy":    "Savings",
                    "pageSize":  page_size,
                    "onSale":    1,
                    "lowerPrice": 0,
                },
                timeout=10,
            )
            resp.raise_for_status()
        except Exception:
            continue  # Skip this store if the request fails

        for deal in resp.json():
            savings = float(deal.get("savings", 0))
            if savings < min_discount:
                continue
            normal_cents = round(float(deal.get("normalPrice", 0)) * 100)
            sale_cents   = round(float(deal.get("salePrice",   0)) * 100)
            games.append(SaleGame(
                name=deal.get("title", ""),
                app_id=deal.get("dealID", ""),
                discount_percent=round(savings),
                original_price_cents=normal_cents,
                sale_price_cents=sale_cents,
                store=store_name,
            ))

    return sorted(games, key=lambda g: g.discount_percent, reverse=True)


def get_epic_free_games() -> list[SaleGame]:
    """Fetch games currently free on the Epic Games Store.

    Uses the same backend endpoint Epic's own store page calls. Games with an
    active promotional offer at 0% of original price are included. Epic free
    games always have discount_percent=100 and sale_price_cents=0.

    Returns:
        List of SaleGame objects for each currently free Epic game.
    """
    resp = requests.get(
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions",
        params={"locale": "en-US", "country": "US", "allowCountries": "US"},
        timeout=10,
    )
    resp.raise_for_status()

    elements = (
        resp.json()
        .get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
    )

    games: list[SaleGame] = []
    for item in elements:
        promos = item.get("promotions") or {}
        # promotionalOffers is a list of offer-groups; each group has a nested
        # list of individual offers. A discount of 0% means the game is free.
        offer_groups = promos.get("promotionalOffers", [])
        is_free_now = any(
            offer.get("discountSetting", {}).get("discountPercentage", 100) == 0
            for group in offer_groups
            for offer in group.get("promotionalOffers", [])
        )
        if not is_free_now:
            continue

        price_info = item.get("price", {}).get("totalPrice", {})
        # originalPrice is in the store's minor currency unit (cents for USD)
        original_cents = price_info.get("originalPrice", 0)

        games.append(SaleGame(
            name=item.get("title", ""),
            app_id=item.get("id", ""),
            discount_percent=100,
            original_price_cents=original_cents,
            sale_price_cents=0,
            store="Epic Games Store",
        ))

    return games
