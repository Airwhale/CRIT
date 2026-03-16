# Changelog

## [Unreleased]

### Library loading skeleton (streaming SSE enrichment)

Games now appear in the table immediately after the platform fetch completes. RAWG ratings, Metacritic scores, genres, and release years stream in row-by-row as each lookup finishes concurrently in the background, rather than requiring a full wait for all enrichment to complete.

#### `web/app.py`
- Added `GET /api/library/stream` SSE endpoint. Event sequence:
  1. `{"type": "games", "games": [...], "errors": [...]}` — emitted immediately after all platforms are fetched concurrently; games are unenriched at this point.
  2. `{"type": "rawg_update", "game": {...}}` — one event per game as each RAWG lookup finishes (5-slot `asyncio.Semaphore`; results arrive out of order).
  3. `{"type": "done"}` — signals stream completion.
- Skips RAWG enrichment entirely when `skip_ratings=true` or no `RAWG_API_KEY` is set.
- Forwards the `rawg_limit` query parameter to cap enrichment to the top N games by playtime.

#### `web/templates/index.html`
- Extracted `_sentinel` and `_cellFor` cell renderers to module scope so both `renderLibraryTable` and the new `_updateGameRow` share them (previously defined inline inside `renderLibraryTable`).
- `renderLibraryTable` now adds `data-game-key="${platform}|${name}"` to each `<tr>` element for row lookup by the streaming updater.
- Added `_updateGameRow(game)` — finds a row via `tbody.rows` array scan + `dataset.gameKey` comparison (avoids CSS selector escaping issues), re-renders the row in-place with updated RAWG fields, and re-attaches price hover handlers.
- Rewrote `loadLibrary()` to use `EventSource` on `/api/library/stream`:
  - On `games` event: hides the load form, shows the table, and displays a subtle "Loading ratings…" spinner in `lib-showing`.
  - On `rawg_update`: updates `allGames[idx]` and calls `_updateGameRow` to patch the row without a full repaint.
  - On `done`: clears the spinner, saves to disk cache via `POST /api/library/save`.

#### `tests/test_web_api.py`
- Added `TestLibraryStream` class (8 tests): games event emitted first, done always last, `skip_ratings` suppresses rawg_update events, one rawg_update per game with `RAWG_API_KEY` set, platform errors reported in games event, playtime sort order, cached library streamed immediately.

---

### ITAD (IsThereAnyDeal) price history integration

The Steam Deals table now shows historical low prices and price history sparklines from IsThereAnyDeal.

#### `game_recommender/itad.py`
- Added `batch_lookup_game_ids(game_infos, api_key, max_workers=8)` — parallel ITAD ID resolution using `ThreadPoolExecutor`, matching by Steam app ID when available.
- Added in-memory `_HISTORY_CACHE` with 6-hour TTL for price history responses.
- Updated `get_overview()` to normalise the API response (handles both list and dict formats from the `/games/overview/v2` endpoint).
- Updated `get_price_history()` to check and populate the in-memory cache.

#### `web/app.py`
- Added `_enrich_deals_with_itad(sale_games, itad_key)` — parallel ID lookup, batch overview call, and verdict assignment (`all_time_low` / `near_low` / `below_regular` / `no_data`) comparing current sale price to historical low.
- Sales SSE stream now yields `{"itad_data": {...}}` after the deals event when `ITAD_API_KEY` is configured.
- Deals payload now includes `app_id` field.

#### `web/templates/index.html`
- Added Chart.js CDN script for price history sparklines.
- Added `#price-popover` singleton and CSS for the popover and ITAD verdict badges.
- Deals table has a new "Hist. Low" column; `_itadCell(d)` renders the badge and price.
- SSE handler updated to process `msg.itad_data` and update the deals table in-place.
- `_attachPriceHoverHandlers()`, `_showPopoverLoading()`, `_showPopoverNoData()`, `_renderPriceChart()` — hovering a game name in the library table fetches price history and renders a Chart.js sparkline popover.
- In-memory `_itadIdCache` and `_historyCache` avoid redundant network calls within a session.

#### `.env.example`
- Added `ITAD_API_KEY=` with a comment pointing to isthereanydeal.com/dev/app/.

---

### Session persistence (localStorage)

Platform credentials and the loaded library are now saved to localStorage so they survive page refreshes without needing to reconnect or re-fetch.

#### `web/app.py`
- Added `POST /api/auth/epic/restore` and `POST /api/auth/gog/restore` endpoints that accept tokens from localStorage and restore the server-side session.
- Updated `GET /api/status` to return `steam_user_id`, `epic_tokens`, and `gog_tokens` fields so the frontend can write them to localStorage on load.

#### `web/templates/index.html`
- Added `_lsGet`, `_lsSet`, `_lsDel` localStorage helpers.
- `_tryRestoreFromLocalStorage()` — called on `init()`; posts saved credentials back to the restore endpoints so the connected state is immediately available.
- `_tryLoadFromDiskCache()` — called on `init()` after platform status check; fetches `/api/library/cached` and shows the table immediately if a saved library exists, with a "Loaded from cache — click ↻ Refresh to reload" notice.
- `loadLibrary()` posts to `/api/library/save` after a successful stream so the library is persisted for the next visit.
- `markDisconnected()` now calls `_lsDel('crit_${platform}')` to clear the stored credential on disconnect.

---

### Recommendation history

Past recommendations are saved in `localStorage` and displayed in a collapsible panel below the output.

#### `web/templates/index.html`
- Added `#history-section` card with a collapsible body.
- `_saveRecommendationHistory(text)` — called after the `done` SSE event; stores up to 20 entries keyed by ISO timestamp.
- `_loadRecommendationHistory()` and `_renderHistory()` — called on `init()`; renders saved entries as expandable items.
- `_toggleHistoryItem(id)` — toggles the collapsed/expanded state of an individual history entry.

---

### Model and thinking controls

The recommendation form now exposes model selection and extended thinking as explicit UI controls rather than hard-coded server defaults.

#### `web/templates/index.html`
- Added a model `<select>` (Sonnet 4.6 default / Haiku 4.5 / Opus 4.6) and a "Deep thinking" checkbox above the Recommend button.
- `onModelChange()` — disables the thinking checkbox for Haiku (which does not support extended thinking).
- The selected model and thinking flag are sent as query parameters to `/api/recommend`.

#### `web/app.py`
- `GET /api/recommend` now accepts `model` and `thinking` query parameters, forwarded to the Anthropic API call.

---

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
