"""Command-line interface for the AgainstAllOdds rating model."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.models import CompletedGame
from againstallodds.ratings import (
    DEFAULT_HOME_FIELD_ADVANTAGE,
    DEFAULT_K_FACTOR,
    PowerRatingSystem,
    record_completed_game,
)
from againstallodds.storage import JsonStore
from againstallodds.teams import NFL_TEAMS


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the project CLI parser."""

    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Educational NFL power ratings and projected lines.",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        type=Path,
        help="Folder used for ratings and game history JSON files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show extra logging while commands run.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("initialize", help="Create ratings and history files.")
    initialize.add_argument(
        "--force",
        action="store_true",
        help="Reset ratings and history back to a clean 0.0 start.",
    )

    subparsers.add_parser("ratings", help="Show rankings from highest to lowest.")
    subparsers.add_parser("teams", help="List valid NFL team names.")

    project = subparsers.add_parser("project", help="Project a line for an upcoming game.")
    add_matchup_arguments(project)

    record_game = subparsers.add_parser("record-game", help="Record a completed game.")
    record_game.add_argument("--game-id", required=True, help="Unique ID, like 2026-W01-DET-CHI.")
    add_matchup_arguments(record_game)
    record_game.add_argument("--home-score", required=True, type=int, help="Final home-team score.")
    record_game.add_argument("--away-score", required=True, type=int, help="Final away-team score.")
    record_game.add_argument(
        "--k-factor",
        default=DEFAULT_K_FACTOR,
        type=float,
        help=f"Rating reaction speed. Default: {DEFAULT_K_FACTOR}.",
    )
    record_game.add_argument("--played-on", help="Optional date label, like 2026-09-13.")

    history = subparsers.add_parser("history", help="Show recorded games.")
    history.add_argument("--limit", type=int, help="Show only the most recent N games.")

    return parser


def add_matchup_arguments(parser: argparse.ArgumentParser) -> None:
    """Add shared home/away matchup options."""

    parser.add_argument("--home", required=True, help="Full home team name.")
    parser.add_argument("--away", required=True, help="Full away team name.")
    parser.add_argument(
        "--neutral-site",
        action="store_true",
        help="Remove home-field advantage from the projection.",
    )
    parser.add_argument(
        "--home-field",
        default=DEFAULT_HOME_FIELD_ADVANTAGE,
        type=float,
        help=f"Home-field advantage in points. Default: {DEFAULT_HOME_FIELD_ADVANTAGE}.",
    )


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""

    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    store = JsonStore(args.data_dir)

    try:
        if args.command == "initialize":
            return handle_initialize(store, force=args.force)
        if args.command == "ratings":
            return handle_ratings(store)
        if args.command == "teams":
            return handle_teams()
        if args.command == "project":
            return handle_project(store, args)
        if args.command == "record-game":
            return handle_record_game(store, args)
        if args.command == "history":
            return handle_history(store, limit=args.limit)
    except AgainstAllOddsError as error:
        LOGGER.error("%s", error)
        print(f"Error: {error}", file=sys.stderr)
        return 2

    parser.error("Unknown command.")
    return 2


def handle_initialize(store: JsonStore, *, force: bool) -> int:
    """Initialize project storage."""

    store.initialize(force=force)
    action = "Reset" if force else "Initialized"
    print(f"{action} ratings for all 32 NFL teams.")
    print(f"Ratings file: {store.ratings_path}")
    print(f"History file: {store.history_path}")
    return 0


def handle_ratings(store: JsonStore) -> int:
    """Print current rankings."""

    store.ensure_initialized()
    system = PowerRatingSystem(store.load_ratings())
    print("Power ratings")
    for rank, (team, rating) in enumerate(system.rankings(), start=1):
        print(f"{rank:>2}. {team:<24} {rating:>6.2f}")
    return 0


def handle_teams() -> int:
    """Print valid team names."""

    print("Valid NFL teams")
    for team in NFL_TEAMS:
        print(f"- {team}")
    return 0


def handle_project(store: JsonStore, args: argparse.Namespace) -> int:
    """Print a projected line for a matchup."""

    store.ensure_initialized()
    system = PowerRatingSystem(store.load_ratings())
    projection = system.project(
        args.home,
        args.away,
        neutral_site=args.neutral_site,
        home_field_advantage=args.home_field,
    )
    print(f"Projected line: {projection.display_line}")
    print(f"Expected margin for {projection.home_team}: {projection.expected_margin:+.1f}")
    if projection.neutral_site:
        print("Neutral site: yes")
    else:
        print(f"Home-field advantage: {projection.home_field_advantage:.1f} points")
    return 0


def handle_record_game(store: JsonStore, args: argparse.Namespace) -> int:
    """Record a completed game and persist the rating update."""

    store.ensure_initialized()
    game = CompletedGame(
        game_id=args.game_id,
        home_team=args.home,
        away_team=args.away,
        home_score=args.home_score,
        away_score=args.away_score,
        neutral_site=args.neutral_site,
        k_factor=args.k_factor,
        home_field_advantage=args.home_field,
        played_on=args.played_on,
    )
    updated_ratings, updated_history, record = record_completed_game(
        store.load_ratings(),
        store.load_history(),
        game,
    )
    store.save_ratings(updated_ratings)
    store.save_history(updated_history)

    print(f"Recorded game: {record.game_id}")
    print(f"Expected margin: {record.expected_margin:+.1f}")
    print(f"Actual margin: {record.actual_margin:+d}")
    print(f"Rating change: {record.rating_change:+.2f} for {record.home_team}")
    print(f"{record.home_team}: {record.home_rating_after:+.2f}")
    print(f"{record.away_team}: {record.away_rating_after:+.2f}")
    return 0


def handle_history(store: JsonStore, *, limit: int | None) -> int:
    """Print game history."""

    store.ensure_initialized()
    history = store.load_history()
    if limit is not None:
        history = history[-limit:]

    if not history:
        print("No games recorded yet.")
        return 0

    print("Game history")
    for record in history:
        site = "neutral" if record.neutral_site else "home"
        print(
            f"{record.game_id}: {record.away_team} {record.away_score}, "
            f"{record.home_team} {record.home_score} ({site}) | "
            f"expected {record.expected_margin:+.1f}, change {record.rating_change:+.2f}"
        )
    return 0
