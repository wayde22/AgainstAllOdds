import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from againstallodds.analytics import forward_results, sync_nfl, upcoming
from againstallodds.analytics_store import AnalyticsStore
from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.odds import consensus, import_odds, market_check_plan, market_quality, run_due_market_checks, sync_odds
from againstallodds.weather import sync_weather, weather_summary


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def schedule():
    return b"game_id,season,week,game_type,gameday,gametime,away_team,home_team,away_score,home_score,location,spread_line\n2026_01_CHI_DET,2026,1,REG,2026-09-10,20:20,CHI,DET,,,Home,\n"


def seeded(tmp_path):
    store = AnalyticsStore(tmp_path)
    sync_nfl(store, fetch=schedule, now=NOW)
    return store


def provider_event():
    return [{"id": 7, "home": "Detroit Lions", "away": "Chicago Bears", "date": "2026-09-11T00:20:00Z", "bookmakers": {"DraftKings": [{"name": "Spread", "updatedAt": "2026-09-07T11:00:00Z", "odds": [{"hdp": "-3.5"}]}], "FanDuel": [{"name": "Spread", "updatedAt": "2026-09-07T11:01:00Z", "odds": [{"hdp": "-2.5"}]}]}}]


def test_provider_sync_matches_schedule_and_uses_home_margin_consensus(tmp_path):
    store = seeded(tmp_path)
    result = sync_odds(store, fetch=provider_event, now=NOW)
    lines, snapshot = consensus(store, now=NOW)
    assert result["lines"] == 2
    assert snapshot["id"] == result["snapshot_id"]
    assert lines["2026_01_CHI_DET"]["market_margin"] == 3.0
    assert lines["2026_01_CHI_DET"]["market_books"] == 2


def test_csv_import_and_saved_forecast_keep_the_observed_market_line(tmp_path):
    store = seeded(tmp_path)
    csv = b"home_team,away_team,bookmaker,home_spread,kickoff,captured_at\nDetroit Lions,Chicago Bears,Book A,-4,2026-09-11T00:20:00Z,2026-09-07T12:00:00Z\n"
    import_odds(store, csv, now=NOW)
    rows = upcoming(store, now=NOW, save=True)
    assert rows[0]["market_margin"] == 4.0
    assert rows[0]["edge"] == -2.0
    assert len(store.predictions()) == 2
    sync_nfl(store, fetch=lambda: schedule().replace(b",,,Home", b",17,24,Home"), now=NOW + timedelta(days=6))
    result = forward_results(store, now=NOW + timedelta(days=6))
    assert result[0]["market_margin"] == 4.0


def test_checked_in_csv_example_imports_into_matching_schedule(tmp_path):
    store = seeded(tmp_path)
    payload = (Path(__file__).resolve().parents[1] / "market-lines.example.csv").read_bytes()
    result = import_odds(store, payload, now=NOW)
    lines, _ = consensus(store, now=NOW)
    assert result["lines"] == 1
    assert lines["2026_01_CHI_DET"]["market_margin"] == 3.5


def test_bad_csv_retains_prior_snapshot_and_records_error(tmp_path):
    store = seeded(tmp_path)
    prior = import_odds(store, b"home_team,away_team,bookmaker,home_spread\nDetroit Lions,Chicago Bears,Book A,-3\n", now=NOW)
    with pytest.raises(AgainstAllOddsError):
        import_odds(store, b"wrong\nvalue\n", now=NOW + timedelta(minutes=1))
    assert store.latest_market_snapshot()["id"] == prior["snapshot_id"]
    assert store.last_market_error()


def test_unmatched_provider_events_do_not_create_market_lines(tmp_path):
    store = seeded(tmp_path)
    result = sync_odds(store, fetch=lambda: [{"home": "Unknown", "away": "Nobody", "date": "2026-09-11T00:20:00Z", "bookmakers": {}}], now=NOW)
    assert result["lines"] == 0
    assert consensus(store, now=NOW)[0] == {}


def test_market_check_plan_is_saved_and_due_checks_do_not_overwrite_snapshots(tmp_path):
    store = seeded(tmp_path)
    checks = market_check_plan(store, now=NOW)
    assert any(check["kind"] == "72h" for check in checks)
    assert any(check["kind"] == "15m" for check in checks)
    assert run_due_market_checks(store, now=NOW)["due"] == 0
    assert store.latest_market_snapshot() is None


def test_market_quality_uses_opening_and_current_saved_consensus(tmp_path):
    store = seeded(tmp_path)
    import_odds(store, b"home_team,away_team,bookmaker,home_spread\nDetroit Lions,Chicago Bears,Book A,-2\n", now=NOW)
    import_odds(store, b"home_team,away_team,bookmaker,home_spread\nDetroit Lions,Chicago Bears,Book A,-4\n", now=NOW + timedelta(minutes=5))
    row = market_quality(store, [{"game_id": "2026_01_CHI_DET", "kickoff": "2026-09-11T00:20:00+00:00", "predicted_margin": 7, "market_margin": 4, "edge": 3}])[0]
    assert row["opening_market_margin"] == 2
    assert row["line_movement"] == 2
    assert row["final_market_margin"] == 4
    assert row["home_win_probability"] > .5
    assert row["possible_edge"]


def test_weather_snapshot_is_immutable_and_summary_uses_forecast_hour(tmp_path):
    store = seeded(tmp_path)
    payload = b'{"hourly":{"time":["2026-09-11T00:00","2026-09-11T01:00"],"temperature_2m":[20,21],"wind_speed_10m":[10,11],"precipitation":[0,0.2]}}'
    saved = sync_weather(store, fetch=lambda _url: payload, now=NOW)
    weather = store.latest_weather("2026_01_CHI_DET", before=NOW)
    assert saved
    assert json.loads(weather["forecast"])["roof"] == "indoor"
    outdoor_id = store.save_weather("2026_01_CHI_DET", "fixture", {"roof": "outdoor", "hourly": json.loads(payload)["hourly"]}, NOW + timedelta(minutes=1))
    outdoor = store.latest_weather("2026_01_CHI_DET", before=NOW + timedelta(minutes=1))
    summary = weather_summary(outdoor, "2026-09-11T00:20:00+00:00")
    assert summary["weather_snapshot_id"] == outdoor_id
    assert summary["weather_temperature_c"] == 20
    row = upcoming(store, now=NOW)[0]
    assert row["venue_surface"] == "turf"
    assert row["away_travel_miles"] > 0
