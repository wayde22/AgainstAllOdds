"""Official NFL availability snapshots and quarterback availability adjustments."""
from __future__ import annotations

import hashlib
import html.parser
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape as xml_escape
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.nfl_data import ALIASES, TEAMS, utcnow
from againstallodds.research_store import canonical, identity
from againstallodds.research_store import ResearchStore

INJURY_URL = "https://www.nfl.com/injuries/"
INACTIVE_URL = "https://www.nfl.com/inactives/"
PARSER_VERSION = "official-nfl-table-v2"
STATUS_WEIGHT = {"out": 1.0, "inactive": 1.0, "doubtful": .8, "questionable": .5,
                 "did not participate": .5, "limited": .25, "full participation": 0.0}


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower().replace("jr", "").replace("sr", ""))


class Tables(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.current, self.row, self.cell = [], None, None, None
        self.headings, self.last_heading = [], ""
        self.subtitle = None
    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "h3", "h4"}: self.headings.append([])
        if tag == "div" and "d3-o-section-sub-title" in dict(attrs).get("class", ""):
            self.subtitle = []
        if tag == "table": self.current = {"heading": self.last_heading, "rows": []}
        if self.current and tag == "tr": self.row = []
        if self.current and tag in {"td", "th"}: self.cell = []
    def handle_data(self, data):
        if self.headings: self.headings[-1].append(data)
        if self.subtitle is not None: self.subtitle.append(data)
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4"} and self.headings:
            self.last_heading = " ".join(self.headings.pop()).strip() or self.last_heading
        if tag == "div" and self.subtitle is not None:
            self.last_heading = " ".join(self.subtitle).strip() or self.last_heading
            self.subtitle = None
        if self.current and tag in {"td", "th"}:
            self.row.append(" ".join(self.cell).strip()); self.cell = None
        if self.current and tag == "tr" and self.row:
            self.current["rows"].append(self.row); self.row = None
        if tag == "table" and self.current:
            self.tables.append(self.current); self.current = None


def parse_official(payload: bytes, source: str) -> list[dict]:
    try:
        parser = Tables(); parser.feed(payload.decode("utf-8"))
    except UnicodeError as error:
        raise AgainstAllOddsError(f"Official report is not UTF-8: {error}") from error
    records = []
    for table in parser.tables:
        rows = table["rows"]
        if not rows or [x.lower() for x in rows[0][:2]] != ["player", "position"]:
            continue
        for row in rows[1:]:
            if len(row) < 2 or not row[0].strip(): continue
            records.append({"player_name": row[0], "position": row[1], "injury": row[2] if len(row) > 2 else "",
                            "practice_status": row[3] if len(row) > 3 else "", "game_status": row[4] if len(row) > 4 else "",
                            "team_hint": table["heading"], "source": source})
    # A healthy report intentionally has no records. A non-report must not silently pass.
    text = payload.decode("utf-8", errors="replace").lower()
    if not records and "no injuries reported" not in text and "injuries" not in text:
        raise AgainstAllOddsError("Official page no longer contains recognizable injury-report content.")
    return records


def team_from_hint(hint: str, games) -> str | None:
    candidates = [team for team in TEAMS.values() if team.lower() in hint.lower()]
    if len(candidates) == 1: return candidates[0]
    nickname = normalize_name(hint)
    candidates = [team for team in TEAMS.values() if normalize_name(team).endswith(nickname)]
    if len(candidates) == 1: return candidates[0]
    return None


def weight(record):
    text = f"{record.get('game_status','')} {record.get('practice_status','')}".lower()
    for status, amount in STATUS_WEIGHT.items():
        if status in text: return amount
    return 0.0


def download(url):
    try:
        with urlopen(Request(url, headers={"User-Agent": "AgainstAllOdds/0.4"}), timeout=30) as response:
            return response.read(5_000_001)
    except OSError as error:
        raise AgainstAllOddsError(f"Official availability download failed: {error}") from error


class AvailabilityStore:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS injury_snapshots (id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL, content_hash TEXT NOT NULL, raw_path TEXT NOT NULL, retrieved_at TEXT NOT NULL, parser_version TEXT NOT NULL, records TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS injury_imports (id INTEGER PRIMARY KEY, attempted_at TEXT NOT NULL, source TEXT NOT NULL, snapshot_id TEXT REFERENCES injury_snapshots(id), error TEXT);
            CREATE TABLE IF NOT EXISTS qb_profiles (id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, source_manifest TEXT NOT NULL, profiles TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wr_profiles (id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, source_manifest TEXT NOT NULL, profiles TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rb_te_profiles (id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, source_manifest TEXT NOT NULL, profiles TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS edge_profiles (id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, source_manifest TEXT NOT NULL, profiles TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS qb_overrides (id INTEGER PRIMARY KEY, game_id TEXT NOT NULL, team TEXT NOT NULL, player_id TEXT, player_name TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(game_id,team));
            CREATE TABLE IF NOT EXISTS availability_assessments (id TEXT PRIMARY KEY, game_id TEXT NOT NULL, team TEXT NOT NULL, snapshot_id TEXT REFERENCES injury_snapshots(id), base_qb TEXT, expected_qb TEXT, replacement_qb TEXT, absence_weight REAL NOT NULL, adjustment REAL NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS injury_predictions (id TEXT PRIMARY KEY, game_id TEXT NOT NULL, model_id TEXT NOT NULL, created_at TEXT NOT NULL, kickoff TEXT NOT NULL, base_margin REAL NOT NULL, adjusted_margin REAL NOT NULL, assessments TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS availability_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS injury_checks (id INTEGER PRIMARY KEY, game_id TEXT NOT NULL, due_at TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned', task_name TEXT, UNIQUE(game_id,due_at));
            CREATE TABLE IF NOT EXISTS availability_changes (id TEXT PRIMARY KEY, game_id TEXT NOT NULL, team TEXT NOT NULL, detected_at TEXT NOT NULL, before_detail TEXT, after_detail TEXT NOT NULL, material INTEGER NOT NULL, notified_at TEXT);
            CREATE TABLE IF NOT EXISTS wr_assessments (id TEXT PRIMARY KEY, game_id TEXT NOT NULL, team TEXT NOT NULL, snapshot_id TEXT, adjustment REAL NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skill_assessments (id TEXT PRIMARY KEY, game_id TEXT NOT NULL, team TEXT NOT NULL, position TEXT NOT NULL, snapshot_id TEXT, adjustment REAL NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS edge_assessments (id TEXT PRIMARY KEY, game_id TEXT NOT NULL, team TEXT NOT NULL, snapshot_id TEXT, adjustment REAL NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(injury_checks)")}
            if "completed_at" not in columns:
                db.execute("ALTER TABLE injury_checks ADD COLUMN completed_at TEXT")

    def setting(self, key, default):
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM availability_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_setting(self, key, value):
        with self.store.connection() as db: db.execute("INSERT OR REPLACE INTO availability_settings VALUES (?,?)", (key, canonical(value)))
    def snapshot(self, source, payload, records, now):
        digest = hashlib.sha256(payload).hexdigest(); sid = identity({"source":source,"hash":digest,"parser":PARSER_VERSION})
        directory = self.store.root / "raw" / "official_nfl"; directory.mkdir(parents=True, exist_ok=True); path = directory / f"{source}-{digest}.html"
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as file:
                temp=Path(file.name); file.write(payload); file.flush(); os.fsync(file.fileno())
            temp.replace(path)
        url = INJURY_URL if source == "injuries" else INACTIVE_URL
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO injury_snapshots VALUES (?,?,?,?,?,?,?,?)", (sid,source,url,digest,str(path),now.isoformat(),PARSER_VERSION,canonical(records)))
            db.execute("INSERT INTO injury_imports(attempted_at,source,snapshot_id) VALUES (?,?,?)", (now.isoformat(),source,sid))
        return sid
    def latest_snapshot(self, source="injuries"):
        with self.store.connection() as db:
            row=db.execute("SELECT s.* FROM injury_snapshots s JOIN injury_imports i ON i.snapshot_id=s.id WHERE s.source=? AND i.error IS NULL ORDER BY i.id DESC LIMIT 1",(source,)).fetchone()
        return dict(row) if row else None
    def failure(self, source,error,now):
        with self.store.connection() as db: db.execute("INSERT INTO injury_imports(attempted_at,source,error) VALUES (?,?,?)",(now.isoformat(),source,str(error)))
    def override(self, game_id,team,player_id,player_name,reason,now):
        with self.store.connection() as db: db.execute("INSERT OR REPLACE INTO qb_overrides(game_id,team,player_id,player_name,reason,created_at) VALUES (?,?,?,?,?,?)",(game_id,team,player_id,player_name,reason,now.isoformat()))
    def overrides(self, game_id):
        with self.store.connection() as db: return {r["team"]:dict(r) for r in db.execute("SELECT * FROM qb_overrides WHERE game_id=?",(game_id,))}
    def clear_override(self, game_id, team):
        with self.store.connection() as db: db.execute("DELETE FROM qb_overrides WHERE game_id=? AND team=?", (game_id, team))
    def save_profile(self, manifest, profiles, now):
        pid=identity({"manifest":manifest,"version":"qb-performance-v2"})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO qb_profiles VALUES (?,?,?,?)",(pid,now.isoformat(),canonical(manifest),canonical(profiles)))
        return pid
    def latest_profile(self):
        with self.store.connection() as db: row=db.execute("SELECT * FROM qb_profiles ORDER BY generated_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    def save_wr_profile(self, manifest, profiles, now):
        pid = identity({"manifest": manifest, "version": "wr-performance-v1"})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO wr_profiles VALUES (?,?,?,?)", (pid, now.isoformat(), canonical(manifest), canonical(profiles)))
        return pid
    def latest_wr_profile(self):
        with self.store.connection() as db: row = db.execute("SELECT * FROM wr_profiles ORDER BY generated_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    def save_rb_te_profile(self, manifest, profiles, now):
        pid = identity({"manifest": manifest, "version": "rb-te-performance-v1"})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO rb_te_profiles VALUES (?,?,?,?)", (pid, now.isoformat(), canonical(manifest), canonical(profiles)))
        return pid
    def latest_rb_te_profile(self):
        with self.store.connection() as db: row = db.execute("SELECT * FROM rb_te_profiles ORDER BY generated_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    def save_edge_profile(self, manifest, profiles, now):
        pid = identity({"manifest": manifest, "version": "edge-disruption-v1"})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO edge_profiles VALUES (?,?,?,?)", (pid, now.isoformat(), canonical(manifest), canonical(profiles)))
        return pid
    def latest_edge_profile(self):
        with self.store.connection() as db: row = db.execute("SELECT * FROM edge_profiles ORDER BY generated_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    def save_wr_assessment(self, detail, now):
        aid = identity({"game": detail["game_id"], "team": detail["team"], "snapshot": detail.get("snapshot_id"), "players": detail["players"], "adjustment": detail["adjustment"]})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO wr_assessments VALUES (?,?,?,?,?,?,?)", (aid, detail["game_id"], detail["team"], detail.get("snapshot_id"), detail["adjustment"], canonical(detail), now.isoformat()))
        return aid
    def save_skill_assessment(self, detail, now):
        aid = identity({"game": detail["game_id"], "team": detail["team"], "position": detail["position"], "snapshot": detail.get("snapshot_id"), "players": detail["players"], "adjustment": detail["adjustment"]})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO skill_assessments VALUES (?,?,?,?,?,?,?,?)", (aid, detail["game_id"], detail["team"], detail["position"], detail.get("snapshot_id"), detail["adjustment"], canonical(detail), now.isoformat()))
        return aid
    def save_edge_assessment(self, detail, now):
        aid = identity({"game": detail["game_id"], "team": detail["team"], "snapshot": detail.get("snapshot_id"), "players": detail["players"], "adjustment": detail["adjustment"]})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO edge_assessments VALUES (?,?,?,?,?,?,?)", (aid, detail["game_id"], detail["team"], detail.get("snapshot_id"), detail["adjustment"], canonical(detail), now.isoformat()))
        return aid
    def save_assessment(self, assessment, now):
        aid=identity({k:assessment[k] for k in ("game_id","team","snapshot_id","base_qb","expected_qb","replacement_qb","absence_weight","adjustment")})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO availability_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?)",(aid,assessment["game_id"],assessment["team"],assessment.get("snapshot_id"),assessment.get("base_qb"),assessment.get("expected_qb"),assessment.get("replacement_qb"),assessment["absence_weight"],assessment["adjustment"],canonical(assessment),now.isoformat()))
        return aid
    def save_prediction(self,row):
        pid=identity({"game":row["game_id"],"model":row["model_id"],"assessments":row["assessments"],"base":row["base_margin"]})
        with self.store.connection() as db: db.execute("INSERT OR IGNORE INTO injury_predictions VALUES (?,?,?,?,?,?,?,?,?)",(pid,row["game_id"],row["model_id"],row["created_at"],row["kickoff"],row["base_margin"],row["adjusted_margin"],canonical(row["assessments"]),canonical(row)))
        return pid
    def latest_assessment(self, game_id, team):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM availability_assessments WHERE game_id=? AND team=? ORDER BY created_at DESC LIMIT 1", (game_id, team)).fetchone()
        return json.loads(row["detail"]) if row else None
    def save_change(self, before, after, now):
        if not before:
            return None
        changed = any(before.get(key) != after.get(key) for key in ("expected_qb", "absence_weight", "adjustment"))
        material = changed and (before.get("expected_qb") != after.get("expected_qb") or abs(before.get("adjustment", 0) - after.get("adjustment", 0)) >= .5 or before.get("absence_weight") != after.get("absence_weight"))
        if not material:
            return None
        cid = identity({"game": after["game_id"], "team": after["team"], "before": before, "after": after})
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO availability_changes(id,game_id,team,detected_at,before_detail,after_detail,material) VALUES (?,?,?,?,?,?,1)", (cid, after["game_id"], after["team"], now.isoformat(), canonical(before) if before else None, canonical(after)))
        return {"id": cid, "game_id": after["game_id"], "team": after["team"], "before": before, "after": after}
    def changes(self, limit=20):
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM availability_changes ORDER BY detected_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]
    def mark_notified(self, change_id, now):
        with self.store.connection() as db:
            db.execute("UPDATE availability_changes SET notified_at=? WHERE id=?", (now.isoformat(), change_id))


def sync_injuries(store, *, source="injuries", fetch=None, now=None):
    now=now or utcnow(); db=AvailabilityStore(store)
    try:
        payload=(fetch or download)(INJURY_URL if source=="injuries" else INACTIVE_URL); records=parse_official(payload,source)
        games=store.games();
        for record in records: record["team"] = team_from_hint(record["team_hint"],games); record["absence_weight"] = weight(record)
        return {"snapshot_id":db.snapshot(source,payload,records,now),"records":len(records)}
    except (OSError,AgainstAllOddsError) as error:
        db.failure(source,error,now); raise


def current_report(store):
    """Reconcile the latest official report and inactive list by player/team."""
    db = AvailabilityStore(store)
    snapshots = [snapshot for source in ("injuries", "inactives") if (snapshot := db.latest_snapshot(source))]
    records = {}
    # Inactive status is definitive and therefore replaces an earlier practice report.
    for snapshot in snapshots:
        for record in json.loads(snapshot["records"]):
            key = (record.get("team"), normalize_name(record.get("player_name", "")))
            prior = records.get(key)
            if not prior or snapshot["source"] == "inactives" or weight(record) >= weight(prior):
                records[key] = {**record, "snapshot_id": snapshot["id"], "retrieved_at": snapshot["retrieved_at"]}
    freshest = max((snapshot["retrieved_at"] for snapshot in snapshots), default=None)
    return {"id": snapshots[0]["id"] if snapshots else None, "records": list(records.values()), "snapshots": snapshots, "retrieved_at": freshest}


def report_freshness(report, *, now=None, hours=8):
    if not report or not report.get("retrieved_at"):
        return {"fresh": False, "age_hours": None}
    now = now or utcnow()
    age = max(0.0, (now - datetime.fromisoformat(report["retrieved_at"])).total_seconds() / 3600)
    return {"fresh": age <= hours, "age_hours": round(age, 1)}


def _profile_players(profile_row):
    """Return the player profiles retained in a profile snapshot."""
    return json.loads(profile_row["profiles"]) if profile_row else []


def build_qb_profiles(store, *, now=None):
    """Build transparent QB profiles from the locally retained nflverse play data.

    These are descriptive, rolling prior-game profiles.  They intentionally do
    not rewrite completed-game predictions or claim a historical injury result.
    """
    now = now or utcnow()
    cutoff = now.date().isoformat()
    snapshots = list(ResearchStore(store).latest_stats().values())
    game_dates = {g.game_id: g.gameday for g in store.games()}
    by_player, all_epa = defaultdict(list), []
    columns = ["game_id", "posteam", "passer_player_id", "passer_player_name", "qb_dropback", "epa", "interception", "sack"]
    manifest = []
    for snapshot in snapshots:
        raw = Path(snapshot["raw_path"])
        if not raw.exists():
            continue
        try:
            frame = pq.read_table(raw, columns=columns).to_pandas()
        except (OSError, ValueError, KeyError):
            continue
        manifest.append(snapshot["id"])
        frame = frame[(frame["qb_dropback"] == 1) & frame["passer_player_id"].notna() & frame["epa"].notna()]
        for (game_id, team, player_id, name), group in frame.groupby(["game_id", "posteam", "passer_player_id", "passer_player_name"], dropna=True):
            gameday = game_dates.get(game_id, "")
            if gameday and gameday >= cutoff:
                continue
            team = ALIASES.get(str(team), str(team))
            if team not in TEAMS:
                continue
            team = TEAMS[team]
            drops = int(len(group)); epa = float(group["epa"].sum())
            item = {"game_id": str(game_id), "date": gameday, "team": team, "player_id": str(player_id),
                    "name": str(name), "dropbacks": drops, "epa": epa,
                    "success": int((group["epa"] > 0).sum()),
                    "sacks": int(group["sack"].fillna(0).sum()),
                    "turnovers": int(group["interception"].fillna(0).sum())}
            by_player[str(player_id)].append(item)
            all_epa.extend(group["epa"].astype(float).tolist())
    if not by_player:
        raise AgainstAllOddsError("No quarterback play-by-play data is available. Run sync-stats first.")
    league_epa = float(np.mean(all_epa)) if all_epa else 0.0
    profiles = []
    for player_id, games in by_player.items():
        games.sort(key=lambda row: (row["date"], row["game_id"]))
        recent = games[-16:]
        drops = sum(row["dropbacks"] for row in recent)
        if drops < 20:
            continue
        epa = sum(row["epa"] for row in recent)
        last = recent[-1]
        profiles.append({"player_id": player_id, "name": last["name"], "team": last["team"],
            "prior_games": len(recent), "dropbacks": drops, "expected_dropbacks": round(drops / len(recent), 1),
            "epa_per_dropback": round(epa / drops, 4),
            "shrunk_epa_per_dropback": round((epa + 100 * league_epa) / (drops + 100), 4),
            "success_rate": round(sum(row["success"] for row in recent) / drops, 4),
            "sack_rate": round(sum(row["sacks"] for row in recent) / drops, 4),
            "turnover_rate": round(sum(row["turnovers"] for row in recent) / drops, 4),
            "last_game": last["game_id"], "last_game_date": last["date"], "last_game_dropbacks": last["dropbacks"]})
    profiles.sort(key=lambda row: (row["team"], row["name"]))
    db = AvailabilityStore(store)
    profile_id = db.save_profile({"stat_snapshots": manifest, "cutoff": cutoff, "league_epa_per_dropback": round(league_epa, 5)}, profiles, now)
    return {"profile_id": profile_id, "players": len(profiles), "league_epa_per_dropback": league_epa}


def build_wr_profiles(store, *, now=None):
    """Build rolling receiver profiles from completed receiving production."""
    now = now or utcnow(); cutoff = now.date().isoformat()
    snapshots = list(ResearchStore(store).latest_stats().values())
    game_dates = {g.game_id: g.gameday for g in store.games()}
    by_player, manifest = defaultdict(list), []
    columns = ["game_id", "posteam", "receiver_player_id", "receiver_player_name", "receiving_yards", "epa"]
    for snapshot in snapshots:
        raw = Path(snapshot["raw_path"])
        if not raw.exists(): continue
        try:
            frame = pq.read_table(raw, columns=columns).to_pandas()
        except (OSError, ValueError, KeyError):
            continue
        manifest.append(snapshot["id"])
        frame = frame[frame["receiver_player_id"].notna() & frame["epa"].notna()]
        for (game_id, team, player_id, name), group in frame.groupby(["game_id", "posteam", "receiver_player_id", "receiver_player_name"], dropna=True):
            date = game_dates.get(game_id, "")
            if date and date >= cutoff: continue
            code = ALIASES.get(str(team), str(team))
            if code not in TEAMS: continue
            by_player[str(player_id)].append({"game_id": str(game_id), "date": date, "team": TEAMS[code], "player_id": str(player_id), "name": str(name), "receptions": int(len(group)), "yards": float(group["receiving_yards"].fillna(0).sum()), "epa": float(group["epa"].sum())})
    profiles = []
    for player_id, games in by_player.items():
        games.sort(key=lambda row: (row["date"], row["game_id"])); recent = games[-16:]
        receptions = sum(row["receptions"] for row in recent)
        if receptions < 12: continue
        last = recent[-1]; epa = sum(row["epa"] for row in recent)
        profiles.append({"player_id": player_id, "name": last["name"], "team": last["team"], "prior_games": len(recent), "receptions": receptions, "yards": round(sum(row["yards"] for row in recent), 1), "epa_per_reception": round(epa / receptions, 4), "epa_per_game": round(epa / len(recent), 3), "last_game_date": last["date"], "last_game_receptions": last["receptions"]})
    profiles.sort(key=lambda row: (row["team"], -row["epa_per_game"], row["name"]))
    db = AvailabilityStore(store); profile_id = db.save_wr_profile({"stat_snapshots": manifest, "cutoff": cutoff}, profiles, now)
    return {"profile_id": profile_id, "players": len(profiles)}


def assess_wide_receivers(store, game, *, profile_row=None, snapshot=None, now=None):
    """Assess reported wide receiver absences; this is distinct from the QB layer."""
    now = now or utcnow(); db = AvailabilityStore(store)
    profile_row = profile_row or db.latest_wr_profile()
    profiles = json.loads(profile_row["profiles"]) if profile_row else []
    raw = snapshot["records"] if snapshot else []
    records = json.loads(raw) if isinstance(raw, str) else raw
    team = game["team"]; available = [p for p in profiles if p["team"] == team]
    details, total = [], 0.0
    for record in records:
        if record.get("team") != team or str(record.get("position", "")).upper() != "WR": continue
        player, confidence = _resolve_player(record.get("player_name"), team, available)
        if not player or not weight(record): continue
        replacement = max((p for p in available if p["player_id"] != player["player_id"]), key=lambda p: p["epa_per_game"], default=None)
        gap = max(0.0, player["epa_per_game"] - (replacement["epa_per_game"] if replacement else 0.0))
        impact = -min(2.0, weight(record) * gap * .22)
        total += impact
        details.append({"player_id": player["player_id"], "player_name": player["name"], "absence_weight": weight(record), "replacement": replacement and replacement["name"], "adjustment": round(impact, 2), "match_confidence": confidence, "status": record.get("game_status") or record.get("practice_status")})
    detail = {"game_id": game["game_id"], "team": team, "snapshot_id": snapshot and snapshot.get("id"), "players": details, "adjustment": round(max(-3.0, total), 2), "reason": "Official WR availability adjustment" if details else "No reported matched WR availability issue"}
    db.save_wr_assessment(detail, now)
    return detail


def build_rb_te_profiles(store, *, now=None):
    """Build role-based RB and TE profiles from completed rushing/receiving plays."""
    now = now or utcnow(); cutoff = now.date().isoformat()
    snapshots = list(ResearchStore(store).latest_stats().values())
    game_dates = {g.game_id: g.gameday for g in store.games()}; by_player, manifest = defaultdict(list), []
    columns = ["game_id", "posteam", "rusher_player_id", "rusher_player_name", "receiver_player_id", "receiver_player_name", "rush_attempt", "rushing_yards", "receiving_yards", "epa"]
    for snapshot in snapshots:
        raw = Path(snapshot["raw_path"])
        if not raw.exists(): continue
        try: frame = pq.read_table(raw, columns=columns).to_pandas()
        except (OSError, ValueError, KeyError): continue
        manifest.append(snapshot["id"])
        frame = frame[frame["epa"].notna()].copy()
        frame["date"] = frame["game_id"].map(game_dates).fillna("")
        frame = frame[(frame["date"] == "") | (frame["date"] < cutoff)]
        frame["code"] = frame["posteam"].astype(str).replace(ALIASES)
        frame["team"] = frame["code"].map(TEAMS)
        frame = frame[frame["team"].notna()]
        keys = ["player_id", "name", "game_id", "date", "team"]
        rush = frame[(frame["rush_attempt"] == 1) & frame["rusher_player_id"].notna()][["rusher_player_id", "rusher_player_name", "game_id", "date", "team", "rushing_yards", "epa"]].rename(columns={"rusher_player_id": "player_id", "rusher_player_name": "name", "rushing_yards": "yards"})
        rush["rushes"], rush["receptions"] = 1, 0
        receive = frame[frame["receiver_player_id"].notna()][["receiver_player_id", "receiver_player_name", "game_id", "date", "team", "receiving_yards", "epa"]].rename(columns={"receiver_player_id": "player_id", "receiver_player_name": "name", "receiving_yards": "yards"})
        receive["rushes"], receive["receptions"] = 0, 1
        combined = pd.concat([rush, receive], ignore_index=True)
        for item in combined.groupby(keys, dropna=True, as_index=False).agg(rushes=("rushes", "sum"), receptions=("receptions", "sum"), yards=("yards", "sum"), epa=("epa", "sum")).to_dict("records"):
            item["player_id"], item["game_id"] = str(item["player_id"]), str(item["game_id"])
            by_player[item["player_id"]].append(item)
    rb, te = [], []
    for player_id, player_games in by_player.items():
        player_games.sort(key=lambda row: (row["date"], row["game_id"])); recent = player_games[-16:]
        last = recent[-1]; rushes = sum(row["rushes"] for row in recent); receptions = sum(row["receptions"] for row in recent); epa = sum(row["epa"] for row in recent)
        shared = {"player_id": player_id, "name": last["name"], "team": last["team"], "prior_games": len(recent), "rushes": rushes, "receptions": receptions, "yards": round(sum(row["yards"] for row in recent), 1), "epa_per_game": round(epa / len(recent), 3), "last_game_date": last["date"]}
        if rushes >= 15: rb.append({**shared, "role": "RB"})
        if receptions >= 8: te.append({**shared, "role": "TE"})
    rb.sort(key=lambda row: (row["team"], -row["epa_per_game"], row["name"])); te.sort(key=lambda row: (row["team"], -row["epa_per_game"], row["name"]))
    db = AvailabilityStore(store); profile_id = db.save_rb_te_profile({"stat_snapshots": manifest, "cutoff": cutoff}, {"RB": rb, "TE": te}, now)
    return {"profile_id": profile_id, "running_backs": len(rb), "tight_ends": len(te)}


def assess_skill_position(store, game, position, *, profile_row=None, snapshot=None, now=None):
    """Assess role-based RB or TE availability from the official position label."""
    now = now or utcnow(); db = AvailabilityStore(store)
    profile_row = profile_row or db.latest_rb_te_profile()
    grouped = json.loads(profile_row["profiles"]) if profile_row else {}
    profiles = grouped.get(position, []); team = game["team"]
    raw = snapshot["records"] if snapshot else []; records = json.loads(raw) if isinstance(raw, str) else raw
    unavailable = {p["player_id"] for record in records if record.get("team") == team and weight(record) > 0 for p, _ in [_resolve_player(record.get("player_name"), team, profiles)] if p}
    details, total = [], 0.0
    for record in records:
        if record.get("team") != team or str(record.get("position", "")).upper() != position: continue
        player, confidence = _resolve_player(record.get("player_name"), team, profiles)
        if not player or not weight(record): continue
        replacement = max((p for p in profiles if p["team"] == team and p["player_id"] not in unavailable and p["player_id"] != player["player_id"]), key=lambda p: p["epa_per_game"], default=None)
        gap = max(0.0, player["epa_per_game"] - (replacement["epa_per_game"] if replacement else 0.0))
        impact = -min(1.5, weight(record) * gap * (.20 if position == "RB" else .18)); total += impact
        details.append({"player_id": player["player_id"], "player_name": player["name"], "absence_weight": weight(record), "replacement": replacement and replacement["name"], "adjustment": round(impact, 2), "match_confidence": confidence, "status": record.get("game_status") or record.get("practice_status")})
    detail = {"game_id": game["game_id"], "team": team, "position": position, "snapshot_id": snapshot and snapshot.get("id"), "players": details, "adjustment": round(total, 2), "reason": f"Official {position} role-based availability adjustment" if details else f"No reported matched {position} availability issue"}
    db.save_skill_assessment(detail, now); return detail


EDGE_POSITIONS = {"DE", "OLB", "EDGE"}


def build_edge_profiles(store, *, now=None):
    """Build rolling pass-rush disruption profiles from sacks and QB hits."""
    now = now or utcnow(); cutoff = now.date().isoformat()
    snapshots = list(ResearchStore(store).latest_stats().values())
    game_dates = {g.game_id: g.gameday for g in store.games()}; by_player, manifest = defaultdict(list), []
    columns = ["game_id", "defteam", "sack_player_id", "sack_player_name", "qb_hit_1_player_id", "qb_hit_1_player_name", "qb_hit_2_player_id", "qb_hit_2_player_name"]
    for snapshot in snapshots:
        raw = Path(snapshot["raw_path"])
        if not raw.exists(): continue
        try: frame = pq.read_table(raw, columns=columns).to_pandas()
        except (OSError, ValueError, KeyError): continue
        manifest.append(snapshot["id"])
        frame["date"] = frame["game_id"].map(game_dates).fillna("")
        frame = frame[(frame["date"] == "") | (frame["date"] < cutoff)]
        frame["code"] = frame["defteam"].astype(str).replace(ALIASES); frame["team"] = frame["code"].map(TEAMS)
        frame = frame[frame["team"].notna()]
        events = []
        for identifier, name, kind in (("sack_player_id", "sack_player_name", "sack"), ("qb_hit_1_player_id", "qb_hit_1_player_name", "hit"), ("qb_hit_2_player_id", "qb_hit_2_player_name", "hit")):
            event = frame[frame[identifier].notna()][[identifier, name, "game_id", "date", "team"]].rename(columns={identifier: "player_id", name: "name"})
            event["sacks"], event["hits"] = (1, 0) if kind == "sack" else (0, 1)
            events.append(event)
        combined = pd.concat(events, ignore_index=True)
        for item in combined.groupby(["player_id", "name", "game_id", "date", "team"], dropna=True, as_index=False).agg(sacks=("sacks", "sum"), hits=("hits", "sum")).to_dict("records"):
            item["player_id"], item["game_id"] = str(item["player_id"]), str(item["game_id"]); by_player[item["player_id"]].append(item)
    profiles = []
    for player_id, games in by_player.items():
        games.sort(key=lambda row: (row["date"], row["game_id"])); recent = games[-16:]
        sacks, hits = sum(row["sacks"] for row in recent), sum(row["hits"] for row in recent)
        if sacks + hits < 3: continue
        last = recent[-1]
        profiles.append({"player_id": player_id, "name": last["name"], "team": last["team"], "prior_games": len(recent), "sacks": sacks, "qb_hits": hits, "disruption_per_game": round((sacks + .25 * hits) / len(recent), 3), "last_game_date": last["date"]})
    profiles.sort(key=lambda row: (row["team"], -row["disruption_per_game"], row["name"]))
    db = AvailabilityStore(store); profile_id = db.save_edge_profile({"stat_snapshots": manifest, "cutoff": cutoff}, profiles, now)
    return {"profile_id": profile_id, "players": len(profiles)}


def refresh_all_availability_data(
    store,
    *,
    sync_report=sync_injuries,
    build_qb=build_qb_profiles,
    build_wr=build_wr_profiles,
    build_rb_te=build_rb_te_profiles,
    build_edge=build_edge_profiles,
):
    """Refresh official reports and all local player profiles without stopping on one failure."""
    steps = (
        ("Official injury report", lambda: sync_report(store), lambda result: f"{result['records']} report rows"),
        ("Official inactives", lambda: sync_report(store, source="inactives"), lambda result: f"{result['records']} inactive rows"),
        ("QB profiles", lambda: build_qb(store), lambda result: f"{result['players']} players"),
        ("WR profiles", lambda: build_wr(store), lambda result: f"{result['players']} players"),
        ("RB/TE profiles", lambda: build_rb_te(store), lambda result: f"{result['running_backs']} RB and {result['tight_ends']} TE players"),
        ("EDGE profiles", lambda: build_edge(store), lambda result: f"{result['players']} players"),
    )
    outcomes = []
    for label, action, describe in steps:
        try:
            outcomes.append({"step": label, "success": True, "detail": describe(action())})
        except (AgainstAllOddsError, OSError, sqlite3.Error) as error:
            outcomes.append({"step": label, "success": False, "detail": str(error)})
    return outcomes


def assess_edge_rushers(store, game, *, profile_row=None, snapshot=None, now=None):
    """Assess reported edge-rusher absences using role-based disruption profiles."""
    now = now or utcnow(); db = AvailabilityStore(store)
    profile_row = profile_row or db.latest_edge_profile(); profiles = json.loads(profile_row["profiles"]) if profile_row else []
    raw = snapshot["records"] if snapshot else []; records = json.loads(raw) if isinstance(raw, str) else raw
    team = game["team"]
    unavailable = {p["player_id"] for record in records if record.get("team") == team and weight(record) > 0 for p, _ in [_resolve_player(record.get("player_name"), team, profiles)] if p}
    details, total = [], 0.0
    for record in records:
        if record.get("team") != team or str(record.get("position", "")).upper() not in EDGE_POSITIONS: continue
        player, confidence = _resolve_player(record.get("player_name"), team, profiles)
        if not player or not weight(record): continue
        replacement = max((p for p in profiles if p["team"] == team and p["player_id"] not in unavailable and p["player_id"] != player["player_id"]), key=lambda p: p["disruption_per_game"], default=None)
        gap = max(0.0, player["disruption_per_game"] - (replacement["disruption_per_game"] if replacement else 0.0))
        impact = -min(1.5, weight(record) * gap * .55); total += impact
        details.append({"player_id": player["player_id"], "player_name": player["name"], "absence_weight": weight(record), "replacement": replacement and replacement["name"], "adjustment": round(impact, 2), "match_confidence": confidence, "status": record.get("game_status") or record.get("practice_status")})
    detail = {"game_id": game["game_id"], "team": team, "snapshot_id": snapshot and snapshot.get("id"), "players": details, "adjustment": round(max(-2.5, total), 2), "reason": "Official EDGE availability adjustment" if details else "No reported matched EDGE availability issue"}
    db.save_edge_assessment(detail, now); return detail


def _resolve_player(name, team, profiles):
    candidates = [p for p in profiles if p["team"] == team]
    target = normalize_name(name or "")
    exact = [p for p in candidates if normalize_name(p["name"]) == target]
    if len(exact) == 1:
        return exact[0], "exact"
    last = target[-8:]
    matches = [p for p in candidates if normalize_name(p["name"]).endswith(last)] if last else []
    return (matches[0], "team-last-name") if len(matches) == 1 else (None, "unmatched")


def _latest_qb(team, profiles):
    candidates = [p for p in profiles if p["team"] == team]
    return max(candidates, key=lambda p: (p["last_game_date"], p["last_game_dropbacks"], p["dropbacks"]), default=None)


def assess_availability(store, game, *, profile_row=None, snapshot=None, now=None):
    """Assess one team's QB availability for an upcoming game."""
    now = now or utcnow(); db = AvailabilityStore(store)
    profile_row = profile_row or db.latest_profile()
    profiles = _profile_players(profile_row)
    team = game["team"]
    base = _latest_qb(team, profiles)
    detail = {"game_id": game["game_id"], "team": team, "snapshot_id": snapshot and snapshot["id"],
              "base_qb": base and base["player_id"], "expected_qb": base and base["player_id"],
              "replacement_qb": None, "absence_weight": 0.0, "adjustment": 0.0, "reason": "No QB profile available",
              "base_qb_name": base and base["name"], "expected_qb_name": base and base["name"], "match_confidence": None, "report": None}
    if not base:
        db.save_assessment(detail, now); return detail
    detail["reason"] = "No reported QB availability issue"
    overrides = db.overrides(game["game_id"])
    override = overrides.get(team)
    raw_records = snapshot["records"] if snapshot else []
    records = json.loads(raw_records) if isinstance(raw_records, str) else raw_records
    matches = [(r, *_resolve_player(r.get("player_name"), team, profiles)) for r in records if r.get("team") == team]
    base_record, confidence = next(((record, confidence) for record, player, confidence in matches if player and player["player_id"] == base["player_id"]), (None, None))
    replacement = None
    if override:
        replacement, _ = _resolve_player(override["player_name"], team, profiles)
        if replacement:
            detail.update(expected_qb=replacement["player_id"], expected_qb_name=replacement["name"], replacement_qb=replacement["player_id"], absence_weight=1.0, reason=f"Manual expected-QB override: {override['reason'] or replacement['name']}", match_confidence="manual")
    elif base_record:
        absence = weight(base_record)
        detail["absence_weight"] = absence
        detail["reason"] = f"Official {base_record.get('game_status') or base_record.get('practice_status') or 'injury'} report"
        detail["match_confidence"] = confidence
        detail["report"] = {key: base_record.get(key) for key in ("source", "player_name", "injury", "practice_status", "game_status", "retrieved_at")}
        alternatives = [p for p in profiles if p["team"] == team and p["player_id"] != base["player_id"]]
        replacement = max(alternatives, key=lambda p: (p["last_game_date"], p["last_game_dropbacks"]), default=None)
        detail["replacement_qb"] = replacement and replacement["player_id"]
    if replacement and detail["absence_weight"]:
        expected = replacement
        raw = detail["absence_weight"] * (expected["shrunk_epa_per_dropback"] - base["shrunk_epa_per_dropback"]) * base["expected_dropbacks"] * .65
        detail["adjustment"] = round(float(np.clip(raw, -7, 7)), 2)
    elif detail["absence_weight"]:
        detail["reason"] += "; no replacement QB profile available"
    prior = db.latest_assessment(game["game_id"], team)
    db.save_assessment(detail, now)
    detail["change"] = db.save_change(prior, detail, now)
    return detail


def injury_adjusted_predictions(store, base_predictions, *, now=None):
    """Apply current availability evidence to prospective base predictions only."""
    now = now or utcnow(); db = AvailabilityStore(store); snapshot = current_report(store)
    profile = db.latest_profile(); results = []
    for row in base_predictions:
        if row.get("home_score") is not None or row.get("away_score") is not None:
            continue
        home = assess_availability(store, {"game_id": row["game_id"], "team": row["home_team"]}, profile_row=profile, snapshot=snapshot, now=now)
        away = assess_availability(store, {"game_id": row["game_id"], "team": row["away_team"]}, profile_row=profile, snapshot=snapshot, now=now)
        home_wr = assess_wide_receivers(store, {"game_id": row["game_id"], "team": row["home_team"]}, snapshot=snapshot, now=now)
        away_wr = assess_wide_receivers(store, {"game_id": row["game_id"], "team": row["away_team"]}, snapshot=snapshot, now=now)
        home_rb = assess_skill_position(store, {"game_id": row["game_id"], "team": row["home_team"]}, "RB", snapshot=snapshot, now=now)
        away_rb = assess_skill_position(store, {"game_id": row["game_id"], "team": row["away_team"]}, "RB", snapshot=snapshot, now=now)
        home_te = assess_skill_position(store, {"game_id": row["game_id"], "team": row["home_team"]}, "TE", snapshot=snapshot, now=now)
        away_te = assess_skill_position(store, {"game_id": row["game_id"], "team": row["away_team"]}, "TE", snapshot=snapshot, now=now)
        home_edge = assess_edge_rushers(store, {"game_id": row["game_id"], "team": row["home_team"]}, snapshot=snapshot, now=now)
        away_edge = assess_edge_rushers(store, {"game_id": row["game_id"], "team": row["away_team"]}, snapshot=snapshot, now=now)
        home_skill = round(max(-3.0, home_wr["adjustment"] + home_rb["adjustment"] + home_te["adjustment"]), 2)
        away_skill = round(max(-3.0, away_wr["adjustment"] + away_rb["adjustment"] + away_te["adjustment"]), 2)
        adjusted = round(float(row["predicted_margin"]) + home["adjustment"] - away["adjustment"] + home_skill - away_skill + home_edge["adjustment"] - away_edge["adjustment"], 2)
        item = {"game_id":row["game_id"], "model_id":row.get("model_id", "baseline"), "created_at":now.isoformat(), "kickoff":row.get("gameday"), "base_margin":row["predicted_margin"], "adjusted_margin":adjusted, "assessments":[home,away,home_wr,away_wr,home_rb,away_rb,home_te,away_te,home_edge,away_edge]}
        db.save_prediction(item); results.append({**row, "injury_adjusted_margin": adjusted, "availability_assessments": [home, away], "wr_assessments": [home_wr, away_wr], "rb_assessments": [home_rb, away_rb], "te_assessments": [home_te, away_te], "edge_assessments": [home_edge, away_edge], "skill_adjustments": [home_skill, away_skill], "availability_changes": [change for change in (home.get("change"), away.get("change")) if change]})
    return results


def capture_all_availability_forecasts(store, *, now=None, forecast_models=None, adjust_predictions=None):
    """Save all available base forecasts, then their availability-adjusted counterparts.

    A challenger without a fitted artifact is reported but does not prevent the
    baseline or any available challenger from being preserved at this checkpoint.
    """
    now = now or utcnow()
    if forecast_models is None:
        from againstallodds.experiments import predict_models
        forecast_models = predict_models
    if adjust_predictions is None:
        adjust_predictions = injury_adjusted_predictions

    base_rows = forecast_models(store, now=now, save=True, model="all", capture_id=now.isoformat())
    available = [row for row in base_rows if row.get("available") and row.get("predicted_margin") is not None]
    unavailable = [row for row in base_rows if row not in available]
    adjusted = adjust_predictions(store, available, now=now) if available else []
    return {
        "base_rows": base_rows,
        "available_rows": available,
        "unavailable_rows": unavailable,
        "adjusted_rows": adjusted,
    }


CHECK_OFFSETS_HOURS = (168, 72, 24, 2, 85 / 60, 15 / 60)


def plan_injury_checks(store, *, mode="dashboard", now=None):
    """Persist check windows for upcoming games; Windows registration stays opt-in."""
    now = now or utcnow(); db = AvailabilityStore(store)
    plans = []
    for game in store.games():
        when = (game.get("kickoff") or game["gameday"]) if isinstance(game, dict) else (game.kickoff or game.gameday)
        kickoff = datetime.fromisoformat(when)
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        for hours in CHECK_OFFSETS_HOURS:
            due = kickoff.timestamp() - hours * 3600
            if due <= now.timestamp():
                continue
            label = f"{hours:g}h before kickoff"
            with store.connection() as conn:
                conn.execute("INSERT OR IGNORE INTO injury_checks(game_id,due_at,kind,mode,state) VALUES (?,?,?,?,?)", (game["game_id"] if isinstance(game, dict) else game.game_id, datetime.fromtimestamp(due, timezone.utc).isoformat(), label, mode, "planned"))
            plans.append({"game_id": game["game_id"] if isinstance(game, dict) else game.game_id, "due_at": datetime.fromtimestamp(due, timezone.utc).isoformat(), "label": label, "mode": mode})
    db.set_setting("check_mode", mode)
    return plans


def run_due_injury_checks(store, *, now=None):
    """Fetch both official feeds once when one or more planned windows are due."""
    now = now or utcnow()
    with store.connection() as conn:
        due = [dict(row) for row in conn.execute("SELECT * FROM injury_checks WHERE state='planned' AND due_at<=?", (now.isoformat(),))]
    if not due:
        return {"due": 0, "synced": False}
    results = []
    for source in ("injuries", "inactives"):
        try:
            results.append({"source": source, **sync_injuries(store, source=source, now=now)})
        except (AgainstAllOddsError, OSError) as error:
            results.append({"source": source, "error": str(error)})
    state = "done" if any("snapshot_id" in result for result in results) else "failed"
    with store.connection() as conn:
        conn.executemany("UPDATE injury_checks SET state=?, completed_at=? WHERE id=?", [(state, now.isoformat(), row["id"]) for row in due])
    changes = []
    if state == "done" and AvailabilityStore(store).latest_profile():
        from againstallodds.experiments import predict_models
        for forecast in injury_adjusted_predictions(store, predict_models(store, now=now, model="power-rating-v1"), now=now):
            changes.extend(forecast["availability_changes"])
    if AvailabilityStore(store).setting("windows_toasts", True):
        for change in changes:
            notify_windows("AgainstAllOdds availability", f"{change['team']}: quarterback availability changed")
            AvailabilityStore(store).mark_notified(change["id"], now)
    return {"due": len(due), "synced": state == "done", "results": results, "changes": changes}


def notify_windows(title, message):
    """Send a concise native toast without exposing report content to a shell."""
    xml = f'<toast><visual><binding template="ToastGeneric"><text>{xml_escape(str(title))}</text><text>{xml_escape(str(message))}</text></binding></visual></toast>'
    quoted = "'" + xml.replace("'", "''") + "'"
    script = f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null; $xml=New-Object Windows.Data.Xml.Dom.XmlDocument; $xml.LoadXml({quoted}); [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AgainstAllOdds').Show([Windows.UI.Notifications.ToastNotification]::new($xml))"
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def windows_runner_command(data_dir):
    """Return the narrowly scoped task command; registration remains explicit."""
    main = Path(__file__).resolve().parents[2] / "main.py"
    return f'"{sys.executable}" "{main}" --data-dir "{Path(data_dir).resolve()}" run-due-injury-checks'


def register_windows_runner(data_dir):
    """Create or update the opt-in 15-minute Windows background checker."""
    result = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "15", "/TN", "AgainstAllOdds-InjuryChecks", "/TR", windows_runner_command(data_dir)], capture_output=True, text=True, check=False)
    if result.returncode:
        raise AgainstAllOddsError(result.stderr.strip() or "Windows could not register the injury-check task.")
    return {"task": "AgainstAllOdds-InjuryChecks", "interval_minutes": 15}
