"""Optional, immutable pregame market-odds snapshots."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from math import erf, sqrt
from statistics import median
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.nfl_data import utcnow

API_ROOT = "https://api.odds-api.io/v3"
DEFAULT_BOOKMAKERS = "DraftKings,FanDuel"
SOURCE = "odds-api.io"


def _load_env():
    """Load local .env values without adding a runtime dependency."""
    path = os.getenv("AGAINSTALLODDS_ENV_FILE", ".env")
    try:
        for raw in open(path, encoding="utf-8"):
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


def _get_json(path, params):
    query = urlencode(params)
    request = Request(f"{API_ROOT}{path}?{query}", headers={"User-Agent": "AgainstAllOdds/0.3"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read(5_000_001)
        if len(payload) > 5_000_000:
            raise ValueError("Odds response exceeds 5 MB")
        return json.loads(payload.decode("utf-8"))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise AgainstAllOddsError(f"Odds download failed: {error}") from error


def download_odds():
    """Fetch pending NFL events and requested bookmaker spreads."""
    _load_env()
    key = os.getenv("AGAINSTALLODDS_ODDS_API_KEY")
    if not key:
        raise AgainstAllOddsError("Set AGAINSTALLODDS_ODDS_API_KEY in .env, or use import-odds with a local CSV.")
    leagues = _get_json("/leagues", {"apiKey": key, "sport": "football"})
    nfl = next((item for item in leagues if str(item.get("slug", "")).lower() == "nfl" or str(item.get("name", "")).upper() == "NFL"), None)
    if not nfl:
        raise AgainstAllOddsError("Odds provider did not return an NFL league for this API key.")
    events = _get_json("/events", {"apiKey": key, "sport": "football", "league": nfl["slug"], "status": "pending", "limit": 50})
    books = os.getenv("AGAINSTALLODDS_ODDS_BOOKMAKERS", DEFAULT_BOOKMAKERS)
    enriched = []
    for event in events:
        odds = _get_json("/odds", {"apiKey": key, "eventId": event["id"], "bookmakers": books})
        enriched.append({**event, "bookmakers": odds.get("bookmakers", odds) if isinstance(odds, dict) else {}})
    return enriched


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _spread_lines(event):
    books = event.get("bookmakers") or {}
    for bookmaker, markets in books.items():
        if not isinstance(markets, list):
            continue
        for market in markets:
            if str(market.get("name", "")).lower() != "spread":
                continue
            for outcome in market.get("odds", []):
                try:
                    spread = float(outcome["hdp"])
                except (KeyError, TypeError, ValueError):
                    continue
                yield {"bookmaker": str(bookmaker), "home_spread": spread, "source_updated_at": market.get("updatedAt")}


def normalize_events(games, events):
    """Match provider events to the local schedule and retain spread offers."""
    output = []
    for event in events:
        home, away = str(event.get("home", "")).strip(), str(event.get("away", "")).strip()
        when = _parse_time(event.get("date"))
        candidates = [g for g in games if g.home_team == home and g.away_team == away]
        if when:
            candidates = [g for g in candidates if g.kickoff and abs((_parse_time(g.kickoff) - when).total_seconds()) <= 36 * 3600]
        if len(candidates) != 1:
            continue
        for line in _spread_lines(event):
            output.append({"game_id": candidates[0].game_id, **line})
    return output


def sync_odds(store, *, fetch=None, now=None):
    now = now or utcnow()
    try:
        events = (fetch or download_odds)()
        if not isinstance(events, list):
            raise ValueError("Odds provider returned an invalid event list")
        lines = normalize_events(store.games(), events)
        payload = json.dumps(events, sort_keys=True, separators=(",", ":")).encode()
        snapshot_id = store.ingest_market(payload, lines, SOURCE, now)
        return {"snapshot_id": snapshot_id, "events": len(events), "lines": len(lines)}
    except (AgainstAllOddsError, OSError, ValueError) as error:
        store.record_market_failure(error, now)
        raise AgainstAllOddsError(str(error)) from error


def import_odds(store, payload: bytes, *, now=None):
    """Import a reviewed local CSV with bookmaker-specific home spreads."""
    now = now or utcnow()
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        required = {"home_team", "away_team", "bookmaker", "home_spread"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("CSV needs home_team, away_team, bookmaker, and home_spread columns")
        events = []
        for row in reader:
            float(row["home_spread"])
            events.append({"home": row["home_team"].strip(), "away": row["away_team"].strip(), "date": row.get("kickoff"), "bookmakers": {row["bookmaker"].strip(): [{"name": "Spread", "updatedAt": row.get("captured_at") or now.isoformat(), "odds": [{"hdp": row["home_spread"]}]}]}})
        lines = normalize_events(store.games(), events)
        snapshot_id = store.ingest_market(payload, lines, "csv", now)
        return {"snapshot_id": snapshot_id, "events": len(events), "lines": len(lines)}
    except (OSError, UnicodeError, ValueError, csv.Error) as error:
        store.record_market_failure(error, now)
        raise AgainstAllOddsError(f"Odds CSV import failed: {error}") from error


def consensus(store, *, now=None):
    """Return the latest saved median home margin for each game."""
    snapshot = store.latest_market_snapshot(before=now)
    if not snapshot:
        return {}, None
    grouped = {}
    for row in store.market_lines(snapshot["id"]):
        grouped.setdefault(row["game_id"], []).append(row)
    return ({game_id: {"market_margin": round(-median([line["home_spread"] for line in lines]), 2), "market_snapshot_id": snapshot["id"], "market_retrieved_at": snapshot["retrieved_at"], "market_books": len(lines)} for game_id, lines in grouped.items()}, snapshot)


def market_check_plan(store, *, now=None):
    now = now or utcnow(); checks = []
    for game in store.games():
        if game.complete or not game.kickoff: continue
        kickoff = _parse_time(game.kickoff)
        if kickoff is None or kickoff > now + timedelta(days=7):
            continue
        for hours, kind in ((72, "72h"), (66, "6h"), (60, "6h"), (54, "6h"), (48, "6h"), (42, "6h"), (36, "6h"), (30, "6h"), (24, "6h"), (18, "6h"), (12, "6h"), (6, "6h"), (3, "3h"), (2, "1h"), (1, "1h"), (.75, "15m"), (.5, "15m"), (.25, "15m")):
            due = kickoff - timedelta(hours=hours)
            if due > now: checks.append((game.game_id, due.isoformat(), kind))
    store.save_market_checks(checks)
    return [{"game_id": game_id, "due_at": due_at, "kind": kind, "state": "planned"} for game_id, due_at, kind in checks]


def normal_probability(margin, threshold=0., sigma=13.5):
    """Normal residual approximation; calibrated only after forward observations accrue."""
    return .5 * (1 + erf((margin - threshold) / (sigma * sqrt(2))))


def market_quality(store, forecasts):
    """Attach conservative probabilities and market movement to forecast rows."""
    output = []
    for row in forecasts:
        history = store.market_history(row["game_id"])
        current = row.get("market_margin")
        edge = row.get("edge")
        probability = normal_probability(row["predicted_margin"])
        cover = normal_probability(row["predicted_margin"], current) if current is not None else None
        signal = bool(edge is not None and abs(edge) >= 2 and cover is not None and (cover >= .56 or cover <= .44))
        if history:
            first_retrieved_at = history[0]["retrieved_at"]
            opening = -median([r["home_spread"] for r in history if r["retrieved_at"] == first_retrieved_at])
            kickoff = row.get("kickoff")
            before_kickoff = [r for r in history if not kickoff or r["retrieved_at"] <= kickoff]
            final_retrieved_at = before_kickoff[-1]["retrieved_at"] if before_kickoff else None
            final = -median([r["home_spread"] for r in before_kickoff if r["retrieved_at"] == final_retrieved_at]) if final_retrieved_at else None
        else:
            opening = None
            final = None
        output.append({**row, "opening_market_margin": opening, "final_market_margin": final, "line_movement": None if opening is None or current is None else round(current-opening,2), "closing_line_value": None if final is None or current is None else round(final-current, 2), "home_win_probability": round(probability, 3), "home_cover_probability": None if cover is None else round(cover, 3), "possible_edge": signal})
    return output


def register_windows_market_runner(data_dir):
    """Create the opt-in 15-minute market refresh task."""
    command = f'"{os.sys.executable}" "{os.path.abspath("main.py")}" --data-dir "{data_dir}" run-due-market-checks'
    result = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "15", "/TN", "AgainstAllOdds-MarketChecks", "/TR", command], capture_output=True, text=True, check=False)
    if result.returncode: raise AgainstAllOddsError(result.stderr.strip() or "Could not register market checker")
    return {"task": "AgainstAllOdds-MarketChecks"}


def run_due_market_checks(store, *, now=None):
    now = now or utcnow(); due = [c for c in store.market_checks() if c["state"] == "planned" and c["due_at"] <= now.isoformat()]
    if not due: return {"due": 0, "synced": False}
    result = sync_odds(store, now=now)
    with store.connection() as db: db.executemany("UPDATE market_checks SET state='done' WHERE game_id=? AND due_at=?", [(c["game_id"], c["due_at"]) for c in due])
    return {"due": len(due), "synced": True, **result}
