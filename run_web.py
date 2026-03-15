#!/usr/bin/env python3
"""Launch the CRIT — Curated Recommendations In Titles web UI.

Starts a uvicorn ASGI server on port 8000. The --reload flag is enabled
so code changes take effect immediately during development without needing
to restart the server manually.

For production, set COOKIE_SECURE=true in your .env and run behind a
reverse proxy (Nginx/Caddy) that handles TLS termination.
"""
import shutil
import sys
from pathlib import Path

import uvicorn

_ROOT = Path(__file__).parent
_ENV  = _ROOT / ".env"
_EX   = _ROOT / ".env.example"

if not _ENV.exists():
    if _EX.exists():
        shutil.copy(_EX, _ENV)
        print("Created .env from .env.example — open it and fill in your API keys, then run again.")
        sys.exit(0)
    else:
        print("Warning: no .env file found. Set ANTHROPIC_API_KEY (and optionally RAWG_API_KEY) as environment variables.")

if __name__ == "__main__":
    print("CRIT — Curated Recommendations In Titles → http://localhost:8000")
    # host="0.0.0.0" binds to all interfaces, making the server reachable
    # from other devices on the local network (e.g. mobile browser testing).
    # For local-only use, change to host="127.0.0.1".
    uvicorn.run("web.app:app", host="0.0.0.0", port=8000, reload=True)
