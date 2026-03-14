# Changelog

## [Unreleased]

### Steam — OpenID login and API-key-free library fetch

**Problem:** Connecting Steam previously required both a developer API key and a manually looked-up 64-bit Steam ID, which was a significant barrier for new users.

**Solution:** Added Steam OpenID 2.0 login ("Login with Steam →") so users can connect with a single redirect — no API key, no manual ID lookup. The library is fetched via Steam's public XML feed when no API key is present. An API key can optionally be added afterwards to unlock Steam Deals wishlist support.

#### `game_recommender/steam.py`
- Added `get_steam_library_xml(user_id)` — fetches `steamcommunity.com/profiles/{id}/games?xml=1`, parses `hoursOnRecord` (hours, comma-formatted) into `playtime_minutes`. No API key required. Returns the same `list[Game]` shape as the existing Web API function.

#### `web/app.py`
- Added `GET /auth/steam/start` — redirects the browser to Steam's OpenID authorization endpoint.
- Added `GET /auth/steam/callback` — verifies the OpenID signature by posting back to Steam (`openid.mode=check_authentication`), extracts the Steam ID from the `claimed_id` URL, and stores the session without an API key.
- Updated `POST /api/auth/steam` — `api_key` is now optional. When omitted, the Steam ID is validated via the XML feed instead of the Web API.
- Added `POST /api/auth/steam/apikey` — upgrades an existing OpenID-only session by adding an API key (validated with a live Web API call before storing).
- Updated `GET /api/status` — response now includes `steam_has_api_key: bool` so the frontend can show the appropriate connected state.
- Updated `_fetch_steam()` — branches on `api_key` presence: uses `get_steam_library` (Web API) when available, otherwise `get_steam_library_xml`.
- Updated Steam Deals SSE stream — wishlist check is silently skipped when no API key is present; `_fetch_sales` receives `include_wishlist=False` in that case.

#### `web/templates/index.html`
- Steam platform card redesigned:
  - Primary action is now **"Login with Steam →"** (OpenID redirect, no credentials needed).
  - Manual credential entry (Steam ID + optional API key) moved into a collapsible `<details>` element.
  - API key field is now labelled as optional, with a note that it is needed for wishlist deals.
- Two distinct connected states for Steam:
  - **Basic** (no API key): shows a success notice and an expandable "Add API key" form.
  - **Full** (API key set): shows a success notice and a disconnect button only.
- Added `saveApiKey()` JS function — posts to `/api/auth/steam/apikey` and switches the card from basic to full state on success.
- `markConnected('steam', hasApiKey)` and `markDisconnected('steam')` updated to manage the two connected states.
- `GET /api/status` response used on page load to correctly restore the connected state variant.

#### `README.md`
- Rewrote the **Steam** section under *Getting your API keys*:
  - Documented Option A (Login with Steam — no API key) as the recommended path.
  - Documented Option B (manual credentials) for users who want wishlist support.
  - Added a feature comparison table showing what works with and without an API key.
- Updated the **Local setup** section (step 2 and step 4) to show separate commands for Linux/macOS, Windows Command Prompt, and Windows PowerShell, including an execution policy note for PowerShell users.

---

### Fix buttons unresponsive — CSP blocking inline JS

**Problem:** The Content Security Policy header set `script-src 'self'` without `'unsafe-inline'`, which caused browsers to silently block all `onclick` handlers and the inline `<script>` block. Every button on the page appeared dead.

**Fix:** Added `'unsafe-inline'` to `script-src` in the CSP middleware (`web/app.py`).

---

### Fix Epic OAuth — remove broken redirect flow

**Problem:** Clicking "Connect with Epic →" triggered a full-page redirect to `/auth/epic/start`, which sent the browser to Epic's OAuth authorize endpoint with a custom `redirect_uri`. Epic rejected this with "OAuth request isn't valid" because the launcher client ID (`34a02cf8f4414e29b15921876da36f9a`) does not support custom redirect URIs.

**Fix:** Removed the redirect flow entirely. The Epic card now shows the manual code flow immediately:
- User opens the Epic redirect page link, copies `authorizationCode` from the JSON, pastes it in.

#### `web/templates/index.html`
- Epic platform card redesigned: removed "Connect with Epic →" redirect button; manual code paste instructions are shown directly (no longer a fallback).
- Removed `startEpicAuth()` JS function.
- Cleaned up `submitEpicCode()` — no longer hides a fallback panel after success.
- Removed dead code in `init()` that showed the Epic fallback panel on `auth_error`.

#### `web/app.py`
- `/auth/epic/start` route kept in place (harmless) but no longer reachable from the UI.

#### `README.md`
- Rewrote the **Epic Games** section to document the manual code flow as the only supported method, with a note explaining why redirect URIs are not supported by this client ID.
