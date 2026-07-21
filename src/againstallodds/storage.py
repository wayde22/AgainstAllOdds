"""JSON persistence for ratings and game history."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from againstallodds.exceptions import StorageError
from againstallodds.models import GameRecord
from againstallodds.ratings import game_record_from_dict, game_record_to_dict
from againstallodds.teams import initial_ratings


LOGGER = logging.getLogger(__name__)


class JsonStore:
    """Persist ratings and history as readable JSON files."""

    def __init__(self, data_dir: Path | str = "data") -> None:
        self.data_dir = Path(data_dir)
        self.ratings_path = self.data_dir / "ratings.json"
        self.history_path = self.data_dir / "game_history.json"

    def initialize(self, *, force: bool = False) -> None:
        """Create storage files while preserving existing data by default."""

        self._ensure_data_dir()
        if force or not self.ratings_path.exists():
            self.save_ratings(initial_ratings())
            LOGGER.info("Initialized ratings at %s", self.ratings_path)
        else:
            ratings = self.load_ratings()
            missing_teams = set(initial_ratings()) - set(ratings)
            if missing_teams:
                ratings.update({team: 0.0 for team in missing_teams})
                self.save_ratings(ratings)
                LOGGER.info("Added missing teams to %s", self.ratings_path)

        if force or not self.history_path.exists():
            self.save_history([])
            LOGGER.info("Initialized history at %s", self.history_path)

    def ensure_initialized(self) -> None:
        """Make sure both JSON files exist before reading from them."""

        self.initialize(force=False)

    def load_ratings(self) -> dict[str, float]:
        """Load team ratings from JSON."""

        try:
            with self.ratings_path.open("r", encoding="utf-8") as file:
                raw_ratings = json.load(file)
        except FileNotFoundError:
            raise StorageError(
                "Ratings have not been initialized yet. Run "
                "`python main.py initialize` first."
            ) from None
        except OSError as error:
            raise StorageError(f"Could not read ratings file: {error}") from error
        except json.JSONDecodeError as error:
            raise StorageError(f"Could not read ratings JSON: {error}") from error

        if not isinstance(raw_ratings, dict):
            raise StorageError("ratings.json must contain an object of team ratings.")

        return {str(team): float(rating) for team, rating in raw_ratings.items()}

    def save_ratings(self, ratings: dict[str, float]) -> None:
        """Save team ratings to JSON."""

        self._ensure_data_dir()
        try:
            with self.ratings_path.open("w", encoding="utf-8") as file:
                json.dump(ratings, file, indent=2, sort_keys=True)
                file.write("\n")
        except OSError as error:
            raise StorageError(f"Could not save ratings file: {error}") from error

    def load_history(self) -> list[GameRecord]:
        """Load game history from JSON."""

        try:
            with self.history_path.open("r", encoding="utf-8") as file:
                raw_history = json.load(file)
        except FileNotFoundError:
            raise StorageError(
                "Game history has not been initialized yet. Run "
                "`python main.py initialize` first."
            ) from None
        except OSError as error:
            raise StorageError(f"Could not read game history file: {error}") from error
        except json.JSONDecodeError as error:
            raise StorageError(f"Could not read game history JSON: {error}") from error

        if not isinstance(raw_history, list):
            raise StorageError("game_history.json must contain a list of games.")

        return [game_record_from_dict(item) for item in raw_history]

    def save_history(self, history: list[GameRecord]) -> None:
        """Save game history to JSON."""

        self._ensure_data_dir()
        try:
            with self.history_path.open("w", encoding="utf-8") as file:
                json.dump([game_record_to_dict(record) for record in history], file, indent=2)
                file.write("\n")
        except OSError as error:
            raise StorageError(f"Could not save game history file: {error}") from error

    def _ensure_data_dir(self) -> None:
        """Create the data directory or raise a friendly storage error."""

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StorageError(f"Could not create data directory: {error}") from error
