"""Core educational NFL power-rating math."""

from __future__ import annotations

from dataclasses import asdict
from typing import Iterable

from againstallodds.exceptions import DuplicateGameError, ValidationError
from againstallodds.models import CompletedGame, GameRecord, Projection
from againstallodds.teams import initial_ratings, normalize_team_name


DEFAULT_K_FACTOR = 0.20
DEFAULT_HOME_FIELD_ADVANTAGE = 2.0


class PowerRatingSystem:
    """Calculate projected lines and update ratings after completed games."""

    def __init__(self, ratings: dict[str, float] | None = None) -> None:
        self.ratings = initial_ratings()
        if ratings:
            self.ratings.update(ratings)

    def expected_margin(
        self,
        home_team: str,
        away_team: str,
        *,
        neutral_site: bool = False,
        home_field_advantage: float = DEFAULT_HOME_FIELD_ADVANTAGE,
    ) -> float:
        """Return the projected home-team margin before a game is played."""

        home_team = normalize_team_name(home_team)
        away_team = normalize_team_name(away_team)
        validate_matchup(home_team, away_team)
        validate_home_field_advantage(home_field_advantage)

        home_rating = self.ratings[home_team]
        away_rating = self.ratings[away_team]
        home_field_points = 0.0 if neutral_site else home_field_advantage
        return home_rating - away_rating + home_field_points

    def project(
        self,
        home_team: str,
        away_team: str,
        *,
        neutral_site: bool = False,
        home_field_advantage: float = DEFAULT_HOME_FIELD_ADVANTAGE,
    ) -> Projection:
        """Build a projection object for an upcoming matchup."""

        home_team = normalize_team_name(home_team)
        away_team = normalize_team_name(away_team)
        expected_margin = self.expected_margin(
            home_team,
            away_team,
            neutral_site=neutral_site,
            home_field_advantage=home_field_advantage,
        )
        return Projection(
            home_team=home_team,
            away_team=away_team,
            expected_margin=expected_margin,
            neutral_site=neutral_site,
            home_field_advantage=home_field_advantage,
        )

    def record_game(self, game: CompletedGame) -> GameRecord:
        """Apply a completed game and return the saved history record."""

        game = validate_completed_game(game)
        expected_margin = self.expected_margin(
            game.home_team,
            game.away_team,
            neutral_site=game.neutral_site,
            home_field_advantage=game.home_field_advantage,
        )

        actual_margin = game.home_score - game.away_score
        performance_difference = actual_margin - expected_margin

        # The k-factor controls how fast ratings react to new information.
        rating_change = performance_difference * game.k_factor
        self.ratings[game.home_team] += rating_change
        self.ratings[game.away_team] -= rating_change

        return GameRecord(
            game_id=game.game_id,
            home_team=game.home_team,
            away_team=game.away_team,
            home_score=game.home_score,
            away_score=game.away_score,
            neutral_site=game.neutral_site,
            k_factor=game.k_factor,
            home_field_advantage=game.home_field_advantage,
            expected_margin=expected_margin,
            actual_margin=actual_margin,
            performance_difference=performance_difference,
            rating_change=rating_change,
            home_rating_after=self.ratings[game.home_team],
            away_rating_after=self.ratings[game.away_team],
            played_on=game.played_on,
        )

    def rankings(self) -> list[tuple[str, float]]:
        """Return teams sorted from highest to lowest rating."""

        return sorted(self.ratings.items(), key=lambda item: item[1], reverse=True)


def validate_completed_game(game: CompletedGame) -> CompletedGame:
    """Validate and normalize a completed game."""

    game_id = game.game_id.strip()
    if not game_id:
        raise ValidationError("Game ID cannot be blank.")

    home_team = normalize_team_name(game.home_team)
    away_team = normalize_team_name(game.away_team)
    validate_matchup(home_team, away_team)
    validate_score(game.home_score, "home score")
    validate_score(game.away_score, "away score")
    validate_k_factor(game.k_factor)
    validate_home_field_advantage(game.home_field_advantage)

    return CompletedGame(
        game_id=game_id,
        home_team=home_team,
        away_team=away_team,
        home_score=game.home_score,
        away_score=game.away_score,
        neutral_site=game.neutral_site,
        k_factor=game.k_factor,
        home_field_advantage=game.home_field_advantage,
        played_on=game.played_on,
    )


def validate_matchup(home_team: str, away_team: str) -> None:
    """Make sure the same team is not playing itself."""

    if home_team == away_team:
        raise ValidationError("Home team and away team must be different.")


def validate_score(score: int, label: str) -> None:
    """Make sure a score is a non-negative integer."""

    if isinstance(score, bool) or not isinstance(score, int):
        raise ValidationError(f"{label.title()} must be a whole number.")
    if score < 0:
        raise ValidationError(f"{label.title()} cannot be negative.")


def validate_k_factor(k_factor: float) -> None:
    """Keep rating movement in a beginner-friendly range."""

    if k_factor <= 0 or k_factor > 1:
        raise ValidationError("K-factor must be greater than 0 and no more than 1.")


def validate_home_field_advantage(home_field_advantage: float) -> None:
    """Validate home-field advantage points."""

    if home_field_advantage < 0:
        raise ValidationError("Home-field advantage cannot be negative.")


def prevent_duplicate_game(history: Iterable[GameRecord], game_id: str) -> None:
    """Raise an error if a game ID already exists in history."""

    if any(record.game_id == game_id for record in history):
        raise DuplicateGameError(
            f"Game ID '{game_id}' already exists in history. "
            "Use a unique game ID for each completed game."
        )


def record_completed_game(
    ratings: dict[str, float],
    history: list[GameRecord],
    game: CompletedGame,
) -> tuple[dict[str, float], list[GameRecord], GameRecord]:
    """Record a game against saved ratings and history."""

    normalized_game = validate_completed_game(game)
    prevent_duplicate_game(history, normalized_game.game_id)

    system = PowerRatingSystem(ratings)
    record = system.record_game(normalized_game)
    return system.ratings, [*history, record], record


def game_record_to_dict(record: GameRecord) -> dict[str, object]:
    """Convert a game record into a JSON-friendly dictionary."""

    return asdict(record)


def game_record_from_dict(data: dict[str, object]) -> GameRecord:
    """Convert saved JSON data back into a game record."""

    return GameRecord(
        game_id=str(data["game_id"]),
        home_team=str(data["home_team"]),
        away_team=str(data["away_team"]),
        home_score=int(data["home_score"]),
        away_score=int(data["away_score"]),
        neutral_site=bool(data["neutral_site"]),
        k_factor=float(data["k_factor"]),
        home_field_advantage=float(data["home_field_advantage"]),
        expected_margin=float(data["expected_margin"]),
        actual_margin=int(data["actual_margin"]),
        performance_difference=float(data["performance_difference"]),
        rating_change=float(data["rating_change"]),
        home_rating_after=float(data["home_rating_after"]),
        away_rating_after=float(data["away_rating_after"]),
        played_on=None if data.get("played_on") is None else str(data["played_on"]),
    )
