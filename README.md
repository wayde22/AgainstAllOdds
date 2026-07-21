# AgainstAllOdds

AgainstAllOdds is a beginner-friendly educational project for learning how a
simple NFL power-rating model can turn team ratings into a projected point
spread.

This project does not place wagers, scrape odds, connect to sportsbooks, handle
payments, or use real-money betting features. It is only a learning tool for
understanding the math behind an opening line.

## How The Model Works

Every NFL team starts with a rating of `0.0`.

For an upcoming game, the projected margin is:

```text
expected margin = home team rating - away team rating + home-field advantage
```

The default home-field advantage is `2.0` points. For a neutral-site game, the
home-field advantage is removed.

After a completed game, the rating change is:

```text
rating change = (actual margin - expected margin) * k
```

The default `k` value is `0.20`. Think of `k` as the learning speed. A higher
number makes ratings react faster, while a lower number makes them move more
slowly.

The home team receives the rating change, and the away team receives the equal
and opposite change.

## Worked Example

Suppose both teams start at `0.0`, the home-field advantage is `2.0`, and the
home team wins by 9.

```text
expected margin = 0.0 - 0.0 + 2.0 = 2.0
actual margin = 9
performance difference = 9 - 2.0 = 7.0
rating change = 7.0 * 0.20 = 1.4
```

The home team moves up `+1.4`, and the away team moves down `-1.4`.

## Commands

Initialize the data files:

```powershell
python main.py initialize
```

Show current ratings:

```powershell
python main.py ratings
```

Project an upcoming matchup:

```powershell
python main.py project --home "Detroit Lions" --away "Chicago Bears"
```

Project a neutral-site matchup:

```powershell
python main.py project --home "Kansas City Chiefs" --away "Buffalo Bills" --neutral-site
```

Record a completed game:

```powershell
python main.py record-game --game-id "2026-W01-DET-CHI" --home "Detroit Lions" --away "Chicago Bears" --home-score 24 --away-score 17
```

Show game history:

```powershell
python main.py history
```

List valid NFL team names:

```powershell
python main.py teams
```

## Storage

Because this was an empty starter project, version one uses readable JSON files
under `data/`:

- `data/ratings.json` stores the current rating for each NFL team.
- `data/game_history.json` stores completed games and the rating movement from
  each game.

This keeps the project easy to inspect while leaving room to move to a database
later if the app grows.

## Architecture Decisions

- The rating math lives in `src/againstallodds/ratings.py`.
- The CLI lives in `src/againstallodds/cli.py`, with `main.py` as the simple
  entry point.
- Persistence lives in `src/againstallodds/storage.py`.
- Team validation lives in `src/againstallodds/teams.py`.
- Tests live in `tests/`.
- No live odds APIs, scraping, accounts, payments, or real-money wagering
  features are included.

## Running Tests

Install the development dependency, then run the test suite:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```
