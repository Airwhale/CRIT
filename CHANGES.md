# Changelog

## [Unreleased]

### release_year, gog_rating, and tags fields

Game objects and library API responses now carry three additional fields from platform data and RAWG:

- **`release_year`** (`int | None | "~"`) — release year parsed from Epic's ISO date strings, GOG's unix timestamps, or filled in from RAWG. Steam's XML/Web API does not provide release dates, so Steam games start with the sentinel value `"~"` and are filled by RAWG when found. `None` means RAWG found the game but has no date.
- **`gog_rating`** (`float | None | "~"`) — GOG community rating (out of 100). Non-GOG games carry `"~"` (not applicable). A rating of 0 on GOG means unrated and is stored as `None`.
- **`tags`** — RAWG tag list (up to 10), passed to Claude for additional genre context.

The sentinel `"~"` renders as `–` in the UI (platform does not have this field), while `None` renders as `○` (platform has the field type but no data for this specific game).

#### `game_recommender/models.py`
- Added `release_year: Optional[int] = None` and `gog_rating: Optional[float] = None` to the `Game` dataclass.

#### `game_recommender/epic.py`
- `get_epic_library` now extracts `release_year` from `metadata.releaseDate` (ISO 8601 string).

#### `game_recommender/gog.py`
- `get_gog_library` now extracts `release_year` from `releaseDate` (unix timestamp) and `gog_rating` from `rating`. Zero/missing values are stored as `None`.

#### `web/app.py`
- `_game_to_dict` now includes `release_year`, `gog_rating`, and `tags` fields with correct sentinel values per platform.
- `_enrich_with_rawg` fills `release_year` from RAWG when the platform didn't supply one, and populates `tags`.

---

### Dynamic column picker for the library table

The library table columns are now user-configurable without any page reload or data re-fetch.

#### `web/templates/index.html`
- Added a **Columns ▾** button in the library toolbar. Clicking it opens a popover with a checkbox for each of the nine columns (Game, Platform, Playtime, Last Played, Year, RAWG, Metacritic, GOG Rating, Genres / Tags). Toggling a checkbox instantly rebuilds the header and all rows.
- Defined a `COLUMNS` array as the single source of truth for column metadata (key, label, sort key, width). `renderTableHead()` and `renderLibraryTable()` both consume it, so adding or removing a column only requires a change to `COLUMNS`.
- `visibleCols` (`Set`) tracks which columns are shown. All columns are visible by default.
- `_updateSortIndicators()` now queries `.sort-ind` elements from the live DOM rather than a hardcoded list, so it works correctly as columns are added and removed.
- Added **Year** (release year) and **GOG Rating** columns with sort support. Both use sentinel rendering: `–` for `"~"` (not applicable), `○` for `null` (unknown).
- Renamed **Genres** to **Genres / Tags** — shows up to 2 genres and 1 RAWG tag.
- Added sort options for Release Year and GOG Rating in the sort dropdown.

---

### CSV export and import

#### `web/templates/index.html`
- **Export CSV** button downloads `library.csv` with all fields: `name`, `platform`, `app_id`, `playtime_minutes`, `last_played`, `release_year`, `rawg_rating`, `metacritic`, `gog_rating`, `genres` (semicolon-separated), `tags` (semicolon-separated).
- **Import CSV** button parses the same layout and populates the table without a network round-trip. `"~"` sentinels are preserved through the round-trip. Column detection is header-based so extra or reordered columns are handled gracefully.

---

### Fix three correctness bugs in Epic data fetching

#### `game_recommender/epic.py`
- **Null records crash:** `data.get("records", [])` returns `None` when the key is present with a null value (the default only applies when the key is absent). Changed to `data.get("records") or []` so a null page is treated as empty rather than crashing with `TypeError`.
- **Missing `redirect_uri` in OAuth token exchange:** the OAuth redirect flow passes `redirect_uri` to Epic's authorize endpoint but `exchange_code()` never echoed it to the token endpoint, violating RFC 6749 §4.1.3. Added an optional `redirect_uri` parameter to `exchange_code()`.

#### `web/app.py`
- **`redirect_uri` now passed from OAuth callback:** `epic_auth_callback` constructs the same callback URL used in the authorization request and passes it to `exchange_code()`.
- **Over-broad exception catch for token refresh:** `_fetch_epic` previously caught all exceptions and attempted a token refresh, masking non-auth errors (network failures, `TypeError`, etc.). Narrowed to `requests.HTTPError` with status 401 only.

---

### Fix null products crash in GOG data fetching

#### `game_recommender/gog.py`
- Same fix as Epic: `data.get("products", [])` can return `None` when the key exists with a null value. Changed to `(data.get("products") or [])` for graceful empty-page handling.

---

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

---

### Fix GOG login popup — white page

**Problem:** The GOG login popup showed a blank white page. Two causes:
1. `auth.gog.com` has been replaced by `login.gog.com` as GOG's auth endpoint.
2. `layout=client2` is GOG Galaxy's headless desktop layout — it renders a minimal page not intended for browser display, causing the white page.

**Fix:** Updated `GOG_AUTH_URL` in both `gog.py` and `index.html` to use `login.gog.com` and removed the `layout=client2` parameter.
