"""Dataclasses used by the power-rating system."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompletedGame:
    """A final score ready to be applied to the ratings."""

    game_id: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    neutral_site: bool = False
    k_factor: float = 0.20
    home_field_advantage: float = 2.0
    played_on: str | None = None


@dataclass(frozen=True, slots=True)
class Projection:
    """A projected point spread for a matchup."""

    home_team: str
    away_team: str
    expected_margin: float
    neutral_site: bool
    home_field_advantage: float

    @property
    def favorite(self) -> str:
        """Return the team currently projected to be favored."""

        if self.expected_margin > 0:
            return self.home_team
        if self.expected_margin < 0:
            return self.away_team
        return "Pick'em"

    @property
    def spread(self) -> float:
        """Return the absolute point spread."""

        return abs(self.expected_margin)

    @property
    def display_line(self) -> str:
        """Return a friendly sportsbook-style line, such as Lions -2.0."""

        if self.expected_margin == 0:
            return "Pick'em"
        return f"{self.favorite} -{self.spread:.1f}"


@dataclass(frozen=True, slots=True)
class GameRecord:
    """Saved history for one recorded game and its rating movement."""

    game_id: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    neutral_site: bool
    k_factor: float
    home_field_advantage: float
    expected_margin: float
    actual_margin: int
    performance_difference: float
    rating_change: float
    home_rating_after: float
    away_rating_after: float
    played_on: str | None = None
