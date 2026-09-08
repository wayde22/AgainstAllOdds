from datetime import datetime, timedelta, timezone

from againstallodds.analytics_store import AnalyticsStore
from againstallodds import injuries
from againstallodds.injuries import AvailabilityStore, assess_availability, assess_edge_rushers, assess_skill_position, assess_wide_receivers, current_report, injury_adjusted_predictions, notify_windows, parse_official, report_freshness


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def test_official_table_parser_retains_report_fields():
    html = b"<h2>Detroit Lions</h2><table><tr><th>Player</th><th>Position</th><th>Injury</th><th>Practice</th><th>Game Status</th></tr><tr><td>QB One</td><td>QB</td><td>Shoulder</td><td>Limited</td><td>Questionable</td></tr></table>"
    records = parse_official(html, "injuries")
    assert records == [{"player_name": "QB One", "position": "QB", "injury": "Shoulder", "practice_status": "Limited", "game_status": "Questionable", "team_hint": "Detroit Lions", "source": "injuries"}]


def test_qb_assessment_uses_official_status_and_keeps_base_forecast_separate(tmp_path):
    store = AnalyticsStore(tmp_path)
    availability = AvailabilityStore(store)
    profiles = [
        {"player_id": "starter", "name": "QB One", "team": "Detroit Lions", "prior_games": 16, "dropbacks": 500, "expected_dropbacks": 32, "shrunk_epa_per_dropback": .18, "last_game_date": "2026-09-01", "last_game_dropbacks": 35},
        {"player_id": "backup", "name": "QB Two", "team": "Detroit Lions", "prior_games": 8, "dropbacks": 160, "expected_dropbacks": 25, "shrunk_epa_per_dropback": -.08, "last_game_date": "2026-08-31", "last_game_dropbacks": 8},
    ]
    availability.save_profile({"fixture": True}, profiles, NOW)
    sid = availability.snapshot("injuries", b"fixture", [{"player_name": "QB One", "team": "Detroit Lions", "game_status": "Out", "practice_status": "", "source": "injuries"}], NOW)
    snapshot = availability.latest_snapshot()
    assert snapshot["id"] == sid
    result = assess_availability(store, {"game_id": "fixture", "team": "Detroit Lions"}, snapshot=snapshot, now=NOW)
    assert result["base_qb"] == "starter"
    assert result["replacement_qb"] == "backup"
    assert result["absence_weight"] == 1.0
    assert result["adjustment"] < 0


def test_inactives_take_precedence_and_assessment_changes_are_recorded(tmp_path):
    store = AnalyticsStore(tmp_path)
    availability = AvailabilityStore(store)
    profiles = [
        {"player_id": "starter", "name": "QB One", "team": "Detroit Lions", "prior_games": 16, "dropbacks": 500, "expected_dropbacks": 32, "shrunk_epa_per_dropback": .18, "last_game_date": "2026-09-01", "last_game_dropbacks": 35},
        {"player_id": "backup", "name": "QB Two", "team": "Detroit Lions", "prior_games": 8, "dropbacks": 160, "expected_dropbacks": 25, "shrunk_epa_per_dropback": -.08, "last_game_date": "2026-08-31", "last_game_dropbacks": 8},
    ]
    availability.save_profile({"fixture": "precedence"}, profiles, NOW)
    assess_availability(store, {"game_id": "fixture", "team": "Detroit Lions"}, now=NOW)
    availability.snapshot("injuries", b"injury", [{"player_name": "QB One", "team": "Detroit Lions", "practice_status": "Limited", "game_status": "", "source": "injuries"}], NOW)
    availability.snapshot("inactives", b"inactive", [{"player_name": "QB One", "team": "Detroit Lions", "practice_status": "", "game_status": "Inactive", "source": "inactives"}], NOW)
    report = current_report(store)
    assert report["records"][0]["game_status"] == "Inactive"
    assert report_freshness(report, now=NOW)["fresh"]
    result = assess_availability(store, {"game_id": "fixture", "team": "Detroit Lions"}, snapshot=report, now=NOW)
    assert result["change"] is not None
    assert availability.changes()[0]["team"] == "Detroit Lions"


def test_report_freshness_marks_old_snapshot_stale(tmp_path):
    store = AnalyticsStore(tmp_path)
    availability = AvailabilityStore(store)
    availability.snapshot("injuries", b"old", [], NOW - timedelta(hours=9))
    assert report_freshness(current_report(store), now=NOW) == {"fresh": False, "age_hours": 9.0}


def test_manual_override_requires_a_real_profile_and_windows_toast_is_mockable(tmp_path, monkeypatch):
    store = AnalyticsStore(tmp_path)
    availability = AvailabilityStore(store)
    profiles = [
        {"player_id": "starter", "name": "QB One", "team": "Detroit Lions", "prior_games": 16, "dropbacks": 500, "expected_dropbacks": 32, "shrunk_epa_per_dropback": .18, "last_game_date": "2026-09-01", "last_game_dropbacks": 35},
        {"player_id": "backup", "name": "QB Two", "team": "Detroit Lions", "prior_games": 8, "dropbacks": 160, "expected_dropbacks": 25, "shrunk_epa_per_dropback": -.08, "last_game_date": "2026-08-31", "last_game_dropbacks": 8},
    ]
    availability.save_profile({"fixture": "override"}, profiles, NOW)
    availability.override("fixture", "Detroit Lions", "backup", "QB Two", "Confirmed starter", NOW)
    result = assess_availability(store, {"game_id": "fixture", "team": "Detroit Lions"}, now=NOW)
    assert result["expected_qb"] == "backup"
    assert result["match_confidence"] == "manual"
    calls = []
    monkeypatch.setattr(injuries.subprocess, "run", lambda *args, **kwargs: calls.append(args) or None)
    assert notify_windows("Availability", "Detroit Lions changed")
    assert calls and calls[0][0][0] == "powershell"


def test_wide_receiver_assessment_uses_matched_official_status(tmp_path):
    store = AnalyticsStore(tmp_path)
    availability = AvailabilityStore(store)
    profiles = [
        {"player_id": "wr-one", "name": "WR One", "team": "Detroit Lions", "epa_per_game": 2.8},
        {"player_id": "wr-two", "name": "WR Two", "team": "Detroit Lions", "epa_per_game": 0.7},
    ]
    availability.save_wr_profile({"fixture": "wr"}, profiles, NOW)
    report = {"id": "fixture", "records": [{"player_name": "WR One", "team": "Detroit Lions", "position": "WR", "game_status": "Out", "practice_status": "", "source": "injuries"}]}
    result = assess_wide_receivers(store, {"game_id": "fixture", "team": "Detroit Lions"}, snapshot=report, now=NOW)
    assert result["players"][0]["player_name"] == "WR One"
    assert result["players"][0]["replacement"] == "WR Two"
    assert result["adjustment"] < 0


def test_rb_te_assessments_match_roles_and_skill_total_is_capped(tmp_path):
    store = AnalyticsStore(tmp_path)
    availability = AvailabilityStore(store)
    availability.save_wr_profile({"fixture": "cap-wr"}, [{"player_id": "wr-one", "name": "WR One", "team": "Detroit Lions", "epa_per_game": 20}, {"player_id": "wr-two", "name": "WR Two", "team": "Detroit Lions", "epa_per_game": 0}], NOW)
    profiles = {"RB": [{"player_id": "rb-one", "name": "RB One", "team": "Detroit Lions", "epa_per_game": 20}, {"player_id": "rb-two", "name": "RB Two", "team": "Detroit Lions", "epa_per_game": 0}], "TE": [{"player_id": "te-one", "name": "TE One", "team": "Detroit Lions", "epa_per_game": 20}, {"player_id": "te-two", "name": "TE Two", "team": "Detroit Lions", "epa_per_game": 0}]}
    availability.save_rb_te_profile({"fixture": "cap-skill"}, profiles, NOW)
    records = [{"player_name": name, "team": "Detroit Lions", "position": position, "game_status": "Out", "practice_status": "", "source": "injuries"} for name, position in [("WR One", "WR"), ("RB One", "RB"), ("TE One", "TE")]]
    availability.snapshot("injuries", b"cap", records, NOW)
    report = current_report(store)
    rb = assess_skill_position(store, {"game_id": "fixture", "team": "Detroit Lions"}, "RB", snapshot=report, now=NOW)
    te = assess_skill_position(store, {"game_id": "fixture", "team": "Detroit Lions"}, "TE", snapshot=report, now=NOW)
    assert rb["players"][0]["replacement"] == "RB Two"
    assert te["players"][0]["replacement"] == "TE Two"
    rows = injury_adjusted_predictions(store, [{"game_id": "fixture", "home_team": "Detroit Lions", "away_team": "Chicago Bears", "home_score": None, "away_score": None, "gameday": "2026-09-10", "predicted_margin": 0.0, "model_id": "power-rating-v1"}], now=NOW)
    assert rows[0]["skill_adjustments"][0] == -3.0


def test_edge_assessment_uses_official_position_and_available_replacement(tmp_path):
    store = AnalyticsStore(tmp_path)
    availability = AvailabilityStore(store)
    availability.save_edge_profile(
        {"fixture": "edge"},
        [
            {"player_id": "edge-one", "name": "EDGE One", "team": "Detroit Lions", "disruption_per_game": 2.8},
            {"player_id": "edge-two", "name": "EDGE Two", "team": "Detroit Lions", "disruption_per_game": 0.4},
        ],
        NOW,
    )
    report = {"id": "fixture", "records": [{"player_name": "EDGE One", "team": "Detroit Lions", "position": "DE", "game_status": "Out", "practice_status": "", "source": "injuries"}]}
    result = assess_edge_rushers(store, {"game_id": "fixture", "team": "Detroit Lions"}, snapshot=report, now=NOW)
    assert result["players"][0]["replacement"] == "EDGE Two"
    assert result["players"][0]["match_confidence"] == "exact"
    assert result["adjustment"] < 0
