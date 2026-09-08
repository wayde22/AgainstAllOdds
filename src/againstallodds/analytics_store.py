"""Local, versioned analytics storage; independent of manual JSON ratings."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from againstallodds.nfl_data import NFLGame, SOURCE_URL, utcnow


class AnalyticsStore:
    def __init__(self, data_dir="data"):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "analytics.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                    source TEXT NOT NULL, raw_path TEXT NOT NULL,
                    start_season INTEGER NOT NULL, end_season INTEGER NOT NULL,
                    retrieved_at TEXT NOT NULL, games_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY, attempted_at TEXT NOT NULL,
                    snapshot_id TEXT REFERENCES snapshots(id), error TEXT);
                CREATE TABLE IF NOT EXISTS games (
                    game_id TEXT PRIMARY KEY, season INTEGER NOT NULL,
                    gameday TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
                    kind TEXT NOT NULL, created_at TEXT NOT NULL,
                    config TEXT NOT NULL, result TEXT NOT NULL,
                    UNIQUE(snapshot_id, kind, config));
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
                    game_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    kickoff TEXT NOT NULL, config TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(snapshot_id, game_id, config));
                CREATE INDEX IF NOT EXISTS predictions_game ON predictions(game_id, created_at);
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, source TEXT NOT NULL,
                    raw_path TEXT NOT NULL, retrieved_at TEXT NOT NULL, records_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS market_imports (
                    id INTEGER PRIMARY KEY, attempted_at TEXT NOT NULL,
                    snapshot_id TEXT REFERENCES market_snapshots(id), error TEXT);
                CREATE TABLE IF NOT EXISTS market_lines (
                    snapshot_id TEXT NOT NULL REFERENCES market_snapshots(id), game_id TEXT NOT NULL,
                    bookmaker TEXT NOT NULL, home_spread REAL NOT NULL, source_updated_at TEXT,
                    PRIMARY KEY (snapshot_id, game_id, bookmaker, home_spread));
                CREATE INDEX IF NOT EXISTS market_lines_game ON market_lines(game_id, snapshot_id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(predictions)")}
            if "model_id" not in columns:
                db.execute("ALTER TABLE predictions ADD COLUMN model_id TEXT NOT NULL DEFAULT 'power-rating-v1'")
            if "artifact_id" not in columns:
                db.execute("ALTER TABLE predictions ADD COLUMN artifact_id TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS predictions_model_game ON predictions(model_id,game_id,created_at)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def latest(self):
        with self.connection() as db:
            row = db.execute("SELECT s.*, i.attempted_at AS checked_at FROM imports i JOIN snapshots s ON s.id=i.snapshot_id WHERE i.error IS NULL ORDER BY i.id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def last_error(self):
        with self.connection() as db:
            row = db.execute("SELECT error FROM imports ORDER BY id DESC LIMIT 1").fetchone()
            return row[0] if row else None

    def stale(self, now=None):
        latest = self.latest()
        return not latest or (now or utcnow()) - datetime.fromisoformat(latest["checked_at"]) >= timedelta(hours=6)

    def record_failure(self, error, now):
        with self.connection() as db:
            db.execute("INSERT INTO imports(attempted_at,error) VALUES (?,?)", (now.isoformat(), str(error)))

    def ingest(self, payload: bytes, games: list[NFLGame], start: int, end: int, now):
        digest = hashlib.sha256(payload).hexdigest()
        snapshot_id = f"{digest}:{start}:{end}"
        raw_dir = self.root / "raw" / "nflverse"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{digest}.csv"
        if not raw_path.exists():
            # Publish complete bytes atomically under their content hash.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=raw_dir, suffix=".tmp", delete=False) as file:
                    temporary = Path(file.name)
                    file.write(payload)
                    file.flush()
                    os.fsync(file.fileno())
                temporary.replace(raw_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        serialized = json.dumps([g.to_dict() for g in games], sort_keys=True)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?)", (snapshot_id, digest, SOURCE_URL, str(raw_path), start, end, now.isoformat(), serialized))
            db.execute("DELETE FROM games")
            db.executemany("INSERT INTO games VALUES (?,?,?,?)", [(g.game_id, g.season, g.gameday, json.dumps(g.to_dict())) for g in games])
            db.execute("INSERT INTO imports(attempted_at,snapshot_id) VALUES (?,?)", (now.isoformat(), snapshot_id))
        return snapshot_id

    def games(self, snapshot=None):
        snapshot = snapshot or self.latest()
        return [NFLGame(**g) for g in json.loads(snapshot["games_json"])] if snapshot else []

    def save_run(self, snapshot_id, config, result, now):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO runs(snapshot_id,kind,created_at,config,result) VALUES (?,?,?,?,?)", (snapshot_id, "historical", now.isoformat(), json.dumps(config, sort_keys=True), json.dumps(result)))

    def save_predictions(self, snapshot_id, config, predictions, now):
        with self.connection() as db:
            db.executemany("INSERT OR IGNORE INTO predictions(snapshot_id,game_id,created_at,kickoff,config,payload,model_id,artifact_id) VALUES (?,?,?,?,?,?,?,?)", [(snapshot_id, p["game_id"], now.isoformat(), p["kickoff"], json.dumps(config, sort_keys=True), json.dumps(p), config.get("model", "power-rating-v1"), config.get("artifact_id")) for p in predictions])

    def predictions(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM predictions ORDER BY created_at, id")]

    def ingest_market(self, payload: bytes, lines, source: str, now):
        digest = hashlib.sha256(payload).hexdigest(); snapshot_id = f"{source}:{digest}:{now.isoformat()}"
        raw_dir = self.root / "raw" / "odds"; raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{digest}.json"
        if not raw_path.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=raw_dir, suffix=".tmp", delete=False) as file:
                    temporary = Path(file.name); file.write(payload); file.flush(); os.fsync(file.fileno())
                temporary.replace(raw_path)
            finally:
                if temporary is not None: temporary.unlink(missing_ok=True)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO market_snapshots VALUES (?,?,?,?,?,?)", (snapshot_id, digest, source, str(raw_path), now.isoformat(), json.dumps(lines, sort_keys=True)))
            db.executemany("INSERT OR IGNORE INTO market_lines VALUES (?,?,?,?,?)", [(snapshot_id, row["game_id"], row["bookmaker"], row["home_spread"], row.get("source_updated_at")) for row in lines])
            db.execute("INSERT INTO market_imports(attempted_at,snapshot_id) VALUES (?,?)", (now.isoformat(), snapshot_id))
        return snapshot_id

    def record_market_failure(self, error, now):
        with self.connection() as db: db.execute("INSERT INTO market_imports(attempted_at,error) VALUES (?,?)", (now.isoformat(), str(error)))

    def latest_market_snapshot(self, before=None):
        with self.connection() as db:
            if before is None:
                row = db.execute("SELECT * FROM market_snapshots ORDER BY retrieved_at DESC LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT * FROM market_snapshots WHERE retrieved_at<=? ORDER BY retrieved_at DESC LIMIT 1", (before.isoformat(),)).fetchone()
        return dict(row) if row else None

    def market_lines(self, snapshot_id):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM market_lines WHERE snapshot_id=? ORDER BY game_id, bookmaker", (snapshot_id,))]

    def last_market_error(self):
        with self.connection() as db:
            row = db.execute("SELECT error FROM market_imports WHERE error IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None
