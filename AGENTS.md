# AgainstAllOdds contributor guidance

## Project

AgainstAllOdds is a local NFL analytics dashboard and CLI. It uses free public
nflverse data plus the official NFL injury and inactive reports. Keep data local;
do not add paid data providers or API-key requirements without an explicit user
request.

Key areas:

- `main.py` and `src/againstallodds/cli.py`: command-line entry points.
- `src/againstallodds/dashboard.py`: Streamlit interface.
- `analytics_store.py`, `research_store.py`, and `injuries.py`: SQLite-backed
  source snapshots, model artifacts, and availability audit records.
- `tests/`: deterministic fixtures and Streamlit AppTest coverage.

## Common commands

Run commands from the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe main.py dashboard
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe main.py sync-nfl
.\.venv\Scripts\python.exe main.py sync-stats
.\.venv\Scripts\python.exe main.py compare-models
.\.venv\Scripts\python.exe main.py sync-injuries
.\.venv\Scripts\python.exe main.py build-qb-profiles
.\.venv\Scripts\python.exe main.py predict-with-availability --model ridge-v1
```

Use focused tests while developing; run the relevant broader suite after a
cross-module change. Tests must not call the network. Use the CLI data-sync
commands as optional live-source smoke checks.

## Data and model rules

- Retain raw source files, immutable source snapshots, and failed-import
  history. Do not silently replace valid cached data after a failed download.
- Do not overwrite saved forward predictions. They represent what was known
  before kickoff.
- Preserve chronological evaluation: features and forecasts may use only data
  available before the game being evaluated.
- Keep baseline and challenger historical comparisons separate from
  availability-adjusted prospective forecasts.
- Treat official injury data as fallible. Show freshness, source evidence, and
  matching confidence; retain manual expected-QB overrides with a reason.
- Require an explicit user action before registering or enabling a Windows
  scheduled task or operating-system notification.

## Changes and verification

- Add focused regression tests for new behavior, including error and stale-data
  paths when applicable.
- Use Streamlit AppTest for dashboard behavior and verify labels, tables, and
  controls after UI changes.
- Update `README.md` whenever a command, data source, scheduling behavior, or
  user-visible workflow changes.
- Keep instructions here project-specific. Create a Codex skill only for a
  stable workflow that will be reused across multiple repositories.
