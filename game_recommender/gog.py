"""
GOG connector.

Uses the unofficial GOG Galaxy OAuth2 flow, documented by the community at
https://gogapidocs.readthedocs.io/en/latest/ (source: Yepoleb/gogapidocs).

Auth flow for users:
  1. Direct user to GOG_AUTH_URL
  2. User logs in; GOG redirects to embed.gog.com/on_login_success?...&code=XXXX
  3. User copies the "code" parameter from their browser's address bar
  4. Call exchange_code(auth_code) to get access/refresh tokens
  5. Call get_gog_library(access_token) to fetch their games

Constraints:
  - GOG only allows embed.gog.com as the registered redirect URI — we cannot
    receive the callback ourselves, hence the copy-paste flow.
  - GOG does not expose playtime via web API; playtime_minutes will be 0.
  - Tokens expire after ~1 hour; use refresh_tokens() to renew silently.
"""

import requests
from .models import Game

_CLIENT_ID     = "46899977096215655"
_CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
_REDIRECT_URI  = "https://embed.gog.com/on_login_success?origin=client"
_TOKEN_URL     = "https://auth.gog.com/token"
_PRODUCTS_URL  = "https://embed.gog.com/account/getFilteredProducts"

GOG_AUTH_URL = (
    f"https://auth.gog.com/auth"
    f"?client_id={_CLIENT_ID}"
    f"&redirect_uri={_REDIRECT_URI}"
    f"&response_type=code"
    f"&layout=client2"
)


def exchange_code(auth_code: str) -> dict:
    """
    Exchange an authorization code for tokens.

    Returns a dict with: access_token, refresh_token, expires_in, user_id.
    Raises requests.HTTPError on failure.
    """
    resp = requests.get(
        _TOKEN_URL,
        params={
            "client_id":     _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "grant_type":    "authorization_code",
            "code":          auth_code,
            "redirect_uri":  _REDIRECT_URI,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_tokens(refresh_token: str) -> dict:
    """Refresh an expired access token. Returns same shape as exchange_code."""
    resp = requests.get(
        _TOKEN_URL,
        params={
            "client_id":     _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri":  _REDIRECT_URI,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_gog_library(access_token: str) -> list[Game]:
    """
    Fetch all games in the user's GOG library.

    Uses page-based pagination on getFilteredProducts (mediaType=1 = games).
    Returns a list of Game objects. playtime_minutes will be 0 for all.
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    games = []
    page = 1

    while True:
        resp = requests.get(
            _PRODUCTS_URL,
            params={"mediaType": 1, "page": page},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        for product in data.get("products", []):
            title = (product.get("title") or "").strip()
            if not title:
                continue
            games.append(Game(
                name=title,
                platform="gog",
                app_id=str(product.get("id", "")),
            ))

        total_pages = data.get("totalPages", 1)
        if page >= total_pages:
            break
        page += 1

    return sorted(games, key=lambda g: g.name.lower())
