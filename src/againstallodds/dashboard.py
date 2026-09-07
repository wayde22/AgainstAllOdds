"""Local Streamlit working surface for real NFL analytics."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st

from againstallodds.analytics import backtest, forward_results, metrics, sync_nfl, upcoming
from againstallodds.analytics_store import AnalyticsStore
from againstallodds.exceptions import AgainstAllOddsError


def format_line(row):
    margin = row.get("predicted_margin")
    if margin is None:
        return "—"
    if margin == 0:
        return "Pick’em"
    return f"{row['home_team'] if margin > 0 else row['away_team']} −{abs(margin):.1f}"


def show_metrics(summary):
    cols = st.columns(4)
    cols[0].metric("Evaluated games", f"{summary['games']:,}")
    cols[1].metric("Margin error · MAE", "—" if summary["margin_mae"] is None else f"{summary['margin_mae']:.2f} pts")
    cols[2].metric("Winner accuracy", "—" if summary["winner_accuracy"] is None else f"{summary['winner_accuracy']:.1%}")
    cols[3].metric("Historical line coverage", f"{summary['line_games']:,} / {summary['games']:,}")
    st.caption(f"Winner samples: {summary['winner_samples']:,} · Actual ties: {summary['ties']} · Model pick’ems: {summary['pickems']} · Missing lines: {summary['missing_lines']:,}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    args, _ = parser.parse_known_args()
    st.set_page_config(page_title="AgainstAllOdds · NFL analytics", page_icon="🏈", layout="wide")
    st.markdown("""<style>
    .stApp {background:#0b1320;color:#edf2fa}
    [data-testid="stSidebar"] {background:#111e31}
    h1,h2,h3 {letter-spacing:-.025em}
    [data-testid="stMetric"] {border-top:3px solid #43d9aa;padding:16px 8px;background:#132138}
    </style>""", unsafe_allow_html=True)
    st.title("AgainstAllOdds")
    st.caption("NFL analytics · Ratings, richer statistics, and model research")
    store = AnalyticsStore(args.data_dir)
    with st.sidebar:
        st.header("NFL workspace")
        view = st.radio("View", ["Games", "Teams", "Performance", "Model comparison", "Availability"])
        refresh = st.button("Refresh data", type="primary", width="stretch")
    session_key = f"initial_refresh:{store.path.resolve()}"
    should_refresh = refresh or (session_key not in st.session_state and store.stale())
    st.session_state[session_key] = True
    if should_refresh:
        prior = store.latest()
        try:
            with st.spinner("Downloading NFL history and rebuilding the baseline…"):
                refresh_result = sync_nfl(store, prior["start_season"] if prior else 2015, prior["end_season"] if prior else 2026)
                if any(r["status"] == "failed" for r in refresh_result.get("statistics", [])):
                    st.warning("Schedule refreshed; some statistics remain cached. Challenger forecasts require complete prior-game coverage.")
        except (AgainstAllOddsError, OSError, sqlite3.Error) as error:
            st.error(f"Refresh failed. {error}")
    snapshot = store.latest()
    if not snapshot:
        st.info("No NFL data has been imported yet. Connect to the internet and choose Refresh data. No API key is needed.")
        st.stop()
    if store.last_error():
        st.warning("The last refresh failed. Showing the last successful import; you can keep exploring it offline.")
    elif store.stale():
        st.warning("The saved data is more than six hours old. Refresh for the latest schedule.")
    st.caption(f"Source: nflverse · Last checked: {snapshot['checked_at']} · Seasons: {snapshot['start_season']}–{snapshot['end_season']}")
    if view == "Model comparison":
        from againstallodds.research_ui import comparison_view
        comparison_view(store)
        return
    if view == "Availability":
        from againstallodds.injuries import AvailabilityStore, build_qb_profiles, build_wr_profiles, sync_injuries, injury_adjusted_predictions, plan_injury_checks, current_report, report_freshness
        from againstallodds.experiments import predict_models
        availability = AvailabilityStore(store)
        st.subheader("Quarterback availability")
        st.caption("Official NFL reports are saved as immutable snapshots. The adjustment applies only to prospective forecasts and is separate from historical model scores.")
        left, middle, right, receiver = st.columns(4)
        if left.button("Sync official injury report", width="stretch"):
            try:
                st.success(f"Saved {sync_injuries(store)['records']} report rows.")
            except (AgainstAllOddsError, OSError, sqlite3.Error) as error:
                st.error(f"Report sync failed: {error}")
        if middle.button("Sync official inactives", width="stretch"):
            try:
                st.success(f"Saved {sync_injuries(store, source='inactives')['records']} inactive rows.")
            except (AgainstAllOddsError, OSError, sqlite3.Error) as error:
                st.error(f"Inactive sync failed: {error}")
        if right.button("Build QB profiles", width="stretch"):
            try:
                st.success(f"Built {build_qb_profiles(store)['players']} rolling QB profiles.")
            except (AgainstAllOddsError, OSError, sqlite3.Error) as error:
                st.error(f"Profile build failed: {error}")
        if receiver.button("Build WR profiles", width="stretch"):
            try:
                st.success(f"Built {build_wr_profiles(store)['players']} rolling WR profiles.")
            except (AgainstAllOddsError, OSError, sqlite3.Error) as error:
                st.error(f"WR profile build failed: {error}")
        report = current_report(store)
        profile = availability.latest_profile()
        wr_profile = availability.latest_wr_profile()
        check_mode = st.radio("Scheduled-check mode", ["dashboard", "windows"], horizontal=True, help="Dashboard records check windows for this app. Windows records the same plan for a future background runner; it does not create an operating-system task until that runner is enabled.")
        toasts = st.checkbox("Send Windows toast alerts for material QB changes", value=availability.setting("windows_toasts", True))
        availability.set_setting("windows_toasts", toasts)
        if st.button("Plan injury-report checks"):
            plans = plan_injury_checks(store, mode=check_mode)
            st.success(f"Planned {len(plans)} checks at 7d, 72h, 24h, 2h, 85m, and 15m before upcoming kickoffs.")
        if check_mode == "windows" and st.button("Enable 15-minute Windows background checker"):
            try:
                from againstallodds.injuries import register_windows_runner
                st.success(f"Registered {register_windows_runner(store.root)['task']}.")
            except (AgainstAllOddsError, OSError) as error:
                st.error(f"Windows scheduler setup failed: {error}")
        freshness = report_freshness(report)
        status, alerts = st.columns([1, 1])
        status.write({"latest_official_snapshot": report.get("retrieved_at"), "report_age_hours": freshness["age_hours"], "latest_qb_profile": profile and profile["generated_at"], "latest_WR_profile": wr_profile and wr_profile["generated_at"]})
        if not freshness["fresh"]:
            status.warning("Official availability data is stale or unavailable. Sync it before relying on an adjustment.")
        changes = availability.changes()
        if changes:
            alerts.warning(f"{len(changes)} material quarterback availability change(s) recorded.")
            alerts.dataframe(pd.DataFrame([{"Detected": c["detected_at"], "Team": c["team"], "Game": c["game_id"], "Windows notified": bool(c["notified_at"])} for c in changes]), hide_index=True, width="stretch")
        else:
            alerts.success("No material availability changes detected yet.")
        if report["records"]:
            records = report["records"]
            st.subheader("Reconciled official report")
            st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch")
        else:
            st.info("Sync the official injury report before calculating an availability adjustment.")
        if profile:
            profiles = json.loads(profile["profiles"])
            upcoming_games = [game for game in store.games() if not game.complete]
            if upcoming_games:
                st.subheader("Expected-QB override")
                game = st.selectbox("Game", upcoming_games, format_func=lambda g: f"{g.away_team} at {g.home_team} · {g.gameday}")
                team = st.selectbox("Team", [game.away_team, game.home_team])
                choices = [player for player in profiles if player["team"] == team]
                if choices:
                    selected = st.selectbox("Expected quarterback", choices, format_func=lambda player: player["name"])
                    reason = st.text_input("Override reason")
                    save_override, clear_override = st.columns(2)
                    if save_override.button("Save expected-QB override"):
                        if reason.strip():
                            availability.override(game.game_id, team, selected["player_id"], selected["name"], reason.strip(), __import__("againstallodds.nfl_data", fromlist=["utcnow"]).utcnow())
                            st.success("Expected-QB override saved.")
                        else:
                            st.error("Enter a reason before saving an override.")
                    if clear_override.button("Clear expected-QB override"):
                        availability.clear_override(game.game_id, team)
                        st.success("Expected-QB override cleared.")
            st.subheader("QB performance inputs")
            st.dataframe(pd.DataFrame(profiles), hide_index=True, width="stretch")
            if wr_profile:
                st.subheader("WR performance inputs")
                st.caption("WR adjustments use rolling receiving EPA per game, matched official status, and a conservative replacement gap. They are prospective-only and capped at three points per team.")
                st.dataframe(pd.DataFrame(json.loads(wr_profile["profiles"])), hide_index=True, width="stretch")
            model = st.selectbox("Base projection model", ["power-rating-v1", "ridge-v1", "boosted-v1"], key="availability-model")
            if st.button("Calculate upcoming availability-adjusted forecasts", type="primary"):
                try:
                    rows = injury_adjusted_predictions(store, predict_models(store, model=model))
                    shown = [{"game_id": r["game_id"], "away": r["away_team"], "home": r["home_team"], "base_margin": r["predicted_margin"], "adjusted_margin": r["injury_adjusted_margin"], "home_expected_QB": r["availability_assessments"][0]["expected_qb_name"], "home_WR_adjustment": r["wr_assessments"][0]["adjustment"], "home_reason": r["availability_assessments"][0]["reason"], "away_expected_QB": r["availability_assessments"][1]["expected_qb_name"], "away_WR_adjustment": r["wr_assessments"][1]["adjustment"], "away_reason": r["availability_assessments"][1]["reason"]} for r in rows if r.get("predicted_margin") is not None]
                    st.dataframe(pd.DataFrame(shown), hide_index=True, width="stretch")
                except (AgainstAllOddsError, OSError, sqlite3.Error) as error:
                    st.error(f"Availability forecast failed: {error}")
        st.caption("Status weights: Out/Inactive 100%, Doubtful 80%, Questionable or DNP 50%, Limited 25%, Full 0%. A manual expected-QB override is available from the command line and retained with its reason.")
        return
    result = backtest(store)
    games = store.games(snapshot)
    if view == "Games":
        from againstallodds.research_ui import LABELS, upcoming_comparison
        from againstallodds.research_store import ResearchStore
        seasons = sorted({g.season for g in games}, reverse=True)
        filter_model, filter_season, filter_week, _ = st.columns([1.45, .7, .55, 1.3])
        with filter_model:
            model = st.selectbox("Projection model", list(LABELS), format_func=LABELS.get)
        with filter_season:
            season = st.selectbox("Season", seasons)
        weeks = sorted({g.week for g in games if g.season == season})
        with filter_week:
            week = st.selectbox("Week", ["All", *weeks])
        projections = {r["game_id"]: r for r in [*result["rows"], *upcoming(store)]}
        if model != "power-rating-v1":
            from againstallodds.experiments import predict_models
            experiment = ResearchStore(store).latest_experiment()
            saved_rows = json.loads(experiment["result"])["rows"] if experiment else []
            projections = {r["game_id"]: r for r in saved_rows if r["model_id"] == model}
            projections.update({r["game_id"]: r for r in predict_models(store, model=model)})
        display = []
        for game in games:
            if game.season != season or (week != "All" and game.week != week):
                continue
            p = projections.get(game.game_id, {})
            display.append({"Date": game.gameday, "Week": game.week, "Stage": game.game_type, "Away": game.away_team, "Home": game.home_team, "Score · away–home": f"{game.away_score}–{game.home_score}" if game.complete else "Pending", "Projected line": format_line(p), "Projection type": p.get("exclusion") or ("Historical simulation" if "actual_margin" in p else "Upcoming estimate" if p else "Not available"), "Neutral": game.neutral_site})
        st.subheader("Schedule & projected lines")
        st.dataframe(pd.DataFrame(display), hide_index=True, width="stretch")
        st.caption("Upcoming estimates cover the next seven days. Historical predictions use only earlier game dates. Scores on the current game date may still be provisional.")
        if ResearchStore(store).latest_experiment():
            st.subheader("Upcoming · all three models")
            upcoming_comparison(store, format_line)
    elif view == "Teams":
        st.subheader("Power ratings")
        st.caption("Points above or below an average team, using results through the previous Eastern date.")
        st.dataframe(pd.DataFrame(result["ratings"], columns=["Team", "Rating"]), hide_index=True, width="stretch")
        team = st.selectbox("Team history", sorted(dict(result["ratings"])))
        history = pd.DataFrame([r for r in result["history"] if r["team"] == team])
        if not history.empty:
            st.line_chart(history.set_index("date")[["rating"]], color="#43d9aa")
        else:
            st.info("No completed games available to build rating history.")
        from againstallodds.research_ui import rolling_history
        rolling_history(store, team)
    else:
        mode = st.radio("Evaluation", ["Historical simulation", "Saved forward predictions"], horizontal=True)
        if mode == "Historical simulation":
            choices = ["All", *sorted({r["season"] for r in result["rows"]})]
            season = st.selectbox("Evaluation season", choices)
            rows = [r for r in result["rows"] if season == "All" or r["season"] == season]
            summary = metrics(rows)
            st.subheader("Historical performance")
            st.caption(f"{snapshot['start_season']} is warm-up and excluded. Ratings carry across seasons. Reference lines have no verified observation timestamp and are used only for retrospective comparison.")
            show_metrics(summary)
            if summary["market_mae"] is not None:
                st.write(f"On the same games with lines — model MAE: **{summary['model_mae_on_lined_games']:.2f} points** · market MAE: **{summary['market_mae']:.2f} points**.")
            st.subheader("Against the spread · model edge")
            st.dataframe(pd.DataFrame(summary["thresholds"]), hide_index=True, width="stretch")
            st.caption("Thresholds use absolute edge strictly above the displayed points. Pushes are excluded from win rate. Percentages are fractions in this table.")
            yearly = pd.DataFrame([{k: v for k, v in s.items() if k != "thresholds"} for s in result["seasons"]])
            st.subheader("By season")
            st.dataframe(yearly, hide_index=True, width="stretch")
        else:
            st.subheader("Saved before kickoff")
            rows = forward_results(store)
            show_metrics(metrics(rows))
            st.caption("Uses the latest eligible prediction saved before kickoff. Later imports preserve the original prediction; final results may reflect source corrections. No verified live market feed is connected.")
            if rows:
                st.dataframe(pd.DataFrame(rows)[["game_id", "saved_at", "predicted_margin", "actual_margin"]], hide_index=True, width="stretch")
            else:
                st.info("No saved predictions have eligible completed results yet. Successful refreshes save upcoming games within seven days; games already underway are never backfilled.")


if __name__ == "__main__":
    main()
