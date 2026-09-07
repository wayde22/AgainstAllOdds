# AgainstAllOdds

A local NFL analytics dashboard that downloads real nflverse schedules and scores,
evaluates a power-rating baseline, and records predictions before kickoff.
Data stays on your computer. No API key or paid subscription is required.

## Run on Windows

Use Python 3.14 and run these commands from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe main.py dashboard
```

The dashboard runs at `http://127.0.0.1:8501`. It downloads data on first use.
Later launches refresh if the last successful check is at least six hours old.
**Refresh data** downloads on demand. Failed downloads leave cached data
available. Switching views does not download.

**Games** shows schedules, scores, and projected spreads. Use the compact
**Projection model**, **Season**, and **Week** filters on one row to narrow the
table. Upcoming estimates cover seven days; missing kickoff times and warm-up
games have no projection.
**Teams** shows ratings and their history. **Performance** separates historical
simulations from predictions actually saved before kickoff.

## Analytics commands

```powershell
python main.py sync-nfl
python main.py sync-nfl --start-season 2015 --end-season 2026
python main.py backtest
python main.py predict-week
python main.py --data-dir D:\NFLData dashboard
```

`sync-nfl` downloads, rebuilds historical results, and saves eligible predictions.
`backtest` evaluates cached data without network access. `predict-week` saves
forecasts from cached data without refreshing it. Dashboard refresh retains the
most recently imported season range. Changing the range replaces active games,
while preserving older snapshots.

## Data provenance and storage

Source: [nflverse games.csv](https://github.com/nflverse/nfldata/blob/master/data/games.csv).
See the [schedule dictionary](https://nflreadr.nflverse.com/articles/dictionary_schedules.html)
and [update schedule](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html).
The app downloads the published CSV; it does not scrape web pages.

- `data/analytics.sqlite3`: games, source snapshots, import attempts, model runs,
  configurations, and saved predictions.
- `data/raw/nflverse/`: original CSV downloads named by SHA-256 content hash.
- `data/ratings.json` and `data/game_history.json`: separate manual experiments,
  which never influence imported analytics.

Imports validate the selected dataset before replacing active games in one
SQLite transaction. Repeated downloads do not duplicate games. Source corrections
create new snapshots and rebuilt historical results; saved forecasts remain
unchanged. Failed attempts retain the active dataset. Back up the entire data
folder to retain both the database and source files. PostgreSQL is not required.

## Baseline and evaluation

Franchises start at zero in the first imported season (2015 by default). That
season is warm-up, excluded from accuracy. Regular season and playoffs are
included. Ratings carry across seasons without offseason regression. Historical
aliases such as Oakland, San Diego, and St. Louis map to current franchises.

```text
predicted home margin = home rating - away rating + home advantage
rating change = (actual home margin - predicted home margin) * 0.20
```

Home advantage is 2 points, or zero at neutral sites. The home rating gains the
change and the away rating loses it. All games on a date use pre-date ratings;
updates apply afterward. The current Eastern date is excluded from training,
even if the feed contains scores. Both scores on an earlier date identify a
completed game. This is a daily analytics feed, not a live game-status service.
Kickoff times are converted from Eastern to UTC. The model predicts margins,
not individual team scores or win probabilities.

- **Margin MAE:** average absolute error in predicted home margin.
- **Winner accuracy:** correct winners divided by decisive predictions; actual
  ties and model pick'ems are excluded and counted separately.
- **Market comparison:** model and market MAE on the same games with lines.
- **Model edge:** predicted home margin minus historical market home margin.
  Positive edge favors the home side. nflverse's positive spread means the home
  team is favored; displayed sportsbook-style lines use a negative sign.
- **ATS results:** choose the model-preferred side when absolute edge is strictly
  above 1, 2, 3, or 5 points. Pushes are excluded from win rate. Thresholds overlap.

Historical reference lines have no verified observation timestamps in this
integration. They are retrospective benchmarks, not proof of prices available
when a prediction would have been made. Missing lines are excluded from market
metrics and their coverage is displayed. No wagering-return claim is made.

## Forward predictions

Successful refreshes save upcoming games within seven days with known kickoffs,
provided they have not started and have no result. Each prediction retains its
creation time, kickoff, model configuration, and source snapshot. Identical
inputs and training cutoffs are deduplicated; changes create revisions.

Forward evaluation uses the latest saved prediction preceding both its recorded
kickoff and the latest source kickoff, and evaluates after the game date. Source
corrections can revise results but never the stored forecast. Already-started
games cannot be backfilled; unknown kickoffs are excluded. There is no verified
live odds feed for forward ATS evaluation in this release.

## Original manual commands

```powershell
python main.py initialize
python main.py teams
python main.py ratings
python main.py project --home "Detroit Lions" --away "Chicago Bears"
python main.py record-game --game-id "manual-001" --home "Detroit Lions" --away "Chicago Bears" --home-score 24 --away-score 17
python main.py history
```

`initialize --force` resets only the manual JSON experiment, not NFL analytics.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests use deterministic CSV fixtures, temporary databases, and Streamlit AppTest.
They cover imports and rollback, corrections, aliases, chronological evaluation,
spread signs, missing data, forward eligibility, dashboard views, and offline use.
The test suite does not call the internet. `sync-nfl` is the real-source smoke check.

College football and live sportsbook APIs remain later milestones. The optional
Windows background injury checker is available from the Availability view; it
only registers after an explicit user action. Rich statistics and model
comparison are described below.

## Rich NFL statistics and model research

Choose **Model comparison** in the dashboard, then **Import rich statistics** and
**Run model comparison**. The first download caches one Parquet file per season;
2015-2025 currently occupies about 218 MB. Current-season statistics may not yet
be published by nflverse. Missing seasons and game coverage are reported, and
available historical work remains usable.

Equivalent commands:

```powershell
python main.py sync-stats
python main.py sync-stats --start-season 2015 --end-season 2025 --refresh-history
python main.py compare-models
python main.py predict-week --model all
python main.py predict-week --model ridge-v1
python main.py predict-week --model boosted-v1
```

Historical seasons are cached. The last requested season is refreshed by
`sync-stats`; `--refresh-history` explicitly refreshes the others. Once rich
statistics are initialized, ordinary **Refresh data** / `sync-nfl` also attempts
the current season and saves forecasts using the existing trained models.
Refreshing and navigating do not retrain models. Run comparison explicitly to
train or reuse an experiment for the exact input snapshot and configuration.

### Features

The play-by-play source is
[nflverse seasonal Parquet releases](https://github.com/nflverse/nflverse-data/releases/tag/pbp).
Original files and hashes are retained under `data/raw/pbp/`. Source snapshots,
team/game aggregates, feature sets, experiment reports, and fitted artifacts are
stored in SQLite. Artifacts are locally fitted Python objects; do not replace
the database with an untrusted downloaded model database. Keep the environment's
scikit-learn version with backups or rerun training after a package upgrade.

Both challengers receive home-minus-away differences over the previous 5 and
16 games, with cross-season history and pooled play counts:

- Offensive and defensive EPA per play and success rate (EPA greater than zero).
- Dropback EPA, including sacks and scrambles, and designed-rushing EPA.
- Explosive-play rate: passes of at least 20 yards or designed runs of at least 10.
- Sack rate per dropback with a known sack indicator; turnovers (interceptions
  or lost fumbles) per play with known turnover indicators.
- Window game counts, offensive/defensive play counts, and missing-value flags.
- Baseline projected margin, neutral-site status, and rest-day difference.

Penalties recorded as no-play, kneels, spikes, and special teams are excluded.
Defensive EPA is EPA allowed, so a lower value is better. Rate denominators count
valid observations for that metric, not missing values as zeros. At least three
prior games per team are required; every game in the required history must have
statistics. Missing coverage prevents a challenger forecast rather than silently
substituting the baseline. Rest is computed within the season, capped at 21 days;
season openers default to seven days.

Features use earlier game dates only. Current-game outcomes, market lines, and
market-derived probabilities are excluded from inputs. Historical EPA and
corrected source files are retrospective data, not exact archives of what was
available at each historical kickoff. Genuine forward predictions record the
source versions actually available at their creation time.

### Experiment protocol

The baseline is unchanged. The two challengers are:

- **Ridge regression** with training-only median imputation and standardization;
  regularization candidates 1, 10, and 100.
- **Histogram gradient-boosted trees** with absolute-error loss, 200 iterations,
  learning rate 0.05, minimum leaf size 30, no random early-stopping split, and
  seed 42. Compare 7/15 maximum leaves and L2 regularization 1/10.

2015 warms up features. Each validation fold trains on 2016 through the preceding
season and predicts one of 2019-2023. Pooled validation MAE selects each model's
settings; ties prefer smaller trees and stronger regularization. All preprocessing
is fitted inside each training fold.

Settings are frozen before the 2024-2025 comparison. The 2024 fit trains through
2023; the 2025 fit trains through 2024. Neither test season chooses settings.
The 2026 models train through 2025 and keep their fitted parameters for the
season while their rolling pregame inputs update. Models are not automatically
promoted; the baseline stays the default. The baseline's historical aggregate
results were already inspected, so the entire historical exercise is not a
previously untouched experiment.

The leaderboard compares the same eligible games for every model. Market
comparisons use the common subset with historical lines. Development results
are labeled as used for tuning. Reports include seasonal results, exclusions,
ATS thresholds, and paired MAE differences from the baseline. A negative MAE
difference favors the challenger. The 95% intervals use 2,000 season/week-block
bootstrap samples with seed 42; they are uncertainty estimates, not promises.

The Games view has a model selector and an upcoming side-by-side table. Teams
includes rolling-stat histories. Model comparison shows settings, standardized
Ridge coefficients (associations, not causes), and development/test/forward
filters. Saved forward results choose the latest valid forecast per game *and*
model. Comparisons require forecasts from all three models on the same game.
All existing baseline predictions are preserved by the database migration.

### Quarterback availability

The **Availability** dashboard view uses the free official [NFL injury report](https://www.nfl.com/injuries/)
and [inactive list](https://www.nfl.com/inactives/). It stores the original page
and parsed rows before making an adjustment. Build rolling 16-game quarterback
profiles from the retained play-by-play data, then calculate a separate upcoming
forecast. The baseline and challenger historical comparisons are never rewritten.

In the dashboard, open **Availability**, then use these controls in order:

1. Choose **Sync official injury report**; near kickoff, also choose **Sync official inactives**.
2. Choose **Build QB profiles** after play-by-play statistics have been imported.
3. Select the base projection model and choose **Calculate upcoming availability-adjusted forecasts**.
4. Choose **Plan injury-report checks** to store the 7d/72h/24h/2h/85m/15m windows. Select **windows** and enable the background checker only when you want Windows Task Scheduler to run due checks every 15 minutes.

The forecast table shows the original home-margin estimate and the adjusted
home-margin estimate. A positive adjustment favors the home team; a negative
adjustment favors the away team. The underlying report, QB profiles, status
weight, replacement, and calculation are retained in the local database.
The first assessment establishes a baseline; later starter, status, or
0.5-point-or-larger adjustment changes appear in the dashboard alert panel and
can trigger the optional Windows toast notification.

```powershell
python main.py sync-injuries
python main.py build-qb-profiles
python main.py predict-with-availability --model ridge-v1
python main.py set-expected-qb --game-id 2026_01_NE_SEA --team "Seattle Seahawks" --player "Expected Quarterback" --reason "Confirmed starter"
python main.py enable-windows-injury-checks
```

Out/inactive carries a 100% absence probability, doubtful 80%, questionable or
DNP 50%, limited 25%, and full participation 0%. The adjustment uses the
expected starter-versus-replacement difference in shrinkage-adjusted EPA per
dropback, expected dropbacks, and a conservative 0.65 multiplier, capped at
seven points. `set-expected-qb` saves a named override and its reason. Check
windows are 7 days, 72 hours, 24 hours, 2 hours, 85 minutes, and 15 minutes
before kickoff. The dashboard can keep that plan locally, or the explicitly
enabled Windows runner can execute due checks every 15 minutes.

Experiments and their fitted models are immutable. Changed historical statistics
require an explicit new experiment; failed downloads or training retain the
previous usable data and models. Complete database and raw-file backups preserve
provenance. College football, live odds, non-QB player adjustments,
opponent-adjusted ratings, individual team scores, and automatic promotion remain future work.
