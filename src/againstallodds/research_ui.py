"""Dashboard views for research actions and saved model comparisons."""
from __future__ import annotations

import json
import sqlite3

import pandas as pd
import streamlit as st

from againstallodds.analytics import forward_results, metrics
from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.experiments import BASELINE, MODELS, compare_models, paired_interval, predict_models
from againstallodds.features import feature_dataset
from againstallodds.nfl_stats import sync_stats
from againstallodds.research_store import ResearchStore

LABELS = {BASELINE: "Power-rating baseline", "ridge-v1": "Ridge regression", "boosted-v1": "Gradient-boosted trees"}


def metric_table(rows):
    table = []
    for model in MODELS:
        selected = [r for r in rows if r["model_id"] == model]
        summary = metrics(selected)
        interval = paired_interval(selected) if selected and model != BASELINE else None
        table.append({"Model": LABELS[model], "Games": summary["games"], "Margin MAE": summary["margin_mae"], "Winner accuracy": summary["winner_accuracy"], "Line games": summary["line_games"], "Market MAE": summary["market_mae"], "Model MAE · lined games": summary["model_mae_on_lined_games"], "MAE change vs baseline": interval["delta_mae"] if interval else None, "95% interval": f"{interval['low']:+.2f} to {interval['high']:+.2f}" if interval else "—"})
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch", column_config={"Winner accuracy": st.column_config.NumberColumn(format="percent"), "Margin MAE": st.column_config.NumberColumn(format="%.3f"), "Market MAE": st.column_config.NumberColumn(format="%.3f")})
    st.caption("Lower margin MAE is better. Negative changes favor the challenger. Intervals resample season/week blocks; they are not a guarantee of future performance.")


def comparison_view(store):
    st.subheader("Model comparison")
    st.write("Train on earlier seasons, compare on later games, then follow forecasts saved before kickoff.")
    research = ResearchStore(store)
    left, right = st.columns(2)
    with left:
        refresh_history = st.checkbox("Re-download historical statistics", value=False)
        import_clicked = st.button("Import rich statistics", width="stretch")
    with right:
        st.caption("Fits both challengers and records a new experiment only when inputs or settings change.")
        compare_clicked = st.button("Run model comparison", type="primary", width="stretch")
    if import_clicked:
        with st.status("Importing seasonal play-by-play…", expanded=True) as status:
            report = sync_stats(store, refresh_history=refresh_history, progress=st.write)
            failed = [r for r in report if r["status"] == "failed"]
            status.update(label="Statistics imported" if not failed else "Some seasons unavailable; cached data retained", state="complete" if not failed else "error")
            if failed:
                st.dataframe(pd.DataFrame(failed), hide_index=True)
    if compare_clicked:
        with st.status("Running the chronological comparison…", expanded=True) as status:
            try:
                compare_models(store, progress=st.write)
                status.update(label="Comparison saved", state="complete")
            except (AgainstAllOddsError, OSError, ValueError, sqlite3.Error) as error:
                status.update(label="Comparison failed; previous results retained", state="error")
                st.error(str(error))
    stats = research.latest_stats()
    st.caption(f"Statistics cached: {len(stats)} seasons. The initial import is larger than a schedule refresh; navigation never downloads or trains models.")
    experiment = research.latest_experiment()
    if not experiment:
        st.info("Import rich statistics, then run the model comparison. Existing baseline forecasts remain available.")
        return
    result = json.loads(experiment["result"])
    st.caption(f"Saved experiment: {experiment['created_at']} · {experiment['id'][:12]} · The baseline remains the default model.")
    manifest = result["manifest"]
    changed = any(manifest["statistics"].get(str(season)) != item["id"] for season, item in stats.items() if season <= 2025)
    if changed:
        st.info("Historical statistics have changed since this experiment. These results and fitted models retain their original inputs; rerun explicitly to create another experiment.")
    stage = st.radio("Comparison period", ["Development · 2019–2023", "Held-out comparison · 2024–2025", "Saved forward predictions"], index=1)
    if stage == "Saved forward predictions":
        forward = forward_results(store, model_id=None)
        counts = {model: len([r for r in forward if r["model_id"] == model]) for model in MODELS}
        common = set.intersection(*[{r["game_id"] for r in forward if r["model_id"] == model} for model in MODELS])
        baseline = {r["game_id"]: r["predicted_margin"] for r in forward if r["model_id"] == BASELINE}
        rows = [{**r, "baseline_margin": baseline[r["game_id"]]} for r in forward if r["game_id"] in common]
        st.caption("Only games with an eligible pre-kickoff forecast from all three models enter the comparison. Historical reference lines are not used as live odds.")
        st.write({LABELS[m]: n for m, n in counts.items()})
        if not rows:
            st.info("No completed games yet have eligible saved forecasts from all three models.")
    else:
        period = "development" if stage.startswith("Development") else "test"
        rows = [r for r in result["rows"] if r["stage"] == period]
        season = st.selectbox("Comparison season", ["All", *sorted({r["season"] for r in rows})])
        rows = [r for r in rows if season == "All" or r["season"] == season]
        st.caption("2015 is feature warm-up. Settings were selected using 2019–2023 only. The 2024 and 2025 fits use preceding seasons; neither test season was used to choose settings. Historical data may contain later corrections and revised EPA estimates.")
        if period == "development":
            st.info("Development results were used to select settings and are optimistic estimates of generalization.")
    metric_table(rows)
    st.subheader("Against the spread · secondary measure")
    ats = [{"Model": LABELS[m], **threshold} for m in MODELS for threshold in metrics([r for r in rows if r["model_id"] == m])["thresholds"]]
    st.dataframe(pd.DataFrame(ats), hide_index=True, width="stretch", column_config={"win_rate": st.column_config.NumberColumn(format="percent")})
    st.caption("Absolute edges strictly above 1, 2, 3, or 5 points; pushes excluded from win rate. Historical lines have no verified observation timestamp.")
    with st.expander("Seasonal results and coverage"):
        st.dataframe(pd.DataFrame([{k: v for k, v in r.items() if k != "thresholds"} for r in result["seasons"]]), hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame(result["coverage"]), hide_index=True, width="stretch")
        if result["exclusions"]:
            st.dataframe(pd.DataFrame(result["exclusions"]), hide_index=True, width="stretch")
    with st.expander("Model settings and associations"):
        st.json(result["settings"])
        st.caption("Ridge coefficients belong to the frozen 2026 model and use standardized features. Correlated inputs can share or reverse weights; these are associations, not causal effects.")
        st.dataframe(pd.DataFrame(result["coefficients"]), hide_index=True, width="stretch")
        st.json(result["validation"])


def upcoming_comparison(store, format_line):
    rows = predict_models(store)
    if not rows:
        st.info("No eligible upcoming games within seven days.")
        return
    table = {}
    for row in rows:
        item = table.setdefault(row["game_id"], {"Date": row["gameday"], "Away": row["away_team"], "Home": row["home_team"]})
        item[LABELS[row["model_id"]]] = format_line(row) if row["available"] else row["exclusion"]
    st.dataframe(pd.DataFrame(table.values()), hide_index=True, width="stretch")
    st.caption("Challengers use frozen annual fits with updated pregame features. Estimates are saved on successful refreshes; simply viewing this table does not create predictions.")


def rolling_history(store, team):
    if not ResearchStore(store).latest_stats():
        return
    _, rows, _ = feature_dataset(store, persist=False)
    values = []
    for row in rows:
        side = "home_stats" if row["home_team"] == team else "away_stats" if row["away_team"] == team else None
        if side:
            values.append({"Date": row["gameday"], "Game": row["game_id"], **row[side], "Coverage": row["exclusion"] or "Available"})
    st.subheader("Pregame rolling statistics")
    window = st.selectbox("Previous games", [5, 16])
    frame = pd.DataFrame(values)
    if not frame.empty:
        columns = [f"off_epa_{window}", f"def_epa_{window}"]
        st.line_chart(frame.set_index("Date")[columns])
        st.caption("Defensive EPA measures EPA allowed; lower is better. Windows carry across seasons. Values precede each listed game.")
        st.dataframe(frame[["Date", "Game", *[c for c in frame if c.endswith(f"_{window}")], "Coverage"]], hide_index=True, width="stretch")
