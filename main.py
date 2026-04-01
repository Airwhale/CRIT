#!/usr/bin/env python3
"""
CRIT — Curated Recommendations In Titles — CLI entry point.

This module wires together all the game_recommender sub-modules into a
single command-line workflow:
  1. Load games from Steam (via API) and/or manual JSON files
  2. Sort by playtime (most-played first)
  3. Optionally enrich top N games with RAWG ratings
  4. Stream Claude's recommendations to stdout

Usage:
  python main.py --help
  python main.py                          # Steam only (reads from .env)
  python main.py --epic-library epic.json
  python main.py --gog-library gog.json
  python main.py --no-ratings             # Skip RAWG lookup (faster)
  python main.py --top 10                 # Show top 10 most-played first
  python main.py --preferences "I want something short and relaxing"
  python main.py --count 3               # Ask for 3 recommendations
  python main.py --init-epic             # Create a sample epic.json to fill in
"""

import sys
import os
import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

# Load .env file before reading any environment variables.
# This must happen at module level so env vars are available to sub-modules
# that read them at import time (e.g. inside os.environ.get() calls).
load_dotenv()

console = Console()


@click.command()
@click.option(
    "--epic-library",
    metavar="FILE",
    help="Path to your Epic Games library JSON file.",
)
@click.option(
    "--gog-library",
    metavar="FILE",
    help="Path to your GOG library JSON file.",
)
@click.option(
    "--other-library",
    metavar="FILE",
    help="Path to any other platform library JSON file.",
)
@click.option(
    "--no-steam",
    is_flag=True,
    default=False,
    help="Skip Steam library (useful if you only want manual imports).",
)
@click.option(
    "--no-ratings",
    is_flag=True,
    default=False,
    help="Skip RAWG ratings lookup (faster, but less context for recommendations).",
)
@click.option(
    "--top",
    default=50,
    show_default=True,
    metavar="N",
    help="Only send the top N games (by playtime) to the LLM to keep prompt size manageable.",
)
@click.option(
    "--count",
    default=5,
    show_default=True,
    metavar="N",
    help="Number of game recommendations to request.",
)
@click.option(
    "--preferences",
    metavar="TEXT",
    help='Your current gaming mood, e.g. "something relaxing" or "short session".',
)
@click.option(
    "--init-epic",
    is_flag=True,
    default=False,
    help="Create a sample epic.json template and exit.",
)
@click.option(
    "--init-gog",
    is_flag=True,
    default=False,
    help="Create a sample gog.json template and exit.",
)
@click.option(
    "--list-library",
    is_flag=True,
    default=False,
    help="Print your combined library table and exit (no recommendations).",
)
def main(
    epic_library,
    gog_library,
    other_library,
    no_steam,
    no_ratings,
    top,
    count,
    preferences,
    init_epic,
    init_gog,
    list_library,
):
    """
    \b
    CRIT — Curated Recommendations In Titles
    ─────────────────────────────────────────
    Pulls your game libraries from Steam, Epic, and GOG,
    enriches them with ratings data from RAWG, and uses
    Claude AI to recommend what you should play next.

    \b
    Quick setup:
      1. Copy .env.example to .env and fill in your API keys
      2. Run: python main.py
    """

    # ── Template generation (early exits) ──────────────────────────────────────
    # These flags just write a sample JSON file and exit — no API calls needed.
    if init_epic:
        from game_recommender.manual_import import create_example_json
        create_example_json("epic.json", "epic")
        return
    if init_gog:
        from game_recommender.manual_import import create_example_json
        create_example_json("gog.json", "gog")
        return

    all_games = []

    # ── Steam ──────────────────────────────────────────────────────────────────
    if not no_steam:
        steam_key = os.environ.get("STEAM_API_KEY")
        steam_id  = os.environ.get("STEAM_USER_ID")
        if not steam_key or not steam_id:
            # Warn but don't crash — the user may have only Epic/GOG games
            console.print(
                "[yellow]⚠ Steam API key or user ID not set — skipping Steam.[/yellow]\n"
                "  Set STEAM_API_KEY and STEAM_USER_ID in your .env file."
            )
        else:
            with console.status("[cyan]Fetching Steam library...[/cyan]"):
                try:
                    from game_recommender.steam import get_steam_library
                    steam_games = get_steam_library(steam_key, steam_id)
                    console.print(f"[green]✓ Steam:[/green] {len(steam_games)} games loaded.")
                    all_games.extend(steam_games)
                except Exception as e:
                    # Steam errors are non-fatal — the user may still have
                    # Epic/GOG games to fall back on
                    console.print(f"[red]✗ Steam error:[/red] {e}")

    # ── Epic / GOG / Other (manual JSON imports) ───────────────────────────────
    from game_recommender.manual_import import load_from_json

    # Process each platform's file if a path was provided.
    # Using a loop avoids repeating the try/except for each platform.
    for path, platform in [
        (epic_library, "epic"),
        (gog_library,  "gog"),
        (other_library, "other"),
    ]:
        if path:
            try:
                games = load_from_json(path, platform)
                console.print(
                    f"[green]✓ {platform.upper()}:[/green] {len(games)} games loaded from {path}."
                )
                all_games.extend(games)
            except Exception as e:
                console.print(f"[red]✗ {platform.upper()} error:[/red] {e}")

    # If nothing loaded from any source, there's nothing to recommend
    if not all_games:
        console.print(
            "\n[bold red]No games loaded.[/bold red] "
            "Check your .env file or pass --epic-library / --gog-library."
        )
        sys.exit(1)

    console.print(f"\n[bold]Total library: {len(all_games)} games across all platforms.[/bold]")

    # ── Sort & trim ────────────────────────────────────────────────────────────
    # Sort the combined multi-platform library by playtime descending.
    # The LLM sees this order, so most-played games get more "weight" in context.
    all_games.sort(key=lambda g: g.playtime_minutes, reverse=True)

    # ── Optional: show library table and exit ──────────────────────────────────
    if list_library:
        _print_library_table(all_games, max_rows=top)
        return

    # ── Enrich with RAWG ratings ───────────────────────────────────────────────
    rawg_key = os.environ.get("RAWG_API_KEY")
    games_for_llm = []

    if no_ratings or not rawg_key:
        # Skip enrichment: wrap bare Game objects in GameWithRating with rating=None
        if not no_ratings and not rawg_key:
            console.print(
                "[yellow]⚠ RAWG_API_KEY not set — skipping ratings enrichment.[/yellow]"
            )
        from game_recommender.models import GameWithRating
        games_for_llm = [GameWithRating(game=g) for g in all_games[:top]]
    else:
        from game_recommender.ratings import enrich_games
        games_to_enrich = all_games[:top]  # Only enrich the top N by playtime
        console.print(
            f"\n[cyan]Fetching ratings for {len(games_to_enrich)} games from RAWG...[/cyan] "
            "(this may take a moment)"
        )
        # Rich Progress shows a spinner and the current game name while enriching
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("", total=len(games_to_enrich))

            def on_progress(current, total, name):
                # Update the task description to show which game is being fetched
                progress.update(task, completed=current, description=f"[cyan]{name}[/cyan]")

            games_for_llm = enrich_games(
                games_to_enrich,
                api_key=rawg_key,
                progress_callback=on_progress,
            )

        rated = sum(1 for g in games_for_llm if g.rating is not None)
        console.print(f"[green]✓ Ratings found for {rated}/{len(games_for_llm)} games.[/green]")

    # ── Get recommendations from Claude ────────────────────────────────────────
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        console.print(
            "\n[bold red]ANTHROPIC_API_KEY not set.[/bold red] "
            "Set it in your .env file to get AI recommendations."
        )
        sys.exit(1)

    console.print(
        Panel(
            f"Asking Claude to recommend [bold]{count}[/bold] games from your library of "
            f"[bold]{len(games_for_llm)}[/bold] titles...",
            title="[bold blue]AI Recommendations[/bold blue]",
            expand=False,
        )
    )

    from game_recommender.recommender import get_recommendations

    try:
        # stream_output=True prints Claude's response to stdout in real time
        get_recommendations(
            games=games_for_llm,
            user_preferences=preferences,
            num_recommendations=count,
            api_key=anthropic_key,
            stream_output=True,
        )
    except Exception as e:
        console.print(f"\n[red]Recommendation error:[/red] {e}")
        sys.exit(1)


def _print_library_table(games, max_rows: int = 50):
    """Print a Rich formatted table of the user's combined game library.

    Called when --list-library is passed. Useful for verifying that all
    platforms loaded correctly before running the full recommendation pipeline.

    Args:
        games: Full sorted list of Game objects.
        max_rows: How many rows to display before showing a truncation message.
    """
    table = Table(title="Your Game Library", show_lines=False)
    table.add_column("#",        style="dim", width=4)
    table.add_column("Game",     style="bold")
    table.add_column("Platform", style="cyan")
    table.add_column("Playtime", justify="right")

    for i, game in enumerate(games[:max_rows], 1):
        # Show hours for played games, "unplayed" for 0-minute games
        playtime = f"{game.playtime_hours}h" if game.playtime_minutes > 0 else "unplayed"
        table.add_row(str(i), game.name, game.platform.upper(), playtime)

    if len(games) > max_rows:
        # Add a footer row indicating how many games were omitted
        table.add_row("...", f"...and {len(games) - max_rows} more", "", "")

    console.print(table)


if __name__ == "__main__":
    main()


# Re-export the ASGI app so `uvicorn main:app` works for deployment.
# The CLI entry point above is unaffected — Click only executes when
# this file is run directly, not when it's imported by uvicorn.
from web.app import app  # noqa: E402, F401
