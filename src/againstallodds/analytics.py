"""Reproducible baseline evaluation and genuine pre-kickoff predictions."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import groupby

from againstallodds import nfl_data
from againstallodds.analytics_store import AnalyticsStore
from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.ratings import PowerRatingSystem

CONFIG = {"model": "power-rating-v1", "k": 0.2, "home_field": 2.0, "offseason_regression": 0, "update_policy": "after_game_date"}


def replay(games, start=2015, end=2026, before=None):
    system = PowerRatingSystem()
    rows, history = [], []
    ordered = sorted((g for g in games if start <= g.season <= end and (before is None or g.gameday < before)), key=lambda g: (g.gameday, g.game_id))
    for day, batch in groupby(ordered, key=lambda g: g.gameday):
        completed = [g for g in batch if g.complete]
        # All predictions and changes for a date use the same pre-date ratings.
        updates = []
        for g in completed:
            margin = system.expected_margin(g.home_team, g.away_team, neutral_site=g.neutral_site)
            actual = g.home_score - g.away_score
            row = {**g.to_dict(), "predicted_margin": margin, "actual_margin": actual, "edge": None if g.market_margin is None else margin - g.market_margin}
            if g.season > start:
                rows.append(row)
            change = (actual - margin) * CONFIG["k"]
            updates.append((g, change))
        for g, change in updates:
            system.ratings[g.home_team] += change
            system.ratings[g.away_team] -= change
        for team, rating in system.rankings():
            if completed:
                history.append({"date": day, "team": team, "rating": rating})
    return rows, system, history


def metrics(rows):
    count = len(rows)
    decisive = [r for r in rows if r["actual_margin"] != 0 and r["predicted_margin"] != 0]
    lined = [r for r in rows if r["market_margin"] is not None]
    thresholds = []
    for threshold in (1, 2, 3, 5):
        chosen = [r for r in lined if abs(r["predicted_margin"] - r["market_margin"]) > threshold]
        outcomes = [(r["actual_margin"] - r["market_margin"]) * (1 if r["predicted_margin"] > r["market_margin"] else -1) for r in chosen]
        wins, losses = sum(x > 0 for x in outcomes), sum(x < 0 for x in outcomes)
        thresholds.append({"edge_above": threshold, "games": len(chosen), "wins": wins, "losses": losses, "pushes": sum(x == 0 for x in outcomes), "win_rate": wins / (wins + losses) if wins + losses else None})
    return {"games": count, "margin_mae": sum(abs(r["actual_margin"] - r["predicted_margin"]) for r in rows) / count if count else None,
            "winner_accuracy": sum(r["actual_margin"] * r["predicted_margin"] > 0 for r in decisive) / len(decisive) if decisive else None,
            "winner_samples": len(decisive), "ties": sum(r["actual_margin"] == 0 for r in rows), "pickems": sum(r["predicted_margin"] == 0 for r in rows),
            "line_games": len(lined), "missing_lines": count - len(lined),
            "market_mae": sum(abs(r["actual_margin"] - r["market_margin"]) for r in lined) / len(lined) if lined else None,
            "model_mae_on_lined_games": sum(abs(r["actual_margin"] - r["predicted_margin"]) for r in lined) / len(lined) if lined else None, "thresholds": thresholds}


def backtest(store, now=None):
    snapshot = store.latest()
    if not snapshot:
        raise AgainstAllOddsError("Import NFL data first with sync-nfl.")
    start, end = snapshot["start_season"], snapshot["end_season"]
    now = now or nfl_data.utcnow()
    # Exclude the current Eastern date even if a source exposes live partial scores.
    today = now.astimezone(nfl_data.ZoneInfo("America/New_York")).date().isoformat()
    config = {**CONFIG, "start": start, "end": end, "before": today, "report_version": 2}
    with store.connection() as db:
        cached = db.execute("SELECT result FROM runs WHERE snapshot_id=? AND kind='historical' AND config=?", (snapshot["id"], json.dumps(config, sort_keys=True))).fetchone()
    if cached:
        return json.loads(cached[0])
    rows, system, history = replay(store.games(snapshot), start, end, before=today)
    result = {"summary": metrics(rows), "seasons": [{"season": season, **metrics([r for r in rows if r["season"] == season])} for season in sorted({r["season"] for r in rows})], "rows": rows, "ratings": system.rankings(), "history": history}
    store.save_run(snapshot["id"], config, result, now)
    return result


def upcoming(store, now=None, save=False):
    now = now or nfl_data.utcnow()
    snapshot = store.latest()
    if not snapshot:
        return []
    games = store.games(snapshot)
    today = now.astimezone(nfl_data.ZoneInfo("America/New_York")).date().isoformat()
    _, system, _ = replay(games, snapshot["start_season"], snapshot["end_season"], before=today)
    from againstallodds.odds import consensus
    market, market_snapshot = consensus(store, now=now)
    rows = []
    for g in games:
        if g.complete or not g.kickoff or not now < datetime.fromisoformat(g.kickoff) <= now + timedelta(days=7):
            continue
        margin = system.expected_margin(g.home_team, g.away_team, neutral_site=g.neutral_site)
        # Schedule reference lines are not verified point-in-time market observations.
        line = market.get(g.game_id, {})
        market_margin = line.get("market_margin")
        rows.append({**g.to_dict(), "predicted_margin": margin, "market_margin": market_margin, "edge": None if market_margin is None else round(margin - market_margin, 2), **line})
    if save:
        store.save_predictions(snapshot["id"], {**CONFIG, "start": snapshot["start_season"], "end": snapshot["end_season"], "before": today, "market_snapshot_id": market_snapshot and market_snapshot["id"]}, rows, now)
    return rows


def forward_results(store, now=None, model_id="power-rating-v1"):
    now = now or nfl_data.utcnow()
    today = now.astimezone(nfl_data.ZoneInfo("America/New_York")).date().isoformat()
    games = {g.game_id: g for g in store.games()}
    selected = {}
    for saved in store.predictions():
        model = saved.get("model_id", "power-rating-v1")
        if model_id is not None and model != model_id:
            continue
        g = games.get(saved["game_id"])
        if not g or not g.complete or not g.kickoff or g.gameday >= today:
            continue
        # Require the save to precede both recorded and latest kickoff (reschedules).
        if saved["created_at"] >= min(saved["kickoff"], g.kickoff):
            continue
        p = json.loads(saved["payload"])
        selected[(g.game_id, model)] = {**p, "model_id": model, "actual_margin": g.home_score - g.away_score, "saved_at": saved["created_at"]}
    return list(selected.values())


def sync_nfl(store: AnalyticsStore, start=2015, end=2026, *, fetch=None, now=None):
    now = now or nfl_data.utcnow()
    try:
        payload = (fetch or nfl_data.download)()
        games = nfl_data.parse_schedule(payload, start, end)
        snapshot_id = store.ingest(payload, games, start, end, now)
    except (AgainstAllOddsError, OSError) as error:
        store.record_failure(error, now)
        raise AgainstAllOddsError(str(error)) from error
    result = backtest(store, now)
    predictions = upcoming(store, now, save=True)
    response = {"snapshot_id": snapshot_id, "games": len(games), "forward_predictions": len(predictions), "summary": result["summary"]}
    # Rich statistics are opt-in; once initialized, ordinary refresh keeps the
    # current season up to date and forecasts with frozen artifacts, never fits.
    from againstallodds.research_store import ResearchStore
    research = ResearchStore(store)
    if research.latest_stats():
        from againstallodds.nfl_stats import sync_stats
        response["statistics"] = sync_stats(store, end, end, now=now)
        if research.latest_experiment():
            from againstallodds.experiments import predict_models
            response["challenger_predictions"] = len([r for r in predict_models(store, now=now, save=True) if r["model_id"] != "power-rating-v1" and r["available"]])
    return response
