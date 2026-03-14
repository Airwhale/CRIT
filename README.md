# Game Recommender

An AI-powered game recommendation web app. Connect your Steam, Epic Games, and GOG libraries, then let Claude analyse your play history and recommend what to play next — or find great deals in the current Steam sale that actually match your taste.

---

## Features

- **From Library** — recommends games you already own based on playtime patterns and genre taste
- **Steam Deals** — scans current Steam sales (and optionally your wishlist) and picks deals that fit your history
- **Backlog** — recommends unplayed or barely-touched games already sitting in your library
- Live-streaming Claude responses with a blinking cursor as text arrives
- RAWG and Metacritic ratings fetched and passed to Claude for context
- Supports Steam (API key), Epic Games (OAuth), and GOG (OAuth)

---

## Requirements

- Python 3.11 or later
- An [Anthropic API key](https://console.anthropic.com) (Claude claude-opus-4-6 is used for recommendations)
- A [RAWG API key](https://rawg.io/apidocs) (free tier is plenty — used for ratings and genres)
- At least one game platform connected (Steam is the most fully featured)

---

## Local setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd claude
```

### 2. Create a virtual environment

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (Command Prompt):**
```cmd
python -m venv .venv
.venv\Scripts\activate
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

> If PowerShell blocks the script with an execution policy error, run this first:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

**Linux / macOS** — copy the example file:
```bash
cp .env.example .env
```

**Windows (Command Prompt):**
```cmd
copy .env.example .env
```

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

Open `.env` in any text editor and set:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...          # from console.anthropic.com
RAWG_API_KEY=...                      # from rawg.io/apidocs (free)
```

Steam credentials are entered in the web UI rather than `.env` — they are stored in the server's in-memory session for the duration of the process.

### 5. Run

```bash
python run_web.py
```

Then open **http://localhost:8000** in your browser.

To stop the server press `Ctrl+C`.

---

## Testing

The test suite covers every module with both success and failure scenarios, plus targeted corner cases for boundary values and tricky logic paths. No real network calls are made — all external APIs (Steam, Epic, GOG, RAWG, Claude) are mocked.

### Run the tests

```bash
python -m pytest tests/ -v
```

Expected output: **160 tests, 0 failures.**

### Test layout

```
tests/
├── conftest.py          — shared fixtures: fake Claude stream, FAKE_GAMES, SSE parser
├── test_steam.py        — Steam library fetch (20 tests)
├── test_epic.py         — Epic OAuth + library (20 tests)
├── test_gog.py          — GOG OAuth + library (19 tests)
├── test_ratings.py      — RAWG ratings + enrich_games (22 tests)
├── test_steam_sales.py  — featured specials, wishlist, get_all_sales (33 tests)
└── test_web_api.py      — FastAPI endpoints end-to-end (46 tests)
```

### What's covered

| Module | Success paths | Failure paths | Corner cases |
|---|---|---|---|
| Steam | Sorted results, field mapping, fallback names, `last_played=0→None`, env-var creds | Missing/empty key or ID, private profile, HTTP 401/403, timeout, connection error | Equal-playtime sort stability, `name=""` passthrough, very large playtime, `playtime_hours` rounding |
| Epic | Token exchange & refresh, single/paginated library, dedup, 32-char ID skip, title fallbacks | Bad auth code, expired refresh token, HTTP error on library | Empty-string cursor stops pagination, 31-char alphanumeric not filtered, cross-page dedup, `records: null` crash path |
| GOG | Token exchange & refresh, single/paginated library, alphabetical sort, blank-title skip | Bad code, expired token, HTTP / network error | `totalPages=0` makes one request, `title: null` skipped, `products: null` crash path, `id=0` stored as `"0"` |
| Ratings | Full `GameRating` fields, `None` on no results, case-insensitive cache, tags capped at 10, progress callback | Missing API key, HTTP error, network error | Minimal result (sparse fields), <10 tags all returned, `delay=0` valid, per-name cache isolation |
| Steam sales | Discount filter, sort, price formatting, wishlist date ordering, `max_check` cap, `get_all_sales` merge / wishlist-first / dedup / owned-game filter | HTTP errors, failed appdetails batch skipped, featured/wishlist errors handled gracefully | `discount == min_discount` included (strict `<`), `min_discount=0`, price `0` → `"unknown"`/`"free"`, `id=0` kept, wishlist version wins dedup, `owned_app_ids=None` |
| Web API | HTML + security headers, Steam auth + cookie + game_count, platform status, disconnect, library, SSE streaming all three modes, `count` clamping | Missing fields (400), whitespace-only credentials (400), private Steam profile (400), no session, no API key, invalid mode, preferences too long, empty library, Claude exception | Whitespace credentials stripped → 400, `count=0` clamped to 1, `preferences` exactly 500 chars accepted, quotes/newlines in SSE text survive round-trip, `playtime == max_new_minutes` is unplayed, `playtime == 61` at threshold is played, top-75 RAWG boundary, RAWG failure returns unenriched games, partial platform failure returns surviving platform, session persists across calls |
| Steam sales | Discount filtering & sorting, price formatting, wishlist date ordering, `max_check` cap, `get_all_sales` merge / dedup / owned-game filter | HTTP errors, failed appdetails batch skipped silently, featured/wishlist errors handled gracefully |
| Web API | HTML + security headers, Steam auth + cookie, platform status, disconnect, library endpoint, SSE streaming for all three recommendation modes, `count` clamping | Missing fields (400), no session, no `ANTHROPIC_API_KEY`, invalid mode, preferences too long, empty library → streamed error, Claude exception → streamed error |

---

## Getting your API keys

### Anthropic (required)

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Sign in or create an account
3. Navigate to **API Keys** → **Create Key**
4. Copy the key (starts with `sk-ant-`) into your `.env` as `ANTHROPIC_API_KEY`

> Recommendations use Claude claude-opus-4-6 with adaptive thinking. Each recommendation call costs a few cents depending on library size.

### RAWG (required for ratings)

1. Go to [rawg.io/apidocs](https://rawg.io/apidocs) and sign up for a free account
2. Your API key appears on the dashboard
3. Copy it into `.env` as `RAWG_API_KEY`

The free tier allows 20,000 requests/month. The app fetches ratings for up to 75 games per library load, so normal use is well within the limit.

### Steam

Steam can be connected in two ways — **no API key required** for the primary method:

**Option A — Login with Steam (recommended, no API key needed)**

Click **Login with Steam →** in the web UI. You'll be redirected to Steam's login page and sent straight back. Your Steam ID is detected automatically and your library is fetched via Steam's public XML feed.

> Your library must be set to **public** in your Steam privacy settings for this to work. Go to Steam → Profile → Edit Profile → Privacy Settings → Game Details → **Public**.

**Option B — Manual credentials (also enables Steam Deals wishlist)**

Enter your Steam ID and API key directly in the UI:

- **Steam ID (64-bit):** Go to [steamid.io](https://steamid.io), enter your profile URL or username, and copy the **steamID64** value (starts with `765611...`, 17 digits).
- **API key (optional but recommended):** Go to [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey), log in, enter any domain (e.g. `localhost`), and copy the 32-character hex key.

**What changes with an API key?**

| Feature | Without API key | With API key |
|---|---|---|
| Library fetch | ✓ (XML feed) | ✓ (Web API) |
| Steam Deals — featured sales | ✓ | ✓ |
| Steam Deals — wishlist check | ✗ | ✓ |

You can connect via OpenID first (no API key) and then add an API key later using the **Add API key** option shown in the connected state.

### Epic Games

Epic uses OAuth. In the web UI, click **Connect with Epic →** and you will be redirected to Epic's login page. After signing in you are sent straight back — no copy-pasting required.

If the redirect fails (e.g. because you are behind a reverse proxy that changes the callback URL), a fallback appears: open the link shown, copy the `authorizationCode` value from the JSON response, and paste it in.

### GOG

GOG's OAuth redirect URI is fixed to `embed.gog.com`, so we cannot receive it server-side. Instead:

1. Click **Connect with GOG →** — a login popup opens
2. The app attempts to auto-capture the code via `postMessage` from GOG's success page
3. If that doesn't work within 90 seconds a fallback appears — paste the `code=` value from the popup's final URL

---

## Using the app

### Step 1 — Connect platforms

Connect at least one platform. You can connect all three; the library view merges them.

### Step 2 — Load library

Click **Load Library**. The app fetches your game list from all connected platforms, then enriches the top 75 titles with RAWG ratings and genres. For large libraries this can take 30–60 seconds.

Check **Skip RAWG ratings** to load in a few seconds — Claude will have less context but will still work.

### Step 3 — Get recommendations

Choose a mode with the tab switcher:

| Mode | What it does |
|---|---|
| **From Library** | Picks games you own that suit your taste |
| **Steam Deals** | Scans current sales and your wishlist for good buys |
| **Backlog** | Picks unplayed games you already own to try next |

**Optional:** type a mood or preference in the text box (e.g. *"something short I can finish this weekend"* or *"a relaxing game with no time pressure"*).

**Steam Deals options:**
- **Min discount** slider — only show deals at or above this percentage (default 40%)
- **Include my wishlist** — checks your Steam wishlist for items on sale (takes an extra ~20 seconds for up to 100 items)

**Backlog options:**
- **Count as "unplayed" if under X minutes** — games with playtime below this threshold are treated as unplayed (default 60 minutes)

Click **✨ Recommend** and watch Claude's response stream in live.

---

## Deploying to a server

The app is a standard FastAPI/Uvicorn application. Any Linux server with Python 3.11+ works.

### Basic setup (Ubuntu/Debian)

```bash
# Install Python and pip
sudo apt update && sudo apt install -y python3.11 python3.11-venv python3-pip

# Clone and install
git clone <repo-url> /opt/game-recommender
cd /opt/game-recommender
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create .env
cp .env.example .env
nano .env   # fill in ANTHROPIC_API_KEY and RAWG_API_KEY
```

### Run with systemd (recommended)

Create `/etc/systemd/system/game-recommender.service`:

```ini
[Unit]
Description=Game Recommender
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/game-recommender
EnvironmentFile=/opt/game-recommender/.env
ExecStart=/opt/game-recommender/.venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now game-recommender
sudo systemctl status game-recommender
```

### Nginx reverse proxy (with HTTPS)

Install Nginx and Certbot:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

Create `/etc/nginx/sites-available/game-recommender`:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;

        # Required for Server-Sent Events (recommendation streaming)
        proxy_set_header   Connection '';
        proxy_buffering    off;
        proxy_cache        off;
        chunked_transfer_encoding on;

        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        proxy_read_timeout 300s;   # Claude can take a minute on large libraries
    }
}
```

Enable and get a certificate:

```bash
sudo ln -s /etc/nginx/sites-available/game-recommender /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your-domain.com
```

> **Epic OAuth and a custom domain:** if you deploy behind a domain other than `localhost`, Epic's redirect callback will fail because `34a02cf8f4414e29b15921876da36f9a` (the public launcher client ID) only allows `localhost` redirects. In that case users should use the manual code fallback that appears automatically when the redirect fails.

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t game-recommender .
docker run -p 8000:8000 --env-file .env game-recommender
```

Or with Docker Compose:

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    restart: unless-stopped
```

```bash
docker compose up -d
```

---

## Sessions and security

Credentials are stored in **server memory only** — they are never written to disk and are cleared on restart. Each browser gets a session cookie (`session_id`) that maps to its credentials.

This design is intentional for a personal/trusted-network app. If you expose the server to the public internet, consider adding HTTP Basic Auth in Nginx or restricting access by IP:

```nginx
# Restrict to specific IPs
allow 203.0.113.42;
deny all;
```

Or add basic auth:

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd yourusername
```

```nginx
auth_basic "Game Recommender";
auth_basic_user_file /etc/nginx/.htpasswd;
```

---

## Troubleshooting

**Library loads empty / "profile is private"**
Set your Steam Game Details privacy to **Public** in Steam → Profile → Edit Profile → Privacy Settings.

**RAWG ratings all missing**
Check that `RAWG_API_KEY` is set correctly in `.env`. The app continues without ratings if the key is invalid — it just shows dashes.

**"ANTHROPIC_API_KEY not configured"**
The key must be in `.env` (or exported as an environment variable) before starting the server. Restart after adding it.

**Streaming stops / recommendation cuts off**
If running behind Nginx, ensure `proxy_buffering off` and `proxy_read_timeout 300s` are set. Without these, SSE connections are buffered and the streaming effect breaks.

**Epic OAuth redirects to wrong URL**
Epic's redirect lands at the URL the server detects from the `Host` header. If you are behind a reverse proxy, ensure `X-Forwarded-Proto` and `X-Forwarded-For` headers are forwarded so the server constructs the correct callback URL.

**Steam Deals mode says "No sales found"**
Lower the minimum discount slider — Steam's featured specials change daily and may all be below the threshold. The slider goes down to 10%.

**Wishlist check is slow**
The wishlist checker batches 20 appdetails requests at a time with a 0.4-second delay between batches to stay within Steam's rate limits. For 100 wishlist items expect about 20–25 seconds. You can uncheck **Include my wishlist** for faster results using only the featured deals.
