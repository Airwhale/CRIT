"""
Epic Games Store connector.

Uses the unofficial Epic launcher OAuth2 flow, reverse-engineered from
Legendary (https://github.com/derrod/legendary) and EpicResearch
(https://github.com/MixV2/EpicResearch).

Auth flow for users:
  1. Direct user to EPIC_AUTH_URL (while logged into epicgames.com)
  2. The page returns JSON — user copies the "authorizationCode" value
  3. Call exchange_code(auth_code) to get access/refresh tokens
  4. Call get_epic_library(access_token) to fetch their games

Note: Epic does not expose playtime via any API, so all games will show 0h.
"""

import base64
import requests
from .models import Game

# Launcher client credentials (launcherAppClient2 — used by Legendary, Heroic)
_CLIENT_ID     = "34a02cf8f4414e29b15921876da36f9a"
_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"

EPIC_AUTH_URL = (
    f"https://www.epicgames.com/id/api/redirect"
    f"?clientId={_CLIENT_ID}&responseType=code"
)
_TOKEN_URL   = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
_LIBRARY_URL = "https://library-service.live.use1a.on.epicgames.com/library/api/public/items"


def _basic_auth() -> str:
    raw = f"{_CLIENT_ID}:{_CLIENT_SECRET}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def exchange_code(auth_code: str) -> dict:
    """
    Exchange an authorization code for tokens.

    Returns a dict with: access_token, refresh_token, account_id, expires_in.
    Raises requests.HTTPError on failure.
    """
    resp = requests.post(
        _TOKEN_URL,
        headers={
            "Authorization": _basic_auth(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "authorization_code", "code": auth_code},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_tokens(refresh_token: str) -> dict:
    """Refresh an expired access token. Returns same shape as exchange_code."""
    resp = requests.post(
        _TOKEN_URL,
        headers={
            "Authorization": _basic_auth(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_epic_library(access_token: str) -> list[Game]:
    """
    Fetch all games in the user's Epic library.

    Uses cursor-based pagination. Returns a list of Game objects.
    Epic doesn't expose playtime, so playtime_minutes will be 0 for all.
    """
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {access_token}"

    records = []
    cursor = None

    while True:
        params: dict = {"includeMetadata": "true"}
        if cursor:
            params["cursor"] = cursor

        resp = session.get(_LIBRARY_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        records.extend(data.get("records", []))
        cursor = data.get("responseMetadata", {}).get("nextCursor")
        if not cursor:
            break

    games = []
    seen = set()

    for r in records:
        metadata = r.get("metadata") or {}
        title = (
            metadata.get("title")
            or r.get("appName")
            or r.get("catalogId", "")
        ).strip()

        # Skip blank titles, internal tools, and duplicate catalog items
        if not title or title in seen:
            continue
        # Heuristic: skip items that look like internal identifiers
        if len(title) == 32 and title.isalnum():
            continue

        seen.add(title)
        games.append(Game(
            name=title,
            platform="epic",
            app_id=r.get("catalogId") or r.get("appName"),
        ))

    return sorted(games, key=lambda g: g.name.lower())
