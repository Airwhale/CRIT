"""
IsThereAnyDeal (ITAD) integration for price history and historical lows.

ITAD provides a free API with game price data across Steam, GOG, Humble,
Fanatical, Epic, and dozens of other stores. Key features used here:

  - Lookup by title or Steam app ID → ITAD game UUID
  - Historical low prices (all-time, 3-month, 1-year)
  - Full price change time series for chart rendering
  - Overview: current best price + historical low

Get an API key at: https://isthereanydeal.com/dev/app/
"""

import os
import json
import time
import requests
from pathlib import Path
from typing import Optional

ITAD_API_BASE = "https://api.isthereanydeal.com"

# Persistent cache for ITAD game UUID lookups
_CACHE_DIR = Path(os.environ.get("CRIT_CACHE_DIR", Path.home() / ".cache" / "crit"))
_ITAD_CACHE_FILE = _CACHE_DIR / "itad_id_cache.json"
_id_cache: dict[str, Optional[str]] = {}
_id_cache_loaded = False


def _load_id_cache() -> None:
    global _id_cache_loaded
    if _id_cache_loaded:
        return
    _id_cache_loaded = True
    try:
        if _ITAD_CACHE_FILE.exists():
            _id_cache.update(json.loads(_ITAD_CACHE_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass


def _save_id_cache() -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _ITAD_CACHE_FILE.write_text(json.dumps(_id_cache), encoding="utf-8")
    except Exception:
        pass


def _get_api_key() -> Optional[str]:
    return os.environ.get("ITAD_API_KEY")


def lookup_game_id(title: str, steam_app_id: Optional[str] = None, api_key: Optional[str] = None) -> Optional[str]:
    """Resolve a game to its ITAD UUID.

    Tries Steam app ID first (more reliable), then falls back to title lookup.
    Results are cached persistently.
    """
    api_key = api_key or _get_api_key()
    if not api_key:
        return None

    _load_id_cache()

    # Check cache by app_id or title
    cache_key = f"steam:{steam_app_id}" if steam_app_id else f"title:{title.lower()}"
    if cache_key in _id_cache:
        return _id_cache[cache_key]

    game_id = None

    # Try by Steam app ID first
    if steam_app_id:
        try:
            resp = requests.get(
                f"{ITAD_API_BASE}/games/lookup/v1",
                params={"key": api_key, "appid": steam_app_id},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("found"):
                    game_id = data["game"]["id"]
        except Exception:
            pass

    # Fall back to title lookup
    if not game_id:
        try:
            resp = requests.get(
                f"{ITAD_API_BASE}/games/lookup/v1",
                params={"key": api_key, "title": title},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("found"):
                    game_id = data["game"]["id"]
        except Exception:
            pass

    _id_cache[cache_key] = game_id
    _save_id_cache()
    return game_id


def get_overview(game_ids: list[str], api_key: Optional[str] = None, country: str = "US") -> dict:
    """Get current best price and historical low for up to 200 games.

    Returns dict mapping game_id → {current: {price, store, url}, lowest: {price, store, date}}.
    """
    api_key = api_key or _get_api_key()
    if not api_key or not game_ids:
        return {}

    try:
        resp = requests.post(
            f"{ITAD_API_BASE}/games/overview/v2",
            params={"key": api_key, "country": country},
            json=game_ids[:200],
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}


def get_price_history(game_id: str, api_key: Optional[str] = None, country: str = "US", shops: Optional[list[int]] = None) -> list[dict]:
    """Get full price change time series for a single game.

    Returns a list of {timestamp, price, store, ...} entries for chart rendering.
    """
    api_key = api_key or _get_api_key()
    if not api_key:
        return []

    params: dict = {"key": api_key, "id": game_id, "country": country}
    if shops:
        params["shops"] = ",".join(str(s) for s in shops)

    try:
        resp = requests.get(
            f"{ITAD_API_BASE}/games/history/v2",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


def get_historical_low(game_ids: list[str], api_key: Optional[str] = None, country: str = "US") -> dict:
    """Get all-time historical low for up to 200 games.

    Returns dict mapping game_id → {amount, shop_name, timestamp, ...}.
    """
    api_key = api_key or _get_api_key()
    if not api_key or not game_ids:
        return {}

    try:
        resp = requests.post(
            f"{ITAD_API_BASE}/games/historylow/v1",
            params={"key": api_key, "country": country},
            json=game_ids[:200],
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}
