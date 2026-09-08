"""Versioned statistics, experiments and locally fitted model artifacts."""
from __future__ import annotations

import hashlib
import json
import pickle

from againstallodds.exceptions import AgainstAllOddsError


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ResearchStore:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS stat_snapshots (
                    id TEXT PRIMARY KEY, season INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, raw_path TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, version TEXT NOT NULL,
                    aggregates TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stat_imports (
                    id INTEGER PRIMARY KEY, season INTEGER NOT NULL,
                    attempted_at TEXT NOT NULL, snapshot_id TEXT REFERENCES stat_snapshots(id),
                    error TEXT);
                CREATE INDEX IF NOT EXISTS stat_import_season ON stat_imports(season,id);
                CREATE TABLE IF NOT EXISTS feature_sets (
                    id TEXT PRIMARY KEY, version TEXT NOT NULL,
                    manifest TEXT NOT NULL, created_at TEXT NOT NULL, rows_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, feature_id TEXT NOT NULL REFERENCES feature_sets(id),
                    created_at TEXT NOT NULL, specification TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_artifacts (
                    id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(id),
                    model_id TEXT NOT NULL, forecast_season INTEGER NOT NULL,
                    trained_through INTEGER NOT NULL, settings TEXT NOT NULL,
                    content_hash TEXT NOT NULL, fitted_model BLOB NOT NULL);
            """)

    def latest_stats(self):
        with self.store.connection() as db:
            rows = db.execute("""SELECT s.* FROM stat_snapshots s JOIN stat_imports i
                ON i.snapshot_id=s.id WHERE i.id IN
                (SELECT MAX(id) FROM stat_imports WHERE error IS NULL GROUP BY season)""")
            return {r["season"]: dict(r) for r in rows}

    def stat_error(self, season, error, now):
        with self.store.connection() as db:
            db.execute("INSERT INTO stat_imports(season,attempted_at,error) VALUES (?,?,?)", (season, now.isoformat(), str(error)))

    def save_stats(self, season, digest, path, aggregates, version, now):
        sid = identity({"season": season, "hash": digest, "version": version})
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO stat_snapshots VALUES (?,?,?,?,?,?,?)", (sid, season, digest, str(path), now.isoformat(), version, canonical(aggregates)))
            db.execute("INSERT INTO stat_imports(season,attempted_at,snapshot_id) VALUES (?,?,?)", (season, now.isoformat(), sid))
        return sid

    def save_features(self, manifest, version, rows, now):
        fid = identity({"manifest": manifest, "version": version})
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO feature_sets VALUES (?,?,?,?,?)", (fid, version, canonical(manifest), now.isoformat(), canonical(rows)))
        return fid

    def latest_experiment(self):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM experiments ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def experiment_for_family(self, family):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM experiments WHERE specification LIKE ? ORDER BY created_at DESC LIMIT 1", (f'%"feature_family":"{family}"%',)).fetchone()
            if row is None and family == "raw":
                row = db.execute("SELECT * FROM experiments WHERE specification NOT LIKE '%feature_family%' ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def experiment(self, eid):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()
            return dict(row) if row else None

    def publish(self, eid, fid, specification, result, artifacts, now):
        # Nothing replaces the last usable experiment until every model succeeded.
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO experiments VALUES (?,?,?,?,?)", (eid, fid, now.isoformat(), canonical(specification), canonical(result)))
            for artifact in artifacts:
                blob = pickle.dumps(artifact["pipeline"], protocol=5)
                digest = hashlib.sha256(blob).hexdigest()
                aid = identity({"experiment": eid, "model": artifact["model_id"], "season": artifact["season"]})
                db.execute("INSERT OR IGNORE INTO model_artifacts VALUES (?,?,?,?,?,?,?,?)", (aid, eid, artifact["model_id"], artifact["season"], artifact["season"] - 1, canonical(artifact["settings"]), digest, blob))

    def artifacts(self, eid, season):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM model_artifacts WHERE experiment_id=? AND forecast_season=?", (eid, season))]

    @staticmethod
    def load_model(artifact):
        blob = artifact["fitted_model"]
        if hashlib.sha256(blob).hexdigest() != artifact["content_hash"]:
            raise AgainstAllOddsError("Saved model failed its integrity check. Rerun the comparison.")
        # These are only locally trained artifacts, never downloads or uploads.
        return pickle.loads(blob)
