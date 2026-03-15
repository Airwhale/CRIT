#!/usr/bin/env python3
"""Launch the CRIT — Curated Recommendations In Titles web UI.

Starts a uvicorn ASGI server on port 8000. The --reload flag is enabled
so code changes take effect immediately during development without needing
to restart the server manually.

For production, set COOKIE_SECURE=true in your .env and run behind a
reverse proxy (Nginx/Caddy) that handles TLS termination.
"""
import uvicorn

if __name__ == "__main__":
    print("CRIT — Curated Recommendations In Titles → http://localhost:8000")
    # host="0.0.0.0" binds to all interfaces, making the server reachable
    # from other devices on the local network (e.g. mobile browser testing).
    # For local-only use, change to host="127.0.0.1".
    uvicorn.run("web.app:app", host="0.0.0.0", port=8000, reload=True)
