"""
Manual import for Epic Games, GOG, and other platforms.

Since Epic and GOG don't have public APIs, users export their library
as a JSON file and we parse it here.

Expected JSON format (list of games):
[
  {
    "name": "Game Title",
    "playtime_minutes": 120,   // optional
    "last_played": "2024-01-15" // optional, ISO date
  },
  ...
]
"""

import json
from pathlib import Path
from .models import Game


def load_from_json(file_path: str, platform: str) -> list[Game]:
    """
    Load a game library from a JSON file.

    Args:
        file_path: Path to the JSON file.
        platform: Platform name (e.g. "epic", "gog", "other").

    Returns:
        List of Game objects.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the JSON format is invalid.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Library file not found: {file_path}")

    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {file_path}: {e}")

    if not isinstance(data, list):
        raise ValueError(
            f"Expected a JSON array of games in {file_path}, got {type(data).__name__}"
        )

    games = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {i} in {file_path} is not an object")
        if "name" not in entry:
            raise ValueError(f"Entry {i} in {file_path} is missing required 'name' field")

        games.append(Game(
            name=entry["name"],
            platform=platform.lower(),
            playtime_minutes=int(entry.get("playtime_minutes", 0)),
            last_played=entry.get("last_played"),
        ))

    return sorted(games, key=lambda g: g.playtime_minutes, reverse=True)


def create_example_json(output_path: str, platform: str = "epic") -> None:
    """Write an example library JSON file the user can fill in."""
    example = [
        {"name": "Fortnite", "playtime_minutes": 300, "last_played": "2024-03-01"},
        {"name": "Rocket League", "playtime_minutes": 1200, "last_played": "2024-02-14"},
        {"name": "Control", "playtime_minutes": 600},
    ]
    with open(output_path, "w") as f:
        json.dump(example, f, indent=2)
    print(f"Example {platform} library written to: {output_path}")
    print("Edit the file with your actual games, then re-run with --{platform}-library <path>")
