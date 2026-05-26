"""
Non-Steam game deal fetchers — all use public APIs with no key required.

Sources:
  GOG Catalog — GOG's own storefront API; returns all discounted GOG titles.
  CheapShark  — aggregates deals from Humble Store, Fanatical, GreenManGaming,
                and the Epic Games Store discount catalog.
  Epic Free   — Epic's rotating weekly free game promotions (always 100% off).
"""

import requests
import time
from game_recommender.steam_sales import SaleGame


# CheapShark store IDs → human-readable names (GOG is queried directly now)
_CS_STORE_NAMES: dict[str, str] = {
    "11": "Humble Store",
    "13": "Fanatical",
    "25": "Epic Games Store",
    "35": "GreenManGaming",
}

_GOG_CATALOG_URL = "https://catalog.gog.com/v1/catalog"
_GOG_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; game-recommender/1.0)"}
_CS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; game-recommender/1.0)"}


def get_gog_catalog_deals(min_discount: int = 40, page_size: int = 48) -> list[SaleGame]:
    """Fetch discounted games directly from GOG's storefront catalog API.

    Queries GOG's own catalog endpoint (the same one powering the GOG store
    page) rather than going through a third-party aggregator. This returns
    the complete set of discounted GOG titles, not just the subset indexed
    by CheapShark.

    The API is paginated (max 48 per page). We keep fetching until a page
    comes back with fewer than page_size results.

    Args:
        min_discount: Minimum discount percentage to include (0–100).
        page_size: Results per page; 48 is the API's documented maximum.

    Returns:
        List of SaleGame objects sorted by discount percentage descending.
    """
    games: list[SaleGame] = []
    page = 1

    while True:
        try:
            resp = requests.get(
                _GOG_CATALOG_URL,
                params={
                    "discounted":   "eq:true",
                    "productType":  "in:game,pack",
                    "order":        "desc:discount",
                    "limit":        page_size,
                    "page":         page,
                    "countryCode":  "US",
                    "locale":       "en-US",
                    "currencyCode": "USD",
                },
                headers=_GOG_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
        except Exception:
            break

        products = resp.json().get("products", [])
        for product in products:
            price = product.get("price") or {}
            # GOG API returns discount as a string like "-95%", extract the number
            discount_str = price.get("discount", "0%").strip().lstrip("-").rstrip("%")
            try:
                discount = int(discount_str) if discount_str else 0
            except ValueError:
                discount = 0
            if discount < min_discount:
                continue
            # baseMoney.amount and finalMoney.amount are decimal strings, e.g. "29.99"
            try:
                base_money = price.get("baseMoney") or {}
                final_money = price.get("finalMoney") or {}
                base_cents  = round(float(base_money.get("amount", 0)) * 100)
                final_cents = round(float(final_money.get("amount", 0)) * 100)
                if base_cents == 0:
                    continue
            except (TypeError, ValueError):
                continue
            slug = product.get("slug", "")
            games.append(SaleGame(
                name=product.get("title", ""),
                app_id=str(product.get("id", "")),
                discount_percent=discount,
                original_price_cents=base_cents,
                sale_price_cents=final_cents,
                store="GOG",
                store_url=f"https://www.gog.com/game/{slug}" if slug else "https://www.gog.com/games/discounted",
            ))

        if len(products) < page_size:
            break
        page += 1

    return sorted(games, key=lambda g: g.discount_percent, reverse=True)


def get_cheapshark_deals(
    store_ids: list[str],
    min_discount: int = 40,
    page_size: int = 60,
) -> list[SaleGame]:
    """Fetch deals from CheapShark for the requested store IDs.

    CheapShark's /deals endpoint accepts a single storeID at a time, so we
    make one call per store and merge the results. Each page returns up to
    page_size deals (API max 60) sorted by savings descending. We fetch
    additional pages as long as the previous page was full (meaning more deals
    may exist), then filter locally by min_discount and re-sort.

    Args:
        store_ids: List of CheapShark store ID strings to query.
        min_discount: Minimum discount percentage to include (0–100).
        page_size: Deals to request per page (CheapShark max is 60).

    Returns:
        Merged list of SaleGame objects sorted by discount descending.
    """
    games: list[SaleGame] = []
    for store_id in store_ids:
        store_name = _CS_STORE_NAMES.get(store_id, "Other")
        page = 0
        while True:
            try:
                resp = requests.get(
                    "https://www.cheapshark.com/api/1.0/deals",
                    params={
                        "storeID":    store_id,
                        "sortBy":     "Savings",
                        "pageSize":   page_size,
                        "pageNumber": page,
                        "onSale":     1,
                        "lowerPrice": 0,
                    },
                    headers=_CS_HEADERS,
                    timeout=10,
                )
                # CheapShark returns 429 on rate limit; stop processing this store
                if resp.status_code == 429:
                    break
                resp.raise_for_status()
            except Exception:
                break  # Skip remaining pages for this store if the request fails

            deals = resp.json()
            if not isinstance(deals, list):
                # API returned an error dict (e.g., rate limit message), skip this store
                break

            for deal in deals:
                savings = float(deal.get("savings", 0))
                if savings < min_discount:
                    continue
                normal_cents = round(float(deal.get("normalPrice", 0)) * 100)
                sale_cents   = round(float(deal.get("salePrice",   0)) * 100)
                deal_id = deal.get("dealID", "")
                games.append(SaleGame(
                    name=deal.get("title", ""),
                    app_id=deal_id,
                    discount_percent=round(savings),
                    original_price_cents=normal_cents,
                    sale_price_cents=sale_cents,
                    store=store_name,
                    store_url=f"https://www.cheapshark.com/redirect?dealID={deal_id}" if deal_id else "",
                ))

            # If the page wasn't full there are no more pages to fetch
            if len(deals) < page_size:
                break
            page += 1
            # Space out requests to avoid rate limiting
            time.sleep(0.5)

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

        slug = item.get("productSlug") or item.get("urlSlug") or ""
        games.append(SaleGame(
            name=item.get("title", ""),
            app_id=item.get("id", ""),
            discount_percent=100,
            original_price_cents=original_cents,
            sale_price_cents=0,
            store="Epic Games Store",
            store_url=f"https://store.epicgames.com/p/{slug}" if slug else "https://store.epicgames.com/en-US/free-games",
        ))

    return games
