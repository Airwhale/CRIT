"""Epic Games Store connector.

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

# ── OAuth2 client credentials ──────────────────────────────────────────────────
# These are the "launcherAppClient2" credentials used by third-party Epic clients
# like Legendary and Heroic Games Launcher. They are publicly known and not secret
# in the traditional sense — the same values appear in the Epic Launcher binary.
_CLIENT_ID     = "34a02cf8f4414e29b15921876da36f9a"
_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"

# ── API endpoints ─────────────────────────────────────────────────────────────
# This URL returns a JSON page with an "authorizationCode" field when the user
# is already logged in. Visiting it while logged out shows the Epic login UI.
EPIC_AUTH_URL = (
    f"https://www.epicgames.com/id/api/redirect"
    f"?clientId={_CLIENT_ID}&responseType=code"
)

# OAuth2 token endpoint for the launcher client
_TOKEN_URL   = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"

# Library endpoint — returns all catalog items the account has entitlement to
_LIBRARY_URL = "https://library-service.live.use1a.on.epicgames.com/library/api/public/items"


def _basic_auth() -> str:
    """Build the HTTP Basic Auth header value for the token endpoint.

    Epic's token API requires client credentials in the Authorization header
    as Base64-encoded "client_id:client_secret". This is standard OAuth2
    client authentication (RFC 6749 §2.3.1).
    """
    raw = f"{_CLIENT_ID}:{_CLIENT_SECRET}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def exchange_code(auth_code: str) -> dict:
    """Exchange an authorization code for access and refresh tokens.

    Called immediately after the user copies their authorizationCode from
    the EPIC_AUTH_URL page. The code is short-lived (typically ~5 minutes),
    so this should be called promptly.

    Args:
        auth_code: The "authorizationCode" value from the Epic redirect JSON.

    Returns:
        Dict containing: access_token, refresh_token, account_id, expires_in.

    Raises:
        requests.HTTPError: If Epic rejects the code (e.g. expired or invalid).
        requests.ConnectionError / requests.Timeout: On network failure.
    """
    resp = requests.post(
        _TOKEN_URL,
        headers={
            "Authorization": _basic_auth(),               # Client authentication
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "authorization_code", "code": auth_code},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_tokens(refresh_token: str) -> dict:
    """Silently refresh an expired access token using the stored refresh token.

    Epic access tokens typically expire after 2 hours. The web app calls this
    automatically in _fetch_epic() when the first library fetch fails, so users
    don't need to re-authenticate on every page load.

    Args:
        refresh_token: The refresh_token value from a previous exchange_code() call.

    Returns:
        Same dict shape as exchange_code() with fresh tokens.

    Raises:
        requests.HTTPError: If the refresh token is also expired (user must re-auth).
    """
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
    """Fetch all games in the user's Epic library.

    Iterates through cursor-based pages of the library endpoint, collecting
    all catalog records, then deduplicates and filters them into clean Game objects.

    Pagination: the response includes responseMetadata.nextCursor when more pages
    exist. An empty/missing cursor means we've reached the end.

    Title resolution:
      Only metadata.title is used. appName and catalogId are internal
      codenames (e.g. "Arrowroot", "bobcat") set by Epic engineers and
      are never shown to users. Records without a metadata.title are
      internal engine/service entitlements and are skipped entirely.

    Filtering:
      - Records with no metadata.title are skipped (internal entitlements)
      - Blank/whitespace titles are skipped
      - Duplicate titles across pages are skipped (keeps first occurrence)

    Args:
        access_token: Bearer token from exchange_code() or refresh_tokens().

    Returns:
        List of Game objects sorted alphabetically. All have playtime_minutes=0
        because Epic exposes no playtime data via any API.

    Raises:
        requests.HTTPError: On auth failure (401) or other API errors.
        TypeError: If the API returns null for the "records" field (defensive check).
    """
    # Use a persistent session so the Authorization header is sent on every
    # paginated request without repeating it in every call.
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {access_token}"

    records = []   # Accumulate raw records from all pages before processing
    cursor = None  # Start without a cursor (first page)

    # Paginate until the API stops returning a next cursor
    while True:
        params: dict = {"includeMetadata": "true"}  # metadata contains the human title
        if cursor:
            params["cursor"] = cursor  # Add cursor only for pages 2+

        resp = session.get(_LIBRARY_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Extend (not append) to flatten pages into one flat record list
        records.extend(data.get("records", []))

        # Extract next cursor; an empty string or missing key both stop pagination
        cursor = data.get("responseMetadata", {}).get("nextCursor")
        if not cursor:
            break

    # ── Deduplicate and filter records into Game objects ──────────────────────
    games = []
    seen = set()  # Track titles already added to prevent duplicates across pages

    for r in records:
        # metadata may be None for internal engine/service entitlements.
        # Only metadata.title is a human-readable name set by Epic's catalog;
        # appName and catalogId are internal identifiers / codenames (e.g.
        # "Arrowroot", "bobcat") that are not meaningful to users.
        # Skipping records with no metadata.title filters out all those noise
        # entries while keeping every actual purchasable / free game.
        metadata = r.get("metadata") or {}
        raw_title = metadata.get("title")

        # Only metadata.title is the human-readable name set by Epic's catalog.
        # appName and catalogId are internal identifiers / codenames (e.g.
        # "Arrowroot", "bobcat", "prokofiev") that are never shown to users.
        # Records with no metadata.title are internal engine/service entitlements
        # and must be skipped — falling back to appName would surface those codenames.
        if not raw_title:
            continue

        title = raw_title.strip()

        # Skip entries with no title, 32-char all-alphanumeric internal IDs, or duplicates
        if not title or (len(title) == 32 and title.isalnum()) or title in seen:
            continue

        seen.add(title)
        # Extract release year from ISO date string (e.g. "2021-08-12T00:00:00.000Z")
        raw_date = metadata.get("releaseDate") or ""
        try:
            release_year = int(raw_date[:4]) if len(raw_date) >= 4 and raw_date[:4].isdigit() else None
        except (ValueError, TypeError):
            release_year = None
        games.append(Game(
            name=title,
            platform="epic",
            # Prefer catalogId as the stable identifier; fall back to appName
            app_id=r.get("catalogId") or r.get("appName"),
            # playtime_minutes intentionally omitted (defaults to 0) because
            # Epic provides no playtime data through their public APIs
            release_year=release_year,
        ))

    # Sort alphabetically so the output is deterministic and easy to scan
    return sorted(games, key=lambda g: g.name.lower())
