"""Read-only, in-app reference for the AgainstAllOdds dashboard."""
from __future__ import annotations

import streamlit as st


def _table(rows):
    st.table(rows)


def render_help():
    """Render the dashboard guide without reading or changing local data."""
    st.subheader("Help & guide")
    st.caption("A plain-language reference for every dashboard view, control, table, and metric. Reading this guide never refreshes data or changes a saved forecast.")

    with st.expander("Start here · game-day workflow", expanded=True):
        st.markdown("""
1. In **Games**, choose the projection model, season, and week that you want to review.
2. In **Availability**, sync the official injury report; closer to kickoff, sync inactives and update player profiles.
3. In **Market quality**, refresh venue weather and import a reviewed market CSV. Then, in **Availability**, choose **Save current all-model forecasts** to retain the complete pre-kickoff record.
4. After games finish, choose **Refresh data**, then open **Performance → Saved forward predictions** to compare saved forecasts with final results.
""")

    with st.expander("Navigation and global buttons"):
        _table([
            {"Item": "Games", "Meaning": "Schedule, completed scores, projected lines, current market consensus, and model edge."},
            {"Item": "Teams", "Meaning": "Current power ratings, rating history, and retained rolling team statistics."},
            {"Item": "Performance", "Meaning": "Historical baseline simulations and saved pre-kickoff forecast results."},
            {"Item": "Model comparison", "Meaning": "Train and compare the baseline, Ridge, and boosted-tree research models."},
            {"Item": "Market quality", "Meaning": "Saved odds movement, forecast-time weather, probabilities, and market coverage."},
            {"Item": "Availability", "Meaning": "Official injury/inactive reports and prospective player-availability adjustments."},
            {"Item": "Help & guide", "Meaning": "This read-only reference. Opening it never refreshes, saves, trains, imports, or schedules anything."},
            {"Item": "Refresh data", "Meaning": "Downloads the latest nflverse schedule/results and rebuilds cached baseline outputs. A failed refresh keeps the last successful data."},
            {"Item": "Refresh market odds", "Meaning": "Uses the optional configured odds provider. Without an API key, import a reviewed CSV from Games instead."},
        ])

    with st.expander("Data freshness lights"):
        _table([
            {"Item": "NFL data", "Meaning": "The saved time of the last successful nflverse data check. It shows the imported season range."},
            {"Item": "Market data", "Meaning": "The saved retrieval time and source of the newest immutable odds snapshot."},
            {"Item": "Green / yellow / red / gray", "Meaning": "Under two hours / two to under six hours / six or more hours / no saved snapshot. NFL and market data are measured independently."},
            {"Item": "Timestamp display", "Meaning": "Dashboard timestamps are shown in Central time. Storage and freshness calculations remain in UTC."},
        ])

    with st.expander("Games · filters, upload, and schedule grid"):
        _table([
            {"Control or column": "Projection model", "Meaning": "Select the power-rating baseline, Ridge regression, or boosted trees. The baseline is the default."},
            {"Control or column": "Feature family", "Meaning": "For challenger models, choose raw rolling rates, opponent-normalized rates, or season-long SRS inputs."},
            {"Control or column": "Season / Week", "Meaning": "Limit the schedule grid to the season and week you want to inspect."},
            {"Control or column": "Import reviewed market CSV", "Meaning": "Upload a local CSV with home_team, away_team, bookmaker, and home_spread; kickoff and captured_at are optional."},
            {"Control or column": "Save imported market odds", "Meaning": "Stores the uploaded CSV as an immutable market snapshot. It never replaces an older snapshot."},
            {"Control or column": "Projected line", "Meaning": "The selected model’s projected spread. ‘Seattle Seahawks −3.5’ means Seattle is projected to win by 3.5 points."},
            {"Control or column": "Market consensus / Model edge / Books", "Meaning": "Median saved sportsbook spread; model margin minus market margin; and number of saved bookmaker offers."},
            {"Control or column": "Projection type / Neutral", "Meaning": "Explains whether the row is historical or upcoming and whether home-field advantage was removed."},
        ])

    with st.expander("Teams and Performance"):
        _table([
            {"Item": "Power ratings", "Meaning": "Points above or below an average team from results through the previous game date."},
            {"Item": "Team history", "Meaning": "Choose a team to view how its rating changed after completed games."},
            {"Item": "Previous games", "Meaning": "Choose the 5-game or 16-game rolling window for a team's pregame statistics when rich statistics are available."},
            {"Item": "Evaluation", "Meaning": "Switch between historical simulations and forecasts actually saved before kickoff."},
            {"Item": "Evaluation season", "Meaning": "Limits the historical performance tables to one season or all available seasons."},
            {"Item": "Margin MAE", "Meaning": "Average absolute error in the predicted home scoring margin. Lower is better."},
            {"Item": "Winner accuracy", "Meaning": "Share of decisive games where the projected winner was correct."},
            {"Item": "Historical line coverage", "Meaning": "Games with a retained reference spread divided by all evaluated games. Historical lines are not verified forecast-time prices."},
            {"Item": "Against the spread grid", "Meaning": "Results when the model preferred a side by more than the stated edge threshold. Pushes are excluded from the win rate."},
        ])

    with st.expander("Model comparison"):
        _table([
            {"Item": "Feature family", "Meaning": "The input family used for the research comparison; it does not automatically change the default forecast."},
            {"Item": "Import rich statistics", "Meaning": "Downloads and retains nflverse play-by-play season files needed for challenger features."},
            {"Item": "Re-download historical statistics", "Meaning": "Explicitly refreshes previously cached seasons; normally historical files stay cached."},
            {"Item": "Run model comparison", "Meaning": "Runs the chronological experiment and stores an immutable report and fitted local artifacts."},
            {"Item": "Comparison period", "Meaning": "View development, held-out 2024–2025, or saved-forward results. Development performance helped choose settings."},
            {"Item": "Leaderboard / seasonal grids", "Meaning": "Compare model error and coverage on the stated shared eligible games; negative paired MAE difference favors a challenger."},
        ])

    with st.expander("Market quality"):
        _table([
            {"Item": "Sync venue weather", "Meaning": "Saves an immutable Open-Meteo forecast-time response for venues within the next seven days."},
            {"Item": "Plan market checks", "Meaning": "Stores local 72-hour, six-hour, hourly, and final-hour collection windows. It does not fetch or schedule anything by itself."},
            {"Item": "Enable 15-minute Windows market checker", "Meaning": "Explicitly registers a Windows task for configured-provider checks. Do not use it for the manual CSV workflow."},
            {"Item": "Forecast-time market-line coverage", "Meaning": "Upcoming games with a saved current market line divided by all upcoming games."},
            {"Item": "Opening / Current / Final pre-kickoff", "Meaning": "First saved consensus, latest saved consensus, and latest saved consensus before kickoff."},
            {"Item": "Movement / Closing-line movement", "Meaning": "Current minus opening margin, and final pre-kickoff margin minus the forecast-time margin. Positive values favor the home team."},
            {"Item": "Home win / cover probability", "Meaning": "Initial conservative probabilities derived from projected margin. They need forward results before calibration can be judged."},
            {"Item": "Possible edge", "Meaning": "An analytical signal requiring a large enough model-market difference and conservative probability threshold. It is not a recommendation."},
        ])

    with st.expander("Availability"):
        _table([
            {"Item": "Sync official injury report / inactives", "Meaning": "Fetches and preserves the official report or near-kickoff inactive list as a source snapshot."},
            {"Item": "Build QB, WR, RB/TE, or EDGE profiles", "Meaning": "Builds rolling player-performance inputs from retained play-by-play data."},
            {"Item": "Scheduled-check mode", "Meaning": "Dashboard stores local check windows. Windows permits later opt-in background checks but does not register a task by itself."},
            {"Item": "Send Windows toast alerts", "Meaning": "Controls local Windows notifications for material quarterback changes when notifications are available."},
            {"Item": "Plan injury-report checks", "Meaning": "Stores report windows before upcoming kickoffs."},
            {"Item": "Enable 15-minute Windows background checker", "Meaning": "Explicitly registers a Windows task to run due official-report checks. It is optional and only appears after selecting Windows scheduled-check mode."},
            {"Item": "Update all availability data", "Meaning": "Refreshes the official injury and inactive reports, then rebuilds QB, WR, RB/TE, and EDGE profiles. A failed step is reported while later steps still run."},
            {"Item": "Base projection model", "Meaning": "Chooses the one model used by the review-only availability forecast below."},
            {"Item": "Save current all-model forecasts", "Meaning": "Saves the available baseline, Ridge, and boosted-tree forecasts, then their availability-adjusted counterparts. Each use keeps a timestamped pre-kickoff record and never overwrites an earlier one."},
            {"Item": "Calculate upcoming availability-adjusted forecasts", "Meaning": "Calculates and displays prospective availability adjustments for the selected base model. It is useful for review; use Save current all-model forecasts to record every available model at a checkpoint."},
            {"Item": "Expected-QB override", "Meaning": "Choose a known expected starter and record a reason. The override is retained for review."},
            {"Item": "Save / Clear expected-QB override", "Meaning": "Saves the selected quarterback only when a reason is supplied, or removes the existing override for that game and team."},
        ])

    with st.expander("Terms and interpretation notes"):
        st.markdown("""
- **Home margin:** positive means the home team is favored or won; negative means the away team is favored or won. A sportsbook-style displayed favorite has a minus sign.
- **Market consensus:** the median home spread across the bookmaker lines in one saved snapshot. One bookmaker is still a usable observation, but more books create a stronger consensus.
- **Model edge:** projected home margin minus market home margin. A positive edge favors the home team; a negative edge favors the away team.
- **Brier score and calibration:** Brier score measures probability error; calibration asks whether, for example, teams given a 70% win probability win about 70% of the time. Both require completed saved forecasts.
- **Saved forward prediction:** a prediction stored before kickoff with its timestamp, model configuration, and available market context. This is the most reliable evaluation record.
- **Historical reference line:** a retrospective line in source history. It can support a historical comparison but does not prove a price was available at forecast time.

Model outputs are estimates, not guarantees. Market data stays out of football-strength training, and a possible edge is an analytical signal rather than a recommendation. Missing weather, market, or availability data leaves the other dashboard outputs usable.
""")
