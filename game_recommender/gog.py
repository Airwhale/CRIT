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

# ── GOG OAuth2 client credentials ─────────────────────────────────────────────
# These are the GOG Galaxy client credentials, publicly documented in the
# community API docs. GOG's registered redirect URI is fixed to embed.gog.com,
# which is why we cannot use a local callback — we must ask users to paste the code.
_CLIENT_ID     = "46899977096215655"
_CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
_REDIRECT_URI  = "https://embed.gog.com/on_login_success?origin=client"

# ── GOG API endpoints ─────────────────────────────────────────────────────────
_TOKEN_URL    = "https://auth.gog.com/token"
_PRODUCTS_URL = "https://embed.gog.com/account/getFilteredProducts"

# Full GOG login URL — open this in a browser; after login GOG redirects to
# embed.gog.com with ?code=XXXX in the URL bar, which the user must copy.
GOG_AUTH_URL = (
    f"https://login.gog.com/auth"
    f"?client_id={_CLIENT_ID}"
    f"&redirect_uri={_REDIRECT_URI}"
    f"&response_type=code"
)


def exchange_code(auth_code: str) -> dict:
    """Exchange an authorization code for access and refresh tokens.

    Unlike Epic (which uses POST), GOG's token endpoint accepts GET requests
    with credentials as query parameters. This is an older OAuth2 pattern.

    Args:
        auth_code: The "code" parameter extracted from the post-login redirect URL.

    Returns:
        Dict containing: access_token, refresh_token, expires_in, user_id.

    Raises:
        requests.HTTPError: If GOG rejects the code (e.g. expired, already used).
        requests.ConnectionError / requests.Timeout: On network failure.
    """
    resp = requests.get(
        _TOKEN_URL,
        params={
            "client_id":     _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "grant_type":    "authorization_code",
            "code":          auth_code,
            "redirect_uri":  _REDIRECT_URI,  # Must match the registered URI exactly
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_tokens(refresh_token: str) -> dict:
    """Silently refresh an expired GOG access token.

    GOG access tokens expire after ~1 hour. The web app (_fetch_gog) calls this
    automatically when the first library request fails, so users only need to
    re-authenticate if the refresh token itself has expired (typically 30 days).

    Args:
        refresh_token: Token from a previous exchange_code() call.

    Returns:
        Same dict shape as exchange_code() with fresh tokens.

    Raises:
        requests.HTTPError: If the refresh token is expired or invalid.
    """
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
    """Fetch all games in the user's GOG library.

    Uses page-based (not cursor-based) pagination on the getFilteredProducts
    endpoint. mediaType=1 filters to games only, excluding soundtracks and extras.

    The loop always makes at least one request (page 1). If totalPages == 0 or 1,
    the break condition `page >= totalPages` fires immediately after the first
    response, so exactly one HTTP request is made.

    Args:
        access_token: Bearer token from exchange_code() or refresh_tokens().

    Returns:
        List of Game objects sorted alphabetically by title (case-insensitive).
        All games have playtime_minutes=0 because GOG exposes no playtime data.

    Raises:
        requests.HTTPError: On authentication failure or API error.
        requests.ConnectionError / requests.Timeout: On network failure.
    """
    # Include the bearer token in every request for this library fetch
    headers = {"Authorization": f"Bearer {access_token}"}

    games = []
    page = 1  # GOG pages are 1-indexed

    while True:
        resp = requests.get(
            _PRODUCTS_URL,
            params={"mediaType": 1, "page": page},  # mediaType=1 = games (not DLC/soundtracks)
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        for product in (data.get("products") or []):
            # GOG sometimes has null titles for add-ons or beta entries —
            # coerce None to empty string so .strip() always works
            title = (product.get("title") or "").strip()
            if not title:
                continue  # Skip blank or null titles

            # Release year from unix timestamp (0 or missing → None)
            rd_ts = product.get("releaseDate") or 0
            try:
                from datetime import datetime, timezone
                release_year = datetime.fromtimestamp(rd_ts, tz=timezone.utc).year if rd_ts else None
            except (OSError, OverflowError, ValueError):
                release_year = None

            # Community rating (0.0 or missing → None so we can distinguish "unrated" from 0).
            # The GOG API may return rating as a plain number *or* as a dict such as
            # {"value": 42, "count": 1000}.  Only coerce when we actually have a
            # number; any other type (dict, list, str that can't convert) → None.
            raw_rating = product.get("rating")
            try:
                gog_rating = float(raw_rating) if raw_rating else None
            except (TypeError, ValueError):
                gog_rating = None

            games.append(Game(
                name=title,
                platform="gog",
                # str() cast ensures app_id is always a string even if id is int 0,
                # and produces "" when the key is missing (str("") == "")
                app_id=str(product.get("id", "")),
                # playtime_minutes intentionally left at default 0 —
                # GOG does not expose playtime through any web API
                release_year=release_year,
                gog_rating=gog_rating,
            ))

        # Check if we've processed the last page.
        # Default to 1 so a missing totalPages field ends the loop after one request.
        total_pages = data.get("totalPages", 1)
        if page >= total_pages:
            break
        page += 1  # Advance to the next page

    # Sort alphabetically so output is deterministic and easy to scan in the UI
    return sorted(games, key=lambda g: g.name.lower())
