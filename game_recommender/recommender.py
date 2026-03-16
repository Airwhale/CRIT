"""
LLM-powered game recommender using Claude.

Takes the user's library (with playtime and optional RAWG ratings data),
then asks Claude to recommend what to play next. The web app also uses
Claude directly via AsyncAnthropic, so this module is primarily
used by the CLI. Both paths use the same model and prompt structure.

Default model is claude-sonnet-4-6 (fast, cheap). Users can switch to
Opus and/or enable extended thinking via the web UI toggles.
"""

import os
import anthropic
from typing import Optional
from .models import GameWithRating


def _format_library_for_prompt(games: list[GameWithRating], max_games: int = 100) -> str:
    """Serialize a game library into a compact, human-readable text block for the LLM prompt.

    Each game becomes one line with as much context as available:
      - Name and platform (always present)
      - Playtime in hours, or "unplayed" (always present)
      - RAWG rating out of 5 (if enriched and non-null)
      - Metacritic score out of 100 (if enriched and non-null)
      - Up to 3 genres (if enriched; capped to avoid prompt bloat)

    The result is embedded directly in the prompt so Claude can reason about
    the user's play history without additional tool calls.

    Args:
        games: Library of GameWithRating objects, typically sorted by playtime.
        max_games: Maximum number of games to include. Caps prompt length to stay
                   within the model's context window and keep inference fast.

    Returns:
        Multi-line string, one game per line.
    """
    lines = []
    for gwr in games[:max_games]:
        game   = gwr.game
        rating = gwr.rating

        # Base: "- Game Name (PLATFORM) | Xh played" (or "unplayed")
        line = f"- {game.name} ({game.platform.upper()})"
        if game.playtime_hours > 0:
            line += f" | {game.playtime_hours}h played"
        else:
            line += " | unplayed"

        # Append ratings metadata when available — gives Claude objective quality signals
        if rating:
            if rating.rawg_rating:
                line += f" | RAWG: {rating.rawg_rating:.1f}/5"
            if rating.metacritic_score:
                line += f" | Metacritic: {rating.metacritic_score}/100"
            if rating.genres:
                # Show at most 3 genres to keep lines readable
                line += f" | Genres: {', '.join(rating.genres[:3])}"

        lines.append(line)

    return "\n".join(lines)


def get_recommendations(
    games: list[GameWithRating],
    user_preferences: Optional[str] = None,
    num_recommendations: int = 5,
    api_key: Optional[str] = None,
    stream_output: bool = True,
    model: str = "claude-sonnet-4-6",
    use_thinking: bool = False,
) -> str:
    """Ask Claude to recommend games from the user's library.

    Builds a structured prompt that includes:
      - The full formatted library (up to 100 games)
      - Library statistics (total/played/unplayed counts)
      - Optional freeform user preferences/mood
      - A detailed format specification for Claude's output

    Args:
        games: List of GameWithRating objects (the user's library with ratings).
               Should be pre-sorted by playtime so most-played appear first in the prompt.
        user_preferences: Optional free-text from the user describing their current
                          mood or desires, e.g. "something short I can finish this week".
        num_recommendations: How many games Claude should recommend. Passed as a
                             literal number in the prompt so Claude counts precisely.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        stream_output: If True, print each text chunk to stdout as it arrives and
                       return the joined string. If False, wait for the full response
                       (used in tests and non-interactive contexts).

    Returns:
        The complete recommendation text from Claude as a single string.

    Raises:
        ValueError: If no Anthropic API key is available.
        anthropic.APIError and subclasses: On API failures (rate limits, auth, etc.).
    """
    # Resolve API key — explicit argument wins over environment variable
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "Anthropic API key is required. Set ANTHROPIC_API_KEY in your .env file.\n"
            "Get one at: https://console.anthropic.com"
        )

    client = anthropic.Anthropic(api_key=api_key)

    # Format the library into the text block that goes into the prompt
    library_text = _format_library_for_prompt(games)

    # Compute library stats for the prompt context
    played   = [g for g in games if g.game.playtime_minutes > 0]
    unplayed = [g for g in games if g.game.playtime_minutes == 0]

    # Only include the preferences section if the user provided one —
    # an empty section would be confusing and wastes tokens.
    preferences_section = ""
    if user_preferences:
        preferences_section = f"\n\n**User's current preferences / mood:**\n{user_preferences}"

    # The prompt is designed to elicit structured, personalized output:
    # - "exactly N games" prevents Claude from over- or under-delivering
    # - The four sub-bullets give Claude a consistent format to fill in
    # - "Only recommend games from their library" prevents hallucination of
    #   games the user doesn't own (a common failure mode for recommendation prompts)
    prompt = f"""You are a knowledgeable gaming advisor helping a player decide what to play next from their existing library.

Here is the player's game library with playtime and ratings data:

{library_text}

**Library stats:**
- Total games: {len(games)}
- Played games: {len(played)}
- Unplayed games: {len(unplayed)}
{preferences_section}

Please recommend exactly {num_recommendations} games from their library to play next. For each recommendation:

1. **Game Name** (Platform) — *[Status: X hours played OR unplayed]*
   - **Why play this now:** A compelling 2-3 sentence reason tailored to their play history and the game's strengths
   - **Ratings:** Mention the RAWG and/or Metacritic scores if available, and what they mean in context
   - **Best for:** What kind of mood or session length this suits (e.g., "short burst", "deep dive weekend", "chill evening")
   - **Similar to:** If they've played similar games in their library, mention them

End with a brief 2-3 sentence overall note about patterns you notice in their library or gaming habits.

Be specific, enthusiastic, and personalized to their actual library. Don't recommend games outside their library."""

    # Build common kwargs for the API call
    api_kwargs = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_thinking:
        api_kwargs["thinking"] = {"type": "adaptive"}

    if stream_output:
        # Stream mode: print each text chunk as it arrives so the CLI shows
        # a live response rather than waiting for the full generation.
        result_parts = []
        with client.messages.stream(**api_kwargs) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)  # flush=True ensures real-time output
                result_parts.append(text)
        print()  # Add a trailing newline after the streamed output
        return "".join(result_parts)
    else:
        # Non-streaming mode: wait for the complete response before returning.
        # Used when streaming to stdout is not desired (e.g. piping to a file).
        response = client.messages.create(**api_kwargs)
        # Response content is a list of blocks (text + optional thinking blocks).
        # We find the first text block and return it, ignoring thinking blocks.
        return next(
            (block.text for block in response.content if block.type == "text"), ""
        )
