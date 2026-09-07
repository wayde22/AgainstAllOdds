"""Download and normalize the published nflverse schedule, without scraping."""
from __future__ import annotations

import csv
import io
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from againstallodds.exceptions import AgainstAllOddsError

SOURCE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
CODES = "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV LAC LA MIA MIN NE NO NYG NYJ PHI PIT SF SEA TB TEN WAS".split()
# Use explicit franchise names; alphabetical display order is not an identifier.
NAMES = ["Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills", "Carolina Panthers", "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns", "Dallas Cowboys", "Denver Broncos", "Detroit Lions", "Green Bay Packers", "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars", "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers", "Los Angeles Rams", "Miami Dolphins", "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants", "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers", "San Francisco 49ers", "Seattle Seahawks", "Tampa Bay Buccaneers", "Tennessee Titans", "Washington Commanders"]
TEAMS = dict(zip(CODES, NAMES, strict=True))
ALIASES = {"OAK": "LV", "SD": "LAC", "STL": "LA", "LAR": "LA", "JAC": "JAX", "WSH": "WAS"}


@dataclass(frozen=True)
class NFLGame:
    game_id: str
    season: int
    week: int
    game_type: str
    gameday: str
    kickoff: str | None
    home_team: str
    away_team: str
    home_score: int | None
    away_score: int | None
    neutral_site: bool
    market_margin: float | None

    @property
    def complete(self) -> bool:
        return self.home_score is not None and self.away_score is not None

    def to_dict(self) -> dict:
        return asdict(self)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def download() -> bytes:
    try:
        request = Request(SOURCE_URL, headers={"User-Agent": "AgainstAllOdds/0.2"})
        with urlopen(request, timeout=30) as response:
            payload = response.read(10_000_001)
        if len(payload) > 10_000_000:
            raise ValueError("Schedule download exceeds 10 MB")
        return payload
    except (OSError, ValueError) as error:
        raise AgainstAllOddsError(f"NFL download failed: {error}") from error


def parse_schedule(payload: bytes, start: int = 2015, end: int = 2026) -> list[NFLGame]:
    if start > end:
        raise AgainstAllOddsError("Start season must not exceed end season.")
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        required = {"game_id", "season", "week", "game_type", "gameday", "gametime", "home_team", "away_team", "home_score", "away_score", "location", "spread_line"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Missing required nflverse columns")
        games, seen = [], set()
        for row in reader:
            season = int(row["season"])
            if not start <= season <= end or row["game_type"] not in {"REG", "WC", "DIV", "CON", "SB"}:
                continue
            game_id = row["game_id"].strip()
            if not game_id or game_id in seen:
                raise ValueError(f"Blank or duplicate game ID: {game_id}")
            seen.add(game_id)
            home, away = [TEAMS[ALIASES.get(row[key], row[key])] for key in ("home_team", "away_team")]
            if home == away:
                raise ValueError(f"Same franchise on both sides: {game_id}")
            day = date.fromisoformat(row["gameday"])
            kickoff = None
            if row["gametime"] not in {"", "NA", "TBD"}:
                kickoff = datetime.fromisoformat(f"{day}T{row['gametime']}").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc).isoformat()
            scores = [None if row[key] in {"", "NA"} else int(row[key]) for key in ("home_score", "away_score")]
            if any(score is not None and score < 0 for score in scores) or (scores[0] is None) != (scores[1] is None):
                raise ValueError(f"Invalid scores: {game_id}")
            spread = None if row["spread_line"] in {"", "NA"} else float(row["spread_line"])
            if spread is not None and not math.isfinite(spread):
                raise ValueError(f"Nonfinite spread: {game_id}")
            if row["location"] not in {"Home", "Neutral"} or int(row["week"]) < 1:
                raise ValueError(f"Invalid location or week: {game_id}")
            games.append(NFLGame(game_id, season, int(row["week"]), row["game_type"], day.isoformat(), kickoff, home, away, *scores, row["location"] == "Neutral", spread))
        if not games:
            raise ValueError("No games in the requested seasons")
        return sorted(games, key=lambda g: (g.gameday, g.game_id))
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise AgainstAllOddsError(f"Invalid NFL schedule: {error}") from error
