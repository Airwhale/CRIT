#!/usr/bin/env python3
"""Launch the Game Recommender web UI."""
import uvicorn

if __name__ == "__main__":
    print("Game Recommender → http://localhost:8000")
    uvicorn.run("web.app:app", host="0.0.0.0", port=8000, reload=True)
