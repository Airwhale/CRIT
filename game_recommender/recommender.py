"""
LLM-powered game recommender using Claude.

Takes the user's library (with playtime) and enriched ratings data,
then asks Claude to recommend what to play next.
"""

import os
import anthropic
from typing import Optional
from .models import GameWithRating


def _format_library_for_prompt(games: list[GameWithRating], max_games: int = 100) -> str:
    """Format the game library into a readable prompt section."""
    lines = []
    for gwr in games[:max_games]:
        game = gwr.game
        rating = gwr.rating

        line = f"- {game.name} ({game.platform.upper()})"
        if game.playtime_hours > 0:
            line += f" | {game.playtime_hours}h played"
        else:
            line += " | unplayed"

        if rating:
            if rating.rawg_rating:
                line += f" | RAWG: {rating.rawg_rating:.1f}/5"
            if rating.metacritic_score:
                line += f" | Metacritic: {rating.metacritic_score}/100"
            if rating.genres:
                line += f" | Genres: {', '.join(rating.genres[:3])}"

        lines.append(line)
    return "\n".join(lines)


def get_recommendations(
    games: list[GameWithRating],
    user_preferences: Optional[str] = None,
    num_recommendations: int = 5,
    api_key: Optional[str] = None,
    stream_output: bool = True,
) -> str:
    """
    Ask Claude to recommend games from the user's library.

    Args:
        games: List of GameWithRating objects (the user's library with ratings).
        user_preferences: Optional freeform text from the user about their mood/preferences.
        num_recommendations: How many games to recommend.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        stream_output: If True, streams the response to stdout as it's generated.

    Returns:
        The full recommendation text from Claude.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "Anthropic API key is required. Set ANTHROPIC_API_KEY in your .env file.\n"
            "Get one at: https://console.anthropic.com"
        )

    client = anthropic.Anthropic(api_key=api_key)

    library_text = _format_library_for_prompt(games)

    # Split into played vs unplayed for better context
    played = [g for g in games if g.game.playtime_minutes > 0]
    unplayed = [g for g in games if g.game.playtime_minutes == 0]

    preferences_section = ""
    if user_preferences:
        preferences_section = f"\n\n**User's current preferences / mood:**\n{user_preferences}"

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

    if stream_output:
        result_parts = []
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                result_parts.append(text)
        print()  # final newline
        return "".join(result_parts)
    else:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
        return next(
            (block.text for block in response.content if block.type == "text"), ""
        )
