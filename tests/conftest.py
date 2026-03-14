"""
Shared fixtures and helpers for the game recommender test suite.
"""

import json
import pytest
from unittest.mock import MagicMock


# ── Fake Claude async streaming ────────────────────────────────────────────────

class _AsyncTextIter:
    """Async iterator that yields pre-defined text chunks."""
    def __init__(self, chunks: list[str]):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration


class FakeClaudeStream:
    """Async context manager simulating client.messages.stream()."""
    def __init__(self, chunks: list[str]):
        self._chunks = chunks

    async def __aenter__(self):
        self.text_stream = _AsyncTextIter(self._chunks)
        return self

    async def __aexit__(self, *args):
        pass


def make_claude_client(chunks: list[str] | None = None):
    """
    Return a mock anthropic.AsyncAnthropic whose stream yields the given chunks.
    Usage: monkeypatch.setattr('web.app.anthropic.AsyncAnthropic', lambda **_: make_claude_client([...]))
    """
    if chunks is None:
        chunks = ["Here are my ", "top picks for you."]
    client = MagicMock()
    client.messages.stream.return_value = FakeClaudeStream(chunks)
    return client


# ── Fake game library data ─────────────────────────────────────────────────────

FAKE_GAMES = [
    {
        "name": "The Witcher 3",
        "platform": "steam",
        "app_id": "292030",
        "playtime_minutes": 7200,
        "rawg_rating": 4.7,
        "metacritic": 93,
        "genres": ["RPG", "Adventure"],
    },
    {
        "name": "Hades",
        "platform": "steam",
        "app_id": "1145360",
        "playtime_minutes": 2700,
        "rawg_rating": 4.5,
        "metacritic": 93,
        "genres": ["Roguelite", "Action"],
    },
    {
        "name": "Disco Elysium",
        "platform": "steam",
        "app_id": "632470",
        "playtime_minutes": 0,
        "rawg_rating": 4.8,
        "metacritic": 97,
        "genres": ["RPG"],
    },
]


# ── SSE parsing helper ─────────────────────────────────────────────────────────

def parse_sse(body: str) -> list[dict]:
    """Parse a Server-Sent Events response body into a list of data payloads."""
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events
