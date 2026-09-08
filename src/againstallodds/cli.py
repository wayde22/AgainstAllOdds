"""Command-line interface for the AgainstAllOdds rating model."""

from __future__ import annotations

import argparse
import logging
import sys
import json
import sqlite3
import subprocess
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
        help="Folder for manual JSON experiments, NFL analytics, and model data.",
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

    sync = subparsers.add_parser("sync-nfl", help="Download and retain real NFL data.")
    sync.add_argument("--start-season", type=int, default=2015)
    sync.add_argument("--end-season", type=int, default=2026)
    subparsers.add_parser("backtest", help="Evaluate the imported NFL baseline.")
    predictions = subparsers.add_parser("predict-week", help="Save predictions for the next seven days.")
    predictions.add_argument("--model", choices=["power-rating-v1", "ridge-v1", "boosted-v1", "all"], default="power-rating-v1")
    predictions.add_argument("--family", choices=["raw", "rolling-adjusted", "srs"], default="raw")
    stats = subparsers.add_parser("sync-stats", help="Import seasonal play-by-play statistics.")
    stats.add_argument("--start-season", type=int, default=2015)
    stats.add_argument("--end-season", type=int, default=2026)
    stats.add_argument("--refresh-history", action="store_true")
    comparison = subparsers.add_parser("compare-models", help="Run a frozen three-model experiment.")
    comparison.add_argument("--family", choices=["raw", "rolling-adjusted", "srs"], default="raw")
    subparsers.add_parser("sync-odds", help="Fetch and retain current NFL market spreads from the configured provider.")
    subparsers.add_parser("sync-weather", help="Save forecast-time venue weather snapshots for upcoming games.")
    subparsers.add_parser("plan-market-checks", help="Save the 72-hour, hourly, and final market-check windows.")
    subparsers.add_parser("run-due-market-checks", help="Fetch market odds when a saved market-check window is due.")
    subparsers.add_parser("enable-windows-market-checks", help="Register the opt-in Windows background market checker.")
    odds_import = subparsers.add_parser("import-odds", help="Import reviewed bookmaker spreads from a local CSV.")
    odds_import.add_argument("--file", type=Path, required=True, help="CSV with home_team, away_team, bookmaker, and home_spread columns.")
    injuries = subparsers.add_parser("sync-injuries", help="Save the current official NFL injury or inactive report.")
    injuries.add_argument("--source", choices=["injuries", "inactives"], default="injuries")
    subparsers.add_parser("build-qb-profiles", help="Build rolling quarterback performance profiles from retained play data.")
    subparsers.add_parser("build-wr-profiles", help="Build rolling wide receiver performance profiles from retained play data.")
    subparsers.add_parser("build-rb-te-profiles", help="Build role-based running back and tight end profiles from retained play data.")
    subparsers.add_parser("build-edge-profiles", help="Build rolling EDGE/pass-rusher disruption profiles from retained play data.")
    injury_predictions = subparsers.add_parser("predict-with-availability", help="Save prospective forecasts with the current QB availability adjustment.")
    injury_predictions.add_argument("--model", choices=["power-rating-v1", "ridge-v1", "boosted-v1", "all"], default="all")
    override = subparsers.add_parser("set-expected-qb", help="Set a reviewable expected-QB override for one game.")
    override.add_argument("--game-id", required=True)
    override.add_argument("--team", required=True)
    override.add_argument("--player", required=True)
    override.add_argument("--reason", default="")
    subparsers.add_parser("run-due-injury-checks", help="Run planned official-report checks that are due now.")
    subparsers.add_parser("enable-windows-injury-checks", help="Register the opt-in Windows background injury checker.")
    subparsers.add_parser("dashboard", help="Open the local NFL analytics dashboard.")

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
        if args.command == "dashboard":
            app = Path(__file__).with_name("dashboard.py")
            return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.address", "127.0.0.1", "--browser.gatherUsageStats", "false", "--", "--data-dir", str(args.data_dir.resolve())])
        if args.command in {"sync-nfl", "backtest", "predict-week", "sync-stats", "compare-models", "sync-odds", "sync-weather", "plan-market-checks", "run-due-market-checks", "enable-windows-market-checks", "import-odds", "sync-injuries", "build-qb-profiles", "build-wr-profiles", "build-rb-te-profiles", "build-edge-profiles", "predict-with-availability", "set-expected-qb", "run-due-injury-checks", "enable-windows-injury-checks"}:
            from againstallodds.analytics_store import AnalyticsStore
            from againstallodds.analytics import backtest, sync_nfl, upcoming
            analytics_store = AnalyticsStore(args.data_dir)
            if args.command == "run-due-injury-checks":
                from againstallodds.injuries import run_due_injury_checks
                result = run_due_injury_checks(analytics_store)
            elif args.command == "enable-windows-injury-checks":
                from againstallodds.injuries import register_windows_runner
                result = register_windows_runner(args.data_dir)
            elif args.command == "sync-injuries":
                from againstallodds.injuries import sync_injuries
                result = sync_injuries(analytics_store, source=args.source)
            elif args.command == "build-qb-profiles":
                from againstallodds.injuries import build_qb_profiles
                result = build_qb_profiles(analytics_store)
            elif args.command == "build-wr-profiles":
                from againstallodds.injuries import build_wr_profiles
                result = build_wr_profiles(analytics_store)
            elif args.command == "build-rb-te-profiles":
                from againstallodds.injuries import build_rb_te_profiles
                result = build_rb_te_profiles(analytics_store)
            elif args.command == "build-edge-profiles":
                from againstallodds.injuries import build_edge_profiles
                result = build_edge_profiles(analytics_store)
            elif args.command == "set-expected-qb":
                from againstallodds.injuries import AvailabilityStore
                from againstallodds.nfl_data import TEAMS, utcnow
                availability = AvailabilityStore(analytics_store)
                team = TEAMS.get(args.team.upper(), args.team)
                availability.override(args.game_id, team, None, args.player, args.reason, utcnow())
                result = {"game_id": args.game_id, "team": team, "expected_qb": args.player, "saved": True}
            elif args.command == "predict-with-availability":
                from againstallodds.experiments import predict_models
                from againstallodds.injuries import injury_adjusted_predictions
                result = injury_adjusted_predictions(analytics_store, predict_models(analytics_store, save=True, model=args.model))
            elif args.command == "sync-stats":
                from againstallodds.nfl_stats import sync_stats
                result = sync_stats(analytics_store, args.start_season, args.end_season, refresh_history=args.refresh_history, progress=lambda message: print(message, file=sys.stderr, flush=True))
                print(json.dumps(result, indent=2))
                return 2 if any(r["status"] == "failed" for r in result) else 0
            elif args.command == "compare-models":
                from againstallodds.experiments import compare_models
                result = compare_models(analytics_store, family=args.family, progress=lambda message: print(message, file=sys.stderr, flush=True))
                result = {k: result[k] for k in ("experiment_id", "settings", "summary", "coverage")}
            elif args.command == "sync-odds":
                from againstallodds.odds import sync_odds
                result = sync_odds(analytics_store)
            elif args.command == "sync-weather":
                from againstallodds.weather import sync_weather
                result = sync_weather(analytics_store)
            elif args.command == "plan-market-checks":
                from againstallodds.odds import market_check_plan
                result = market_check_plan(analytics_store)
            elif args.command == "run-due-market-checks":
                from againstallodds.odds import run_due_market_checks
                result = run_due_market_checks(analytics_store)
            elif args.command == "enable-windows-market-checks":
                from againstallodds.odds import register_windows_market_runner
                result = register_windows_market_runner(args.data_dir)
            elif args.command == "import-odds":
                from againstallodds.odds import import_odds
                result = import_odds(analytics_store, args.file.read_bytes())
            elif args.command == "sync-nfl":
                result = sync_nfl(analytics_store, args.start_season, args.end_season)
            elif args.command == "backtest":
                result = backtest(analytics_store)["summary"]
            else:
                if not analytics_store.latest():
                    raise AgainstAllOddsError("Import NFL data first with sync-nfl.")
                if args.model == "power-rating-v1":
                    result = upcoming(analytics_store, save=True)
                else:
                    from againstallodds.experiments import predict_models
                    result = predict_models(analytics_store, save=True, model=args.model, family=args.family)
            print(json.dumps(result, indent=2))
            return 0
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
    except (AgainstAllOddsError, OSError, sqlite3.Error) as error:
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
