"""Pregame features built from prior dates only, with explicit coverage."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from itertools import groupby

from againstallodds.nfl_data import utcnow
from againstallodds.nfl_stats import RATES
from againstallodds.ratings import PowerRatingSystem
from againstallodds.research_store import ResearchStore

FEATURE_VERSION = "pregame-5-16-v2"
FEATURE_FAMILIES = ("raw", "rolling-adjusted", "srs")
WINDOWS = (5, 16)


def team_features(history, aggregates):
    values, reasons = {}, []
    if len(history) < 3:
        reasons.append("fewer than three previous games")
    for window in WINDOWS:
        previous = history[-window:]
        values[f"games_{window}"] = len(previous)
        missing = [g.game_id for g, team in previous if team not in aggregates.get(g.game_id, {})]
        if missing:
            reasons.append(f"missing statistics in last {window} games: {', '.join(missing)}")
        for side in ("off", "def"):
            totals = defaultdict(float)
            for game, team in previous:
                for key, value in aggregates.get(game.game_id, {}).get(team, {}).get(side, {}).items():
                    totals[key] += value
            values[f"{side}_plays_{window}"] = totals["plays"]
            for rate, (numerator, denominator) in RATES.items():
                key = f"{side}_{rate}_{window}"
                values[key] = totals[numerator] / totals[denominator] if totals[denominator] else None
    return values, reasons


def rest_days(history, game):
    prior = [old for old, _ in history if old.season == game.season]
    return min(21, max(0, (date.fromisoformat(game.gameday) - date.fromisoformat(prior[-1].gameday)).days)) if prior else 7


def build_features(games, aggregates, before, family="raw"):
    if family not in FEATURE_FAMILIES:
        raise ValueError(f"Unknown feature family: {family}")
    histories = defaultdict(list)
    opponents = defaultdict(list)
    system = PowerRatingSystem()
    rows = []
    ordered = sorted(games, key=lambda g: (g.gameday, g.game_id))
    for day, group in groupby(ordered, key=lambda g: g.gameday):
        batch = list(group)
        pending = []
        for game in batch:
            home, home_errors = team_features(histories[game.home_team], aggregates)
            away, away_errors = team_features(histories[game.away_team], aggregates)
            baseline = system.expected_margin(game.home_team, game.away_team, neutral_site=game.neutral_site)
            features = {"baseline_margin": baseline, "neutral_site": int(game.neutral_site), "rest_difference": rest_days(histories[game.home_team], game) - rest_days(histories[game.away_team], game)}
            for key in home:
                missing = home[key] is None or away[key] is None
                features[f"diff_{key}"] = None if missing else home[key] - away[key]
                features[f"missing_{key}"] = int(missing)
            if family == "rolling-adjusted":
                # Ratings and opponent lists contain only completed earlier dates.
                home_opp = [system.ratings[t] for t in opponents[game.home_team][-16:]]
                away_opp = [system.ratings[t] for t in opponents[game.away_team][-16:]]
                features["opponent_rating_difference"] = (sum(home_opp) / len(home_opp) if home_opp else 0.) - (sum(away_opp) / len(away_opp) if away_opp else 0.)
                for window in WINDOWS:
                    for side in ("off", "def"):
                        key = f"diff_{side}_epa_{window}"
                        features[f"opponent_adjusted_{side}_epa_{window}"] = None if features[key] is None else features[key] - .08 * features["opponent_rating_difference"]
            elif family == "srs":
                features["srs_home_rating"] = system.ratings[game.home_team]
                features["srs_away_rating"] = system.ratings[game.away_team]
                features["srs_margin"] = baseline
            actual = game.home_score - game.away_score if game.complete and day < before else None
            reasons = [f"Home: {r}" for r in home_errors] + [f"Away: {r}" for r in away_errors]
            rows.append({**game.to_dict(), "baseline_margin": baseline, "actual_margin": actual,
                         "features": features, "home_stats": home, "away_stats": away,
                         "eligible": not reasons, "exclusion": "; ".join(reasons)})
            if actual is not None:
                pending.append((game, (actual - baseline) * 0.2))
        for game, change in pending:
            system.ratings[game.home_team] += change
            system.ratings[game.away_team] -= change
            histories[game.home_team].append((game, game.home_team))
            histories[game.away_team].append((game, game.away_team))
            opponents[game.home_team].append(game.away_team)
            opponents[game.away_team].append(game.home_team)
    return rows


def feature_dataset(store, now=None, persist=True, family="raw"):
    from zoneinfo import ZoneInfo
    from againstallodds.exceptions import AgainstAllOddsError
    now = now or utcnow()
    snapshot = store.latest()
    if not snapshot:
        raise AgainstAllOddsError("Import the NFL schedule before building features.")
    research = ResearchStore(store)
    stats = research.latest_stats()
    aggregates = {}
    for item in stats.values():
        aggregates.update(json.loads(item["aggregates"]))
    cutoff = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    manifest = {"schedule": snapshot["id"], "statistics": {str(s): item["id"] for s, item in stats.items()}, "before": cutoff, "family": family}
    rows = build_features(store.games(snapshot), aggregates, cutoff, family)
    fid = research.save_features(manifest, FEATURE_VERSION, rows, now) if persist else None
    return fid, rows, manifest
