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
import logging
import requests
from .models import Game

logger = logging.getLogger(__name__)

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
_TOKEN_URL = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"

# Primary library endpoint — returns catalog items the account has entitlement to
_LIBRARY_URL = "https://library-service.live.use1a.on.epicgames.com/library/api/public/items"

# Fallback: launcher assets endpoint lists every owned app by appName/catalogItemId.
# This is the same mechanism Heroic Games Launcher uses and is more reliable than
# the library service when the library service returns empty results.
_ASSETS_URL = (
    "https://launcher-public-service-prod06.ol.epicgames.com"
    "/launcher/api/public/assets/v2/platform/Windows/label/Live"
)

# Catalog service resolves catalogItemId → human-readable title, release date, etc.
# Requires {namespace} in the path; accepts up to ~20 `id` params per request.
_CATALOG_URL = (
    "https://catalog-public-service-prod06.ol.epicgames.com"
    "/catalog/api/shared/namespace/{namespace}/bulk/items"
)


def _basic_auth() -> str:
    """Build the HTTP Basic Auth header value for the token endpoint.

    Epic's token API requires client credentials in the Authorization header
    as Base64-encoded "client_id:client_secret". This is standard OAuth2
    client authentication (RFC 6749 §2.3.1).
    """
    raw = f"{_CLIENT_ID}:{_CLIENT_SECRET}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def exchange_code(auth_code: str, redirect_uri: str | None = None) -> dict:
    """Exchange an authorization code for access and refresh tokens.

    Called immediately after the user copies their authorizationCode from
    the EPIC_AUTH_URL page, or after the OAuth redirect callback delivers a code.
    The code is short-lived (typically ~5 minutes), so this should be called promptly.

    Args:
        auth_code: The "authorizationCode" value from the Epic redirect JSON, or the
            ?code= parameter delivered to the OAuth redirect callback.
        redirect_uri: The redirect_uri used in the authorization request. Must be
            included when using the OAuth2 redirect flow (RFC 6749 §4.1.3); omit for
            the manual code-paste flow which has no redirect_uri.

    Returns:
        Dict containing: access_token, refresh_token, account_id, expires_in.

    Raises:
        requests.HTTPError: If Epic rejects the code (e.g. expired or invalid).
        requests.ConnectionError / requests.Timeout: On network failure.
    """
    body: dict = {"grant_type": "authorization_code", "code": auth_code}
    if redirect_uri:
        body["redirect_uri"] = redirect_uri
    resp = requests.post(
        _TOKEN_URL,
        headers={
            "Authorization": _basic_auth(),               # Client authentication
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=body,
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


def _filter_title(title: str, seen: set) -> bool:
    """Return True if this title should be kept (not filtered out).

    Filters applied:
      - blank / whitespace-only: skip
      - exactly 32 all-alphanumeric chars: skip (these are raw catalogItem UUIDs
        that Epic occasionally surfaces as metadata.title for internal entitlements)
      - already seen (duplicate across pages): skip
    """
    if not title:
        return False
    if len(title) == 32 and title.isalnum():
        return False
    if title in seen:
        return False
    return True


def _extract_year(raw_date: str) -> int | None:
    """Parse a 4-digit year from the start of an ISO date string, or return None."""
    try:
        return int(raw_date[:4]) if len(raw_date) >= 4 and raw_date[:4].isdigit() else None
    except (ValueError, TypeError):
        return None


def _get_games_via_assets(session: requests.Session) -> list[Game]:
    """Fetch the Epic library via the launcher assets API + catalog service.

    This is the same mechanism Heroic Games Launcher uses and is more reliable
    than the library service for accounts where the library endpoint returns no
    results (token scope differences, regional endpoints, API changes, etc.).

    Flow:
      1. GET /assets/v2/platform/Windows/label/Live  →  list of owned apps
         Each asset has: appName, catalogItemId, namespace
      2. Group assets by namespace and batch-look up titles via the catalog API
         (up to 20 IDs per namespace per request)

    Args:
        session: requests.Session with Authorization and User-Agent already set.

    Returns:
        List of Game objects sorted alphabetically.
    """
    resp = session.get(_ASSETS_URL, timeout=15)
    resp.raise_for_status()
    assets = resp.json().get("assets") or []
    logger.debug("Epic assets endpoint returned %d assets", len(assets))

    # Group catalog IDs by namespace for batch catalog lookups.
    # namespace → [(catalogItemId, appName), ...]
    by_ns: dict[str, list[tuple[str, str]]] = {}
    for asset in assets:
        ns  = asset.get("namespace") or ""
        cid = asset.get("catalogItemId") or ""
        if not ns or not cid:
            continue
        by_ns.setdefault(ns, []).append((cid, asset.get("appName") or ""))

    games: list[Game] = []
    seen:  set[str]   = set()

    for ns, items in by_ns.items():
        # Catalog API accepts up to ~20 IDs per request; batch accordingly.
        for i in range(0, len(items), 20):
            batch = items[i : i + 20]
            params: dict = {
                "id":                     [cid for cid, _ in batch],
                "includeDLCDetails":      "false",
                "includeMainGameDetails": "true",
                "country":                "US",
                "locale":                 "en-US",
            }
            try:
                cr = session.get(
                    _CATALOG_URL.format(namespace=ns),
                    params=params,
                    timeout=15,
                )
                cr.raise_for_status()
                catalog = cr.json()
            except requests.HTTPError as exc:
                logger.debug(
                    "Epic catalog lookup failed for namespace %r: %s", ns, exc
                )
                continue

            for cid, _ in batch:
                item      = catalog.get(cid) or {}
                raw_title = (item.get("title") or "").strip()
                if not _filter_title(raw_title, seen):
                    continue
                seen.add(raw_title)

                # releaseInfo is a list; use the first entry's dateAdded
                raw_date = ""
                for ri in item.get("releaseInfo") or []:
                    raw_date = ri.get("dateAdded") or ri.get("releaseDate") or ""
                    if raw_date:
                        break

                games.append(Game(
                    name=raw_title,
                    platform="epic",
                    app_id=cid,
                    release_year=_extract_year(raw_date),
                ))

    logger.debug("Epic assets fallback produced %d games", len(games))
    return sorted(games, key=lambda g: g.name.lower())


def get_epic_library(access_token: str) -> list[Game]:
    """Fetch all games in the user's Epic library.

    Primary path: the library service with includeMetadata=True returns records
    with embedded metadata (title, releaseDate). Records without a metadata.title
    are internal engine/service entitlements and are skipped.

    Fallback path: if the library service returns no playable games (empty response,
    token scope limitation, or API change), the function automatically retries using
    the launcher assets API + catalog service — the same mechanism used by Heroic
    Games Launcher, which is actively maintained and reliably returns all owned games.

    Pagination: the response includes responseMetadata.nextCursor when more pages
    exist. An empty/missing cursor means we've reached the end.

    Args:
        access_token: Bearer token from exchange_code() or refresh_tokens().

    Returns:
        List of Game objects sorted alphabetically. All have playtime_minutes=0
        because Epic exposes no playtime data via any API.

    Raises:
        requests.HTTPError: On auth failure (401) or other API errors.
    """
    # Use a persistent session so the Authorization header is sent on every
    # paginated request without repeating it in every call.
    # The User-Agent must identify as the Epic Games Launcher — the library
    # service returns empty metadata (or rejects the request entirely) for
    # clients that don't send a recognised launcher user-agent string.
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "EpicGamesLauncher/14.0.8-22004860 Windows/10.0.19041.1.256.64bit",
    })

    # ── Primary path: library service ─────────────────────────────────────────
    records = []   # Accumulate raw records from all pages before processing
    cursor  = None  # Start without a cursor (first page)

    while True:
        # Use Python True so requests encodes includeMetadata=True (capital T),
        # matching exactly what Legendary sends. The library service is
        # case-sensitive about this boolean parameter.
        params: dict = {"includeMetadata": True}
        if cursor:
            params["cursor"] = cursor

        resp = session.get(_LIBRARY_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Extend (not append) to flatten pages into one flat record list.
        # Use `or []` because the key may be present with a null value, in which
        # case data.get("records", []) returns None rather than the default.
        records.extend(data.get("records") or [])
        logger.debug(
            "Epic library page fetched: %d records so far", len(records)
        )

        # Extract next cursor; an empty string or missing key both stop pagination.
        # Use `or {}` in case responseMetadata is present but null — the default
        # in .get("responseMetadata", {}) only fires when the key is absent, not
        # when its value is explicitly null.
        cursor = (data.get("responseMetadata") or {}).get("nextCursor")
        if not cursor:
            break

    # ── Deduplicate and filter records into Game objects ──────────────────────
    games: list[Game] = []
    seen:  set[str]   = set()

    for r in records:
        # metadata may be None for internal engine/service entitlements.
        # Only metadata.title is a human-readable name set by Epic's catalog;
        # appName and catalogId are internal identifiers / codenames (e.g.
        # "Arrowroot", "bobcat") that are not meaningful to users.
        metadata  = r.get("metadata") or {}
        raw_title = (metadata.get("title") or "").strip()

        if not _filter_title(raw_title, seen):
            continue
        seen.add(raw_title)

        games.append(Game(
            name=raw_title,
            platform="epic",
            # Prefer catalogId as the stable identifier; fall back to appName
            app_id=r.get("catalogId") or r.get("appName"),
            # playtime_minutes intentionally omitted (defaults to 0) because
            # Epic provides no playtime data through their public APIs
            release_year=_extract_year(metadata.get("releaseDate") or ""),
        ))

    logger.debug(
        "Epic library service: %d raw records → %d games after filtering",
        len(records), len(games),
    )

    games = sorted(games, key=lambda g: g.name.lower())

    # ── Fallback path: assets API + catalog service ────────────────────────────
    # If the library service returned no playable games, fall back to the launcher
    # assets API which enumerates owned apps by catalogItemId and resolves titles
    # via the catalog service. This is the approach used by Heroic Games Launcher
    # and is more robust against token scope limitations and library API changes.
    if not games:
        logger.debug(
            "Epic library service returned 0 games; trying assets+catalog fallback"
        )
        games = _get_games_via_assets(session)

    return games
