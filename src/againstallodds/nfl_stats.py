"""Bounded seasonal nflverse downloads and reproducible play aggregation."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.nfl_data import ALIASES, TEAMS, utcnow
from againstallodds.research_store import ResearchStore

STAT_VERSION = "scrimmage-v1"
SOURCE = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"
COLUMNS = ["game_id", "play_id", "drive", "season_type", "posteam", "defteam", "play_type", "qb_kneel", "qb_spike", "qb_dropback", "epa", "yards_gained", "sack", "interception", "fumble_lost"]
RATES = {"epa": ("epa_sum", "epa_n"), "success": ("success_sum", "epa_n"), "dropback_epa": ("dropback_epa_sum", "dropback_epa_n"), "rush_epa": ("rush_epa_sum", "rush_epa_n"), "explosive": ("explosive_sum", "yards_n"), "sack": ("sack_sum", "dropbacks"), "turnover": ("turnover_sum", "turnover_n")}


def aggregate_plays(frame):
    missing = set(COLUMNS) - set(frame.columns)
    if missing:
        raise AgainstAllOddsError(f"Missing play columns: {', '.join(sorted(missing))}")
    if frame.duplicated(["game_id", "drive", "play_id"]).any():
        raise AgainstAllOddsError("Duplicate play identifiers in statistics download.")
    eligible = frame["season_type"].isin(["REG", "POST"]) & frame["play_type"].isin(["pass", "run"]) & frame["qb_kneel"].eq(0) & frame["qb_spike"].eq(0)
    plays = frame.loc[eligible].copy()
    if plays.empty:
        raise AgainstAllOddsError("No eligible scrimmage plays in statistics download.")
    for col in ("posteam", "defteam"):
        plays[col] = plays[col].replace(ALIASES).map(TEAMS)
    if plays[["game_id", "posteam", "defteam"]].isna().any().any() or plays["posteam"].eq(plays["defteam"]).any():
        raise AgainstAllOddsError("Unknown franchise or invalid matchup in play data.")
    for col in ("epa", "yards_gained", "qb_dropback", "sack", "interception", "fumble_lost"):
        plays[col] = pd.to_numeric(plays[col], errors="raise")
        if np.isinf(plays[col]).any():
            raise AgainstAllOddsError(f"Nonfinite {col} in play data.")
    valid_epa = plays.epa.notna()
    dropback = plays.qb_dropback.eq(1)
    rush = plays.play_type.eq("run") & plays.qb_dropback.eq(0)
    valid_yards = plays.yards_gained.notna()
    turnover_known = plays.interception.notna() & plays.fumble_lost.notna()
    sums = pd.DataFrame({
        "game_id": plays.game_id, "posteam": plays.posteam, "defteam": plays.defteam,
        "plays": 1, "epa_sum": plays.epa.fillna(0), "epa_n": valid_epa.astype(int),
        "success_sum": (valid_epa & plays.epa.gt(0)).astype(int),
        "dropback_epa_sum": plays.epa.where(dropback, 0).fillna(0), "dropback_epa_n": (valid_epa & dropback).astype(int),
        "rush_epa_sum": plays.epa.where(rush, 0).fillna(0), "rush_epa_n": (valid_epa & rush).astype(int),
        "explosive_sum": ((plays.play_type.eq("pass") & plays.yards_gained.ge(20)) | (rush & plays.yards_gained.ge(10))).astype(int),
        "yards_n": valid_yards.astype(int), "sack_sum": plays.sack.where(dropback, 0).fillna(0),
        "dropbacks": (dropback & plays.sack.notna()).astype(int),
        "turnover_sum": ((plays.interception.eq(1) | plays.fumble_lost.eq(1)) & turnover_known).astype(int), "turnover_n": turnover_known.astype(int),
    })
    numeric = [c for c in sums if c not in {"game_id", "posteam", "defteam"}]
    result = {}
    for side, column in (("off", "posteam"), ("def", "defteam")):
        grouped = sums.groupby(["game_id", column], sort=True)[numeric].sum()
        for (gid, team), row in grouped.iterrows():
            result.setdefault(str(gid), {}).setdefault(team, {})[side] = {k: float(v) for k, v in row.items()}
    for gid, teams in result.items():
        if len(teams) != 2 or any(set(v) != {"off", "def"} for v in teams.values()):
            raise AgainstAllOddsError(f"Incomplete two-sided statistics for {gid}.")
    return result


def fetch_season(season, root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        request = Request(SOURCE.format(season=season), headers={"User-Agent": "AgainstAllOdds/0.3"})
        with urlopen(request, timeout=60) as response, tempfile.NamedTemporaryFile(dir=root, suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            digest, size = hashlib.sha256(), 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 250_000_000:
                    raise AgainstAllOddsError("Seasonal download exceeds the 250 MB limit.")
                digest.update(chunk)
                file.write(chunk)
            file.flush()
            os.fsync(file.fileno())
        # Validate and load only needed columns before publishing the source file.
        frame = pq.read_table(temporary, columns=COLUMNS).to_pandas()
        aggregates = aggregate_plays(frame)
        path = root / f"{season}-{digest.hexdigest()}.parquet"
        temporary.replace(path)
        return digest.hexdigest(), path, aggregates
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def sync_stats(store, start=2015, end=2026, *, refresh_history=False, progress=None, now=None, fetch=None):
    if not 1999 <= start <= end <= 2100:
        raise AgainstAllOddsError("Statistics seasons must be ordered and start in 1999 or later.")
    research = ResearchStore(store)
    cached = research.latest_stats()
    now = now or utcnow()
    report = []
    for season in range(start, end + 1):
        if season in cached and cached[season]["version"] == STAT_VERSION and season != end and not refresh_history:
            report.append({"season": season, "status": "cached"})
            continue
        if progress:
            progress(f"Downloading and aggregating {season} play-by-play…")
        try:
            digest, path, aggregates = (fetch or fetch_season)(season, store.root / "raw" / "pbp")
            if any(not gid.startswith(f"{season}_") for gid in aggregates):
                raise AgainstAllOddsError("Downloaded games do not match the requested season.")
            sid = research.save_stats(season, digest, path, aggregates, STAT_VERSION, now)
            report.append({"season": season, "status": "imported", "games": len(aggregates), "snapshot": sid})
        except (OSError, ValueError, AgainstAllOddsError) as error:
            research.stat_error(season, error, now)
            # Each season commits separately so a retry resumes completed work.
            report.append({"season": season, "status": "failed", "error": str(error)})
            if progress:
                progress(f"{season}: unavailable; retained any cached statistics.")
    return report
