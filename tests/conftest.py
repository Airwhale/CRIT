"""
Shared fixtures and helpers for the game recommender test suite.

This module is loaded automatically by pytest before any test file runs.
It provides:
  - FakeClaudeStream / make_claude_client — mock the AsyncAnthropic streaming API
  - FAKE_GAMES — a small realistic game library for API tests
  - parse_sse() — parse Server-Sent Events response bodies into event dicts
"""

import json
import pytest
from unittest.mock import MagicMock


# ── Fake Claude async streaming ────────────────────────────────────────────────
# The web app uses `async with client.messages.stream(...) as stream:` and then
# `async for text in stream.text_stream:`. We need to mock both the async context
# manager protocol (__aenter__/__aexit__) and the async iterator protocol.

class _AsyncTextIter:
    """Async iterator that yields pre-defined text chunks, one per iteration.

    Simulates the text_stream attribute of an AsyncAnthropic stream context.
    StopAsyncIteration signals the end of the stream to `async for` loops.
    """
    def __init__(self, chunks: list[str]):
        self._chunks = iter(chunks)  # Wrap in a regular iterator to consume one-by-one

    def __aiter__(self):
        return self  # The iterator is its own async iterator

    async def __anext__(self):
        try:
            return next(self._chunks)  # Return the next chunk synchronously
        except StopIteration:
            raise StopAsyncIteration  # Translate sync StopIteration → async equivalent


class FakeClaudeStream:
    """Async context manager that simulates client.messages.stream().

    Usage (in tests):
        mock_client.messages.stream.return_value = FakeClaudeStream(["Hello ", "world!"])

    The with-block sets up text_stream as an _AsyncTextIter so the body of
    `async for text in stream.text_stream:` receives the predefined chunks.
    """
    def __init__(self, chunks: list[str]):
        self._chunks = chunks

    async def __aenter__(self):
        # Expose text_stream as the attribute the app code iterates over
        self.text_stream = _AsyncTextIter(self._chunks)
        return self

    async def __aexit__(self, *args):
        pass  # No cleanup needed for the fake stream


def make_claude_client(chunks: list[str] | None = None):
    """Create a mock anthropic.AsyncAnthropic whose stream yields the given chunks.

    Used to monkeypatch `web.app.anthropic.AsyncAnthropic` in SSE tests so the
    endpoint streams predictable text without hitting the real Claude API.

    Args:
        chunks: List of text strings to yield from the stream. Defaults to a
                short two-part message that contains "The Witcher 3" (which
                several tests check for in the streamed output).

    Returns:
        MagicMock whose .messages.stream() returns a FakeClaudeStream.

    Example (in a test):
        monkeypatch.setattr(
            'web.app.anthropic.AsyncAnthropic',
            lambda **_: make_claude_client(["Great pick: ", "The Witcher 3"])
        )
    """
    if chunks is None:
        chunks = ["Here are my ", "top picks for you."]
    client = MagicMock()
    # .stream() is called as an async context manager in the app code,
    # so its return_value must support __aenter__ / __aexit__
    client.messages.stream.return_value = FakeClaudeStream(chunks)
    return client


# ── Fake OpenRouter (OpenAI-compatible) async streaming ──────────────────────
# When the user picks the OpenRouter provider, the web app uses:
#     client = openai.AsyncOpenAI(...)
#     stream = await client.chat.completions.create(stream=True, ...)
#     async for chunk in stream:
#         text = chunk.choices[0].delta.content
#
# We mock the awaitable `.create()` and the async-iterable stream of chunks
# whose `.choices[0].delta.content` carries the text deltas.

class _AsyncChunkIter:
    """Async iterator yielding OpenAI-shaped chat completion chunk objects."""
    def __init__(self, chunks: list[str]):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            text = next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration
        # Construct the minimum chunk shape the app reads: chunk.choices[0].delta.content
        delta  = type("Delta",  (), {"content": text})()
        choice = type("Choice", (), {"delta":   delta})()
        return  type("Chunk",   (), {"choices": [choice]})()


def make_openrouter_client(chunks: list[str] | None = None):
    """Create a fake openai.AsyncOpenAI client whose stream yields the given chunks.

    Used to monkeypatch `web.app.openai.AsyncOpenAI` in the OpenRouter-path SSE
    tests so they can stream predictable text without hitting the real API.

    `client.chat.completions.create` is implemented as an async function, which
    makes `await client.chat.completions.create(...)` resolve to an _AsyncChunkIter
    that the app code then iterates over with `async for chunk in stream:`.
    """
    if chunks is None:
        chunks = ["Here are my ", "top picks for you."]

    async def _create(**_kwargs):
        return _AsyncChunkIter(chunks)

    client = MagicMock()
    client.chat.completions.create = _create
    return client


# ── Fake game library data ─────────────────────────────────────────────────────
# Three games with varied playtime and ratings, covering all the game dict fields
# that the web API returns. Used as the default library in most web API tests.

FAKE_GAMES = [
    {
        "name":             "The Witcher 3",
        "platform":         "steam",
        "app_id":           "292030",
        "playtime_minutes": 7200,    # 120 hours — clearly "most played"
        "last_played":      "1700000000",
        "release_year":     "~",     # Steam XML feed has no release dates; RAWG fills this in
        "rawg_rating":      4.7,
        "metacritic":       93,
        "genres":           ["RPG", "Adventure"],
        "tags":             ["Open World", "Story Rich", "Fantasy"],
        "gog_rating":       "~",     # Not a GOG game
    },
    {
        "name":             "Hades",
        "platform":         "steam",
        "app_id":           "1145360",
        "playtime_minutes": 2700,    # 45 hours — "played"
        "last_played":      "1690000000",
        "release_year":     2020,
        "rawg_rating":      4.5,
        "metacritic":       93,
        "genres":           ["Roguelite", "Action"],
        "tags":             ["Roguelike", "Hack and Slash"],
        "gog_rating":       "~",
    },
    {
        "name":             "Disco Elysium",
        "platform":         "steam",
        "app_id":           "632470",
        "playtime_minutes": 0,       # 0 minutes — "unplayed"
        "last_played":      None,
        "release_year":     2019,
        "rawg_rating":      4.8,
        "metacritic":       97,
        "genres":           ["RPG"],
        "tags":             ["Story Rich", "Detective", "Choices Matter"],
        "gog_rating":       "~",
    },
]


# ── SSE parsing helper ─────────────────────────────────────────────────────────

def parse_sse(body: str) -> list[dict]:
    """Parse a Server-Sent Events response body into a list of event data dicts.

    SSE format (each event):
        data: <json_string>\n\n

    This helper strips the "data: " prefix, JSON-parses each line, and returns
    all successfully parsed events as a flat list. Non-data lines (comments,
    empty lines) are silently ignored.

    Args:
        body: The raw text body of an SSE response (e.g. from TestClient.get(...).text).

    Returns:
        List of dicts, one per successfully parsed SSE data event.

    Example:
        events = parse_sse(resp.text)
        assert any(e.get("done") is True for e in events)
        text = "".join(e["text"] for e in events if "text" in e)
    """
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))  # Strip the "data: " prefix
            except json.JSONDecodeError:
                pass  # Skip malformed lines (shouldn't happen in normal operation)
    return events
