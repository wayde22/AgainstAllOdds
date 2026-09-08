from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from againstallodds.analytics import forward_results, replay
from againstallodds.analytics_store import AnalyticsStore
from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.features import build_features, rest_days, team_features
from againstallodds.nfl_data import NFLGame
from againstallodds.nfl_stats import STAT_VERSION, aggregate_plays, sync_stats
from againstallodds.research_store import ResearchStore

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def plays(game_id="2015_01_CHI_DET"):
    rows = []
    scenarios = [("pass", 20, 1., 1, 0, 0), ("run", 10, -1., 0, 0, 0), ("pass", -5, -2., 1, 1, 0), ("run", 12, .5, 1, 0, 0), ("pass", 0, None, 1, 0, 1)]
    for home in (True, False):
        for kind, yards, epa, dropback, sack, interception in scenarios:
            rows.append(dict(game_id=game_id, play_id=len(rows) + 1, drive=1, season_type="REG", posteam="DET" if home else "CHI", defteam="CHI" if home else "DET", play_type=kind, qb_kneel=0, qb_spike=0, qb_dropback=dropback, epa=epa, yards_gained=yards, sack=sack, interception=interception, fumble_lost=0))
    return pd.DataFrame(rows)


def game(season=2015, week=1, home_score=24, away_score=17):
    return NFLGame(f"{season}_{week:02}_CHI_DET", season, week, "REG", f"{season}-09-{week:02}", f"{season}-09-{week:02}T20:00:00+00:00", "Detroit Lions", "Chicago Bears", home_score, away_score, False, 3.)


def aggregate_games(games):
    result = {}
    for g in games:
        result.update(aggregate_plays(plays(g.game_id)))
    return result


def research_fixture(tmp_path):
    store = AnalyticsStore(tmp_path)
    research = ResearchStore(store)
    games = [game(season, week, 20 + week + (season % 3), 15 + week % 4) for season in range(2015, 2026) for week in range(1, 9)]
    games.append(replace(game(2026, 10, None, None), week=1))
    store.ingest(b"fixture schedule", games, 2015, 2026, NOW)
    for season in range(2015, 2026):
        gs = [g for g in games if g.season == season]
        research.save_stats(season, str(season), tmp_path / f"{season}.parquet", aggregate_games(gs), STAT_VERSION, NOW)
    return store, research, games


def test_play_filters_and_denominators():
    frame = plays()
    excluded = []
    for kind, kneel, spike in [("no_play", 0, 0), ("run", 1, 0), ("pass", 0, 1), ("kickoff", 0, 0)]:
        excluded.append({**frame.iloc[0].to_dict(), "play_id": 100 + len(excluded), "play_type": kind, "qb_kneel": kneel, "qb_spike": spike, "epa": 1000})
    result = aggregate_plays(pd.concat([frame, pd.DataFrame(excluded)], ignore_index=True))
    home = result["2015_01_CHI_DET"]["Detroit Lions"]
    assert home["off"]["plays"] == 5
    assert home["off"]["epa_sum"] == -1.5
    assert home["off"]["epa_n"] == 4
    assert home["off"]["success_sum"] == 2
    assert home["off"]["dropback_epa_n"] == 3
    assert home["off"]["explosive_sum"] == 2
    assert home["off"]["sack_sum"] == 1
    assert home["off"]["dropbacks"] == 4
    assert home["off"]["turnover_sum"] == 1
    assert home["off"] == result["2015_01_CHI_DET"]["Chicago Bears"]["def"]


def test_bad_stats_and_aliases():
    frame = plays()
    with pytest.raises(AgainstAllOddsError):
        aggregate_plays(frame.drop(columns="epa"))
    with pytest.raises(AgainstAllOddsError):
        aggregate_plays(pd.concat([frame, frame.iloc[:1]]))
    with pytest.raises(AgainstAllOddsError):
        aggregate_plays(frame.assign(posteam="UNKNOWN"))
    result = aggregate_plays(frame.replace({"DET": "OAK", "CHI": "STL"}))
    assert set(result["2015_01_CHI_DET"]) == {"Las Vegas Raiders", "Los Angeles Rams"}


def test_features_are_pregame_and_match_baseline():
    games = [game(2015, n) for n in range(1, 8)] + [game(2016, 1)]
    aggregates = aggregate_games(games)
    rows = build_features(games, aggregates, "2026-01-01")
    assert not rows[2]["eligible"]
    assert rows[3]["eligible"]
    assert rows[-1]["home_stats"]["games_5"] == 5
    assert rows[-1]["home_stats"]["games_16"] == 7
    assert rows[-1]["baseline_margin"] == replay(games)[0][0]["predicted_margin"]
    changed_games = [*games[:-1], replace(games[-1], home_score=100)]
    changed_aggregates = dict(aggregates)
    changed_aggregates[games[-1].game_id] = {}
    altered = build_features(changed_games, changed_aggregates, "2026-01-01")
    assert altered[-1]["features"] == rows[-1]["features"]
    assert all(r["features"] == a["features"] for r, a in zip(rows, altered))


def test_same_day_missing_data_and_current_date():
    games = [game(2015, n) for n in range(1, 6)]
    games.append(replace(game(2015, 6), gameday=games[-1].gameday))
    stats = aggregate_games(games)
    rows = build_features(games, stats, "2015-09-05")
    assert rows[-1]["features"] == rows[-2]["features"]
    assert rows[-1]["actual_margin"] is None
    stats.pop(games[1].game_id)
    missing = build_features(games, stats, "2026-01-01")
    assert not missing[-1]["eligible"]
    assert "missing statistics" in missing[-1]["exclusion"]


def test_opponent_adjusted_feature_families_are_pregame_and_distinct():
    games = [game(2015, n) for n in range(1, 7)]
    stats = aggregate_games(games)
    rolling = build_features(games, stats, "2026-01-01", family="rolling-adjusted")
    srs = build_features(games, stats, "2026-01-01", family="srs")
    assert "opponent_adjusted_off_epa_5" in rolling[-1]["features"]
    assert "srs_margin" in srs[-1]["features"]
    changed = [*games[:-1], replace(games[-1], home_score=100)]
    assert build_features(changed, stats, "2026-01-01", family="rolling-adjusted")[-1]["features"] == rolling[-1]["features"]


def test_feature_family_leaderboard_marks_untrained_families(tmp_path):
    from againstallodds.research_ui import family_leaderboard
    store = AnalyticsStore(tmp_path)
    table = family_leaderboard(store)
    assert {row["Feature family"] for row in table} == {"Raw rolling rates", "Rolling opponent-normalized", "Season-long SRS"}
    assert {row["Status"] for row in table} == {"Not trained"}


def test_rest_and_rate_pooling():
    games = [game(2015, n) for n in range(1, 4)]
    history = [(g, g.home_team) for g in games]
    stats = aggregate_games(games)
    stats[games[0].game_id][games[0].home_team]["off"]["epa_sum"] = 20
    stats[games[0].game_id][games[0].home_team]["off"]["epa_n"] = 20
    values, reasons = team_features(history, stats)
    assert values["off_epa_5"] == pytest.approx((20 - 1.5 - 1.5) / (20 + 4 + 4))
    assert not reasons
    assert rest_days(history, game(2016)) == 7
    assert rest_days(history, replace(game(), gameday="2015-12-01")) == 21


def test_stats_cache_retry_and_correction(tmp_path):
    store = AnalyticsStore(tmp_path)
    calls = []
    def fetch(season, root):
        calls.append(season)
        return str(season), root / "fixture", aggregate_games([game(season)])
    sync_stats(store, 2015, 2016, fetch=fetch, now=NOW)
    sync_stats(store, 2015, 2016, fetch=fetch, now=NOW)
    assert calls == [2015, 2016, 2016]
    old = ResearchStore(store).latest_stats()
    def fail(season, root):
        raise OSError("offline fixture")
    report = sync_stats(store, 2015, 2016, fetch=fail, now=NOW)
    assert report[-1]["status"] == "failed"
    assert ResearchStore(store).latest_stats() == old
    sync_stats(store, 2015, 2015, refresh_history=True, fetch=lambda s, r: ("new-hash", r / "new", aggregate_games([game(s, home_score=30)])), now=NOW)
    assert ResearchStore(store).latest_stats()[2015]["id"] != old[2015]["id"]


def test_training_split_imputer_and_deterministic_bootstrap():
    from againstallodds.experiments import fit_model, fold_rows, paired_interval
    rows = [{"season": s, "eligible": True, "actual_margin": y, "features": {"x": x}} for s, x, y in [(2016, 0, 0), (2017, 2, 2), (2018, None, 1), (2024, 1000, 1000), (2025, 2000, 2000)]]
    train, test = fold_rows(rows, 2024)
    assert len(train) == 3 and len(test) == 1
    model = fit_model("ridge-v1", {"alpha": 1}, train, ["x"])
    assert model.named_steps["impute"].statistics_[0] == 1
    assert model.named_steps["scale"].mean_[0] == 1
    sample = [dict(season=2024, week=w, actual_margin=5, predicted_margin=4, baseline_margin=2) for w in range(1, 5)]
    assert paired_interval(sample) == paired_interval(sample)
    assert paired_interval(sample)["delta_mae"] == -2


def test_existing_predictions_migrate_and_models_do_not_overwrite(tmp_path):
    path = tmp_path / "analytics.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE predictions(id INTEGER PRIMARY KEY,snapshot_id TEXT,game_id TEXT,created_at TEXT,kickoff TEXT,config TEXT,payload TEXT,UNIQUE(snapshot_id,game_id,config))")
    db.execute("INSERT INTO predictions VALUES (1,'old','oldgame','2026-09-01','2026-09-02','{}','{}')")
    db.commit()
    db.close()
    store = AnalyticsStore(tmp_path)
    assert store.predictions()[0]["model_id"] == "power-rating-v1"
    g = game(2026, 10, None, None)
    sid = store.ingest(b"test", [g], 2015, 2026, NOW)
    for model, margin in [("power-rating-v1", 3), ("ridge-v1", 5), ("boosted-v1", 1)]:
        p = {**g.to_dict(), "predicted_margin": margin, "market_margin": None}
        store.save_predictions(sid, {"model": model}, [p], NOW)
    store.ingest(b"final", [replace(g, home_score=21, away_score=17)], 2015, 2026, NOW)
    after = datetime(2026, 9, 12, tzinfo=timezone.utc)
    assert len(forward_results(store, after)) == 1
    assert len(forward_results(store, after, model_id=None)) == 3
    assert forward_results(store, after, model_id="ridge-v1")[0]["predicted_margin"] == 5


def test_full_experiment_saved_forecasts_and_failed_retrain(tmp_path, monkeypatch):
    from againstallodds import experiments
    store, research, games = research_fixture(tmp_path)
    # Small deterministic candidate set tests orchestration; production uses the full grid.
    monkeypatch.setitem(experiments.SPEC, "validation", [2019])
    monkeypatch.setattr(experiments, "candidates", lambda m: [{"alpha": 100}] if m == "ridge-v1" else [{"max_leaf_nodes": 7, "l2_regularization": 10}])
    result = experiments.compare_models(store, now=NOW)
    assert len(result["summary"]) == 6
    test_counts = {r["games"] for r in result["summary"] if r["stage"] == "test"}
    assert test_counts == {16}
    for artifact in research.artifacts(result["experiment_id"], 2026):
        assert artifact["trained_through"] == 2025
        assert research.load_model(artifact) is not None
    forecasts = experiments.predict_models(store, now=NOW, save=True)
    assert {r["model_id"] for r in forecasts if r["available"]} == set(experiments.MODELS)
    assert len(store.predictions()) == 3
    previous = store.predictions()
    assert experiments.compare_models(store, now=NOW)["experiment_id"] == result["experiment_id"]
    assert store.predictions() == previous
    # A failed new experiment cannot replace the previously published one.
    monkeypatch.setitem(experiments.SPEC, "version", "failure-fixture")
    def fail(*args, **kwargs):
        raise AgainstAllOddsError("training failure")
    monkeypatch.setattr(experiments, "fit_model", fail)
    with pytest.raises(AgainstAllOddsError):
        experiments.compare_models(store, now=NOW)
    assert research.latest_experiment()["id"] == result["experiment_id"]
    assert store.predictions() == previous


def test_research_dashboard_empty_and_navigation(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from againstallodds import dashboard
    store = AnalyticsStore(tmp_path)
    store.ingest(b"schedule", [game()], 2015, 2026, NOW)
    monkeypatch.setattr("sys.argv", ["dashboard", "--data-dir", str(tmp_path)])
    app = AppTest.from_file(dashboard.__file__, default_timeout=30)
    app.session_state[f"initial_refresh:{store.path.resolve()}"] = True
    app.run()
    app.sidebar.radio[0].set_value("Model comparison").run()
    assert not app.exception
    assert any("Import rich statistics" in i.value for i in app.info)
    app.sidebar.radio[0].set_value("Games").run()
    next(s for s in app.selectbox if s.label == "Projection model").select("ridge-v1").run()
    assert not app.exception
