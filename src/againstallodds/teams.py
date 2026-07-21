"""NFL team names used by the first version of the rating model."""

from __future__ import annotations

from difflib import get_close_matches

from againstallodds.exceptions import ValidationError


NFL_TEAMS: tuple[str, ...] = (
    "Arizona Cardinals",
    "Atlanta Falcons",
    "Baltimore Ravens",
    "Buffalo Bills",
    "Carolina Panthers",
    "Chicago Bears",
    "Cincinnati Bengals",
    "Cleveland Browns",
    "Dallas Cowboys",
    "Denver Broncos",
    "Detroit Lions",
    "Green Bay Packers",
    "Houston Texans",
    "Indianapolis Colts",
    "Jacksonville Jaguars",
    "Kansas City Chiefs",
    "Las Vegas Raiders",
    "Los Angeles Chargers",
    "Los Angeles Rams",
    "Miami Dolphins",
    "Minnesota Vikings",
    "New England Patriots",
    "New Orleans Saints",
    "New York Giants",
    "New York Jets",
    "Philadelphia Eagles",
    "Pittsburgh Steelers",
    "San Francisco 49ers",
    "Seattle Seahawks",
    "Tampa Bay Buccaneers",
    "Tennessee Titans",
    "Washington Commanders",
)


def initial_ratings() -> dict[str, float]:
    """Return all 32 NFL teams at a starting rating of 0.0."""

    return {team: 0.0 for team in NFL_TEAMS}


def normalize_team_name(team_name: str) -> str:
    """Validate and normalize a team name to the official project spelling."""

    cleaned_name = " ".join(team_name.strip().split())
    if not cleaned_name:
        raise ValidationError("Team name cannot be blank.")

    team_lookup = {team.lower(): team for team in NFL_TEAMS}
    normalized = team_lookup.get(cleaned_name.lower())
    if normalized is not None:
        return normalized

    close_matches = get_close_matches(cleaned_name, NFL_TEAMS, n=3, cutoff=0.5)
    suggestion = ""
    if close_matches:
        suggestion = f" Did you mean: {', '.join(close_matches)}?"

    raise ValidationError(
        f"Unknown NFL team '{team_name}'. Use the full team name, like "
        f"'Detroit Lions'.{suggestion}"
    )
