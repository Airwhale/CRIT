"""
Manual import for Epic Games, GOG, and other platforms.

The web app supports live OAuth flows for Epic and GOG, but the CLI has no
browser integration. This module lets CLI users export their library as a
simple JSON file and import it into the recommendation pipeline.

The JSON format is intentionally minimal — just a list of game objects with
a required "name" field and optional "playtime_minutes" and "last_played":

[
  {
    "name": "Game Title",
    "playtime_minutes": 120,   // optional — defaults to 0
    "last_played": "2024-01-15" // optional, ISO date
  },
  ...
]

Use `--init-epic` or `--init-gog` in the CLI to generate an example file.
"""

import json
from pathlib import Path
from .models import Game


def load_from_json(file_path: str, platform: str) -> list[Game]:
    """Load a game library from a user-supplied JSON file.

    Validates the structure strictly so errors are caught at load time with
    clear messages, rather than surfacing as confusing TypeErrors later.

    Args:
        file_path: Path to the JSON file (relative or absolute).
        platform: Platform label to attach to each Game, e.g. "epic", "gog", "other".
                  Stored as lowercase for consistency with the steam/epic/gog values.

    Returns:
        List of Game objects sorted by playtime_minutes descending.
        Games without a playtime_minutes field default to 0 (treated as unplayed).

    Raises:
        FileNotFoundError: If the file does not exist at the given path.
        ValueError: If the JSON is malformed, the top-level value is not a list,
                    or any entry is missing the required "name" field.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Library file not found: {file_path}")

    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            # Re-raise as ValueError with the file path for clearer error messages
            raise ValueError(f"Invalid JSON in {file_path}: {e}")

    # The top-level value must be a JSON array — not an object or primitive
    if not isinstance(data, list):
        raise ValueError(
            f"Expected a JSON array of games in {file_path}, got {type(data).__name__}"
        )

    games = []
    for i, entry in enumerate(data):
        # Each entry must be an object (dict), not a string or number
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {i} in {file_path} is not an object")

        # "name" is the only required field — without it we can't identify the game
        if "name" not in entry:
            raise ValueError(f"Entry {i} in {file_path} is missing required 'name' field")

        games.append(Game(
            name=entry["name"],
            platform=platform.lower(),  # Normalize to lowercase for consistency
            # int() cast handles the case where JSON provides a float like 120.0
            playtime_minutes=int(entry.get("playtime_minutes", 0)),
            last_played=entry.get("last_played"),  # Optional ISO date string, e.g. "2024-03-01"
            # app_id is not included in the manual format — we have no reliable
            # cross-platform identifier to use, so it defaults to None
        ))

    # Sort most-played first, matching the ordering used for Steam libraries
    return sorted(games, key=lambda g: g.playtime_minutes, reverse=True)


def create_example_json(output_path: str, platform: str = "epic") -> None:
    """Write an example library JSON file the user can fill in.

    Called by the CLI's `--init-epic` and `--init-gog` flags. The example
    contains three well-known games with plausible playtime values so users
    can see the expected format before editing.

    Args:
        output_path: Where to write the example file (will be created or overwritten).
        platform: Name shown in the printed instructions ("epic", "gog", etc.).
    """
    # Three recognizable games with varied playtime to illustrate the format
    example = [
        {"name": "Fortnite",      "playtime_minutes": 300,  "last_played": "2024-03-01"},
        {"name": "Rocket League", "playtime_minutes": 1200, "last_played": "2024-02-14"},
        {"name": "Control",       "playtime_minutes": 600},   # last_played is optional
    ]
    with open(output_path, "w") as f:
        json.dump(example, f, indent=2)

    print(f"Example {platform} library written to: {output_path}")
    print(f"Edit the file with your actual games, then re-run with --{platform}-library <path>")
