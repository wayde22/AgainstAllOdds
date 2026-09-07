from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from againstallodds.analytics import backtest, forward_results, metrics, replay, sync_nfl, upcoming
from againstallodds.analytics_store import AnalyticsStore
from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.nfl_data import parse_schedule

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def csv_data(*overrides):
    rows = [
        dict(game_id="2015_01_CHI_DET", season=2015, week=1, game_type="REG", gameday="2015-09-10", gametime="20:20", away_team="CHI", home_team="DET", away_score=17, home_score=24, location="Home", spread_line=3),
        dict(game_id="2016_01_DET_CHI", season=2016, week=1, game_type="REG", gameday="2016-09-10", gametime="13:00", away_team="DET", home_team="CHI", away_score=14, home_score=21, location="Home", spread_line=-1),
        dict(game_id="2026_01_CHI_DET", season=2026, week=1, game_type="REG", gameday="2026-09-10", gametime="20:20", away_team="CHI", home_team="DET", away_score="", home_score="", location="Home", spread_line=2),
    ]
    for index, changes in overrides:
        rows[index].update(changes)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def seeded(tmp_path):
    store = AnalyticsStore(tmp_path)
    sync_nfl(store, fetch=csv_data, now=NOW)
    return store


def test_normalization_and_timezone():
    games = parse_schedule(csv_data((0, {"home_team": "STL", "away_team": "OAK", "location": "Neutral"})))
    assert games[0].home_team == "Los Angeles Rams"
    assert games[0].away_team == "Las Vegas Raiders"
    assert games[0].neutral_site
    assert games[2].kickoff == "2026-09-11T00:20:00+00:00"
    assert not games[2].complete


@pytest.mark.parametrize("changes", [{"home_team": "???"}, {"home_team": "CHI"}, {"home_score": -1}, {"home_score": ""}, {"spread_line": "nan"}, {"location": "unknown"}, {"week": 0}, {"gameday": "invalid"}, {"game_id": "2016_01_DET_CHI"}])
def test_invalid_rows_rejected(changes):
    with pytest.raises(AgainstAllOddsError):
        parse_schedule(csv_data((0, changes)))


def test_invalid_document_and_empty_seasons():
    for payload in (b"<html>Error</html>", b"\xff"):
        with pytest.raises(AgainstAllOddsError):
            parse_schedule(payload)
    with pytest.raises(AgainstAllOddsError):
        parse_schedule(csv_data(), 2020, 2021)


def test_duplicate_import_and_corrections_preserve_snapshots(tmp_path):
    store = seeded(tmp_path)
    original = store.latest()
    predictions = store.predictions()
    sync_nfl(store, fetch=csv_data, now=NOW + timedelta(hours=1))
    assert store.latest()["id"] == original["id"]
    assert store.predictions() == predictions
    assert len(store.games()) == 3
    before = backtest(store, NOW)["summary"]["margin_mae"]
    sync_nfl(store, fetch=lambda: csv_data((0, {"home_score": 40})), now=NOW + timedelta(hours=2))
    assert store.latest()["id"] != original["id"]
    assert store.games(original)[0].home_score == 24
    assert store.games()[0].home_score == 40
    assert backtest(store, NOW)["summary"]["margin_mae"] != before
    assert store.predictions()[0] == predictions[0]


def test_bad_refresh_keeps_cached_data_and_records_error(tmp_path):
    store = seeded(tmp_path)
    original = store.latest()
    with pytest.raises(AgainstAllOddsError):
        sync_nfl(store, fetch=lambda: b"bad csv", now=NOW + timedelta(hours=7))
    assert store.latest() == original
    assert store.last_error()
    assert store.stale(NOW + timedelta(hours=7))
    assert not store.stale(NOW + timedelta(hours=5))


def test_import_transaction_rolls_back(tmp_path):
    store = seeded(tmp_path)
    original = store.latest()
    games = store.games()
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest(b"test", [*games, games[0]], 2015, 2026, NOW)
    assert store.latest() == original
    assert len(store.games()) == 3


def test_warmup_chronology_and_no_future_leakage():
    games = parse_schedule(csv_data())
    rows, system, _ = replay(games)
    assert len(rows) == 1
    assert rows[0]["predicted_margin"] == pytest.approx(0)
    altered = [games[0], replace(games[1], home_score=70), games[2]]
    assert replay(altered)[0][0]["predicted_margin"] == rows[0]["predicted_margin"]
    assert replay(altered)[1].ratings != system.ratings
    same_day = replace(games[1], game_id="other", gameday=games[0].gameday)
    assert replay([games[0], same_day])[0][0]["predicted_margin"] == 2


def test_neutral_site_and_missing_lines():
    games = parse_schedule(csv_data((1, {"location": "Neutral", "spread_line": ""})))
    rows, _, _ = replay(games)
    assert rows[0]["predicted_margin"] == -2
    summary = metrics(rows)
    assert summary["missing_lines"] == 1
    assert summary["market_mae"] is None


def test_metrics_spread_signs_pushes_ties_and_threshold_boundary():
    rows = [dict(predicted_margin=p, actual_margin=a, market_margin=m) for p, a, m in [(5, 7, 3), (-5, -7, -3), (5, 3, 3), (0, 0, None), (4, 2, 3)]]
    result = metrics(rows)
    assert result["ties"] == result["pickems"] == 1
    assert result["thresholds"][0] == dict(edge_above=1, games=3, wins=2, losses=0, pushes=1, win_rate=1)
    assert result["thresholds"][1]["games"] == 0
    assert metrics([])["margin_mae"] is None


def test_forward_saves_are_immutable_and_no_backfilling(tmp_path):
    store = seeded(tmp_path)
    original = store.predictions()[0]
    after = datetime(2026, 9, 12, tzinfo=timezone.utc)
    sync_nfl(store, fetch=lambda: csv_data((2, {"home_score": 24, "away_score": 17})), now=after)
    results = forward_results(store, after)
    assert len(results) == 1
    assert results[0]["predicted_margin"] == json.loads(original["payload"])["predicted_margin"]
    assert results[0]["actual_margin"] == 7
    assert results[0]["market_margin"] is None
    assert store.predictions()[0] == original
    assert upcoming(store, after, save=True) == []
    fresh = AnalyticsStore(tmp_path / "late")
    sync_nfl(fresh, fetch=lambda: csv_data((2, {"home_score": 24, "away_score": 17})), now=after)
    assert forward_results(fresh, after) == []


def test_latest_prediction_and_postponement(tmp_path):
    store = seeded(tmp_path)
    sync_nfl(store, fetch=lambda: csv_data((0, {"home_score": 40})), now=NOW + timedelta(hours=1))
    newest = json.loads(store.predictions()[-1]["payload"])
    after = NOW + timedelta(days=6)
    sync_nfl(store, fetch=lambda: csv_data((2, {"home_score": 24, "away_score": 17})), now=after)
    assert forward_results(store, after)[0]["predicted_margin"] == newest["predicted_margin"]
    # A corrected kickoff before any recorded prediction invalidates eligibility.
    sync_nfl(store, fetch=lambda: csv_data((2, {"gameday": "2026-09-01", "home_score": 24, "away_score": 17})), now=after)
    assert forward_results(store, after) == []


def test_unknown_kickoff_and_current_day_scores_are_excluded(tmp_path):
    store = AnalyticsStore(tmp_path)
    sync_nfl(store, fetch=lambda: csv_data((2, {"gametime": ""})), now=NOW)
    assert store.predictions() == []
    sync_nfl(store, fetch=lambda: csv_data((2, {"gameday": "2026-09-07", "home_score": 3, "away_score": 0})), now=NOW)
    assert backtest(store, NOW)["summary"]["games"] == 1


def test_dashboard_views_and_filters(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from againstallodds import dashboard
    store = seeded(tmp_path)
    monkeypatch.setattr("sys.argv", ["dashboard", "--data-dir", str(tmp_path)])
    app = AppTest.from_file(dashboard.__file__, default_timeout=30)
    app.session_state[f"initial_refresh:{store.path.resolve()}"] = True
    app.run()
    assert not app.exception
    next(s for s in app.selectbox if s.label == "Season").select(2015).run()
    assert not app.exception
    app.sidebar.radio[0].set_value("Teams").run()
    assert not app.exception
    app.sidebar.radio[0].set_value("Performance").run()
    assert not app.exception
    next(r for r in app.radio if r.label == "Evaluation").set_value("Saved forward predictions").run()
    assert not app.exception


def test_dashboard_empty_and_offline_refresh(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from againstallodds import dashboard, nfl_data
    monkeypatch.setattr("sys.argv", ["dashboard", "--data-dir", str(tmp_path)])
    def fail():
        raise AgainstAllOddsError("Offline fixture")
    monkeypatch.setattr(nfl_data, "download", fail)
    app = AppTest.from_file(dashboard.__file__, default_timeout=30).run()
    assert not app.exception
    assert app.error
    seeded(tmp_path)
    app.button[0].click().run()
    assert not app.exception
    assert app.warning
    assert app.dataframe
