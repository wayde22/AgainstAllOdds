"""Optional, immutable pregame market-odds snapshots."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from datetime import datetime, timezone
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
