from __future__ import annotations

import pytest

from againstallodds.exceptions import DuplicateGameError
from againstallodds.models import CompletedGame
from againstallodds.ratings import PowerRatingSystem, record_completed_game
from againstallodds.storage import JsonStore


def test_expected_margin_uses_home_rating_away_rating_and_home_field() -> None:
    system = PowerRatingSystem(
        {
            "Detroit Lions": 3.0,
            "Chicago Bears": -1.0,
        }
    )

    expected_margin = system.expected_margin(
        "Detroit Lions",
        "Chicago Bears",
        home_field_advantage=2.0,
    )

    assert expected_margin == pytest.approx(6.0)


def test_rating_change_updates_both_teams_equally_and_oppositely() -> None:
    system = PowerRatingSystem()
    game = CompletedGame(
        game_id="2026-W01-DET-CHI",
        home_team="Detroit Lions",
        away_team="Chicago Bears",
        home_score=24,
        away_score=17,
        home_field_advantage=2.0,
        k_factor=0.20,
    )

    record = system.record_game(game)

    assert record.expected_margin == pytest.approx(2.0)
    assert record.actual_margin == 7
    assert record.rating_change == pytest.approx(1.0)
    assert system.ratings["Detroit Lions"] == pytest.approx(1.0)
    assert system.ratings["Chicago Bears"] == pytest.approx(-1.0)


def test_neutral_site_removes_home_field_advantage() -> None:
    system = PowerRatingSystem()
    game = CompletedGame(
        game_id="2026-NEUTRAL-KC-BUF",
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
        home_score=30,
        away_score=20,
        neutral_site=True,
        home_field_advantage=2.0,
        k_factor=0.20,
    )

    record = system.record_game(game)

    assert record.expected_margin == pytest.approx(0.0)
    assert record.rating_change == pytest.approx(2.0)


def test_duplicate_game_id_is_rejected() -> None:
    history = []
    ratings = PowerRatingSystem().ratings
    game = CompletedGame(
        game_id="2026-W01-DAL-PHI",
        home_team="Philadelphia Eagles",
        away_team="Dallas Cowboys",
        home_score=21,
        away_score=17,
    )
    ratings, history, _ = record_completed_game(ratings, history, game)

    with pytest.raises(DuplicateGameError):
        record_completed_game(ratings, history, game)

    assert len(history) == 1


def test_json_store_persists_ratings_and_history(tmp_path) -> None:
    store = JsonStore(tmp_path)
    store.initialize()
    game = CompletedGame(
        game_id="2026-W02-MIA-NYJ",
        home_team="Miami Dolphins",
        away_team="New York Jets",
        home_score=28,
        away_score=14,
    )
    ratings, history, _ = record_completed_game(
        store.load_ratings(),
        store.load_history(),
        game,
    )
    store.save_ratings(ratings)
    store.save_history(history)

    reloaded_store = JsonStore(tmp_path)
    reloaded_ratings = reloaded_store.load_ratings()
    reloaded_history = reloaded_store.load_history()

    assert reloaded_ratings["Miami Dolphins"] == pytest.approx(2.4)
    assert reloaded_ratings["New York Jets"] == pytest.approx(-2.4)
    assert len(reloaded_history) == 1
    assert reloaded_history[0].game_id == "2026-W02-MIA-NYJ"
