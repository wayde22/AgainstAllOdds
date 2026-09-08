"""Pregame venue and weather snapshots using the free Open-Meteo forecast API."""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from datetime import timedelta
from againstallodds.exceptions import AgainstAllOddsError
from againstallodds.nfl_data import utcnow

SOURCE = "open-meteo"
TURF_TEAMS = {"Atlanta Falcons", "Carolina Panthers", "Cincinnati Bengals", "Dallas Cowboys", "Detroit Lions", "Houston Texans", "Indianapolis Colts", "Las Vegas Raiders", "Los Angeles Chargers", "Los Angeles Rams", "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants", "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers", "Seattle Seahawks"}
TIMEZONE_OFFSETS = {"Arizona Cardinals": -7, "Denver Broncos": -7, "Las Vegas Raiders": -8, "Los Angeles Chargers": -8, "Los Angeles Rams": -8, "San Francisco 49ers": -8, "Seattle Seahawks": -8, "Dallas Cowboys": -6, "Houston Texans": -6, "Kansas City Chiefs": -6, "Minnesota Vikings": -6, "New Orleans Saints": -6, "Tennessee Titans": -6, "Green Bay Packers": -6, "Chicago Bears": -6, "Indianapolis Colts": -5, "Detroit Lions": -5, "Atlanta Falcons": -5, "Baltimore Ravens": -5, "Buffalo Bills": -5, "Carolina Panthers": -5, "Cincinnati Bengals": -5, "Cleveland Browns": -5, "Jacksonville Jaguars": -5, "Miami Dolphins": -5, "New England Patriots": -5, "New York Giants": -5, "New York Jets": -5, "Philadelphia Eagles": -5, "Pittsburgh Steelers": -5, "Tampa Bay Buccaneers": -5, "Washington Commanders": -5}
# City-level coordinates are sufficient for a conservative stadium forecast.
VENUES = {"Arizona Cardinals":(33.53,-112.26,"retractable"),"Atlanta Falcons":(33.76,-84.40,"retractable"),"Baltimore Ravens":(39.28,-76.62,"outdoor"),"Buffalo Bills":(42.77,-78.79,"outdoor"),"Carolina Panthers":(35.23,-80.85,"outdoor"),"Chicago Bears":(41.86,-87.62,"outdoor"),"Cincinnati Bengals":(39.10,-84.52,"outdoor"),"Cleveland Browns":(41.51,-81.70,"outdoor"),"Dallas Cowboys":(32.75,-97.09,"retractable"),"Denver Broncos":(39.74,-105.02,"outdoor"),"Detroit Lions":(42.34,-83.05,"indoor"),"Green Bay Packers":(44.50,-88.06,"outdoor"),"Houston Texans":(29.68,-95.41,"retractable"),"Indianapolis Colts":(39.76,-86.16,"retractable"),"Jacksonville Jaguars":(30.32,-81.64,"outdoor"),"Kansas City Chiefs":(39.05,-94.48,"outdoor"),"Las Vegas Raiders":(36.09,-115.18,"indoor"),"Los Angeles Chargers":(33.95,-118.34,"indoor"),"Los Angeles Rams":(33.95,-118.34,"indoor"),"Miami Dolphins":(25.96,-80.24,"outdoor"),"Minnesota Vikings":(44.97,-93.26,"indoor"),"New England Patriots":(42.09,-71.26,"outdoor"),"New Orleans Saints":(29.95,-90.08,"indoor"),"New York Giants":(40.81,-74.07,"outdoor"),"New York Jets":(40.81,-74.07,"outdoor"),"Philadelphia Eagles":(39.90,-75.17,"outdoor"),"Pittsburgh Steelers":(40.45,-80.01,"outdoor"),"San Francisco 49ers":(37.40,-121.97,"outdoor"),"Seattle Seahawks":(47.60,-122.33,"outdoor"),"Tampa Bay Buccaneers":(27.98,-82.50,"outdoor"),"Tennessee Titans":(36.17,-86.77,"outdoor"),"Washington Commanders":(38.91,-76.86,"outdoor")}

def sync_weather(store, *, fetch=None, now=None):
    now=now or utcnow(); saved=[]
    for game in store.games():
        if game.complete or not game.kickoff or game.home_team not in VENUES: continue
        kickoff = datetime.fromisoformat(game.kickoff.replace("Z", "+00:00"))
        if kickoff > now + timedelta(days=7):
            continue
        lat,lon,roof=VENUES[game.home_team]
        forecast={"venue":game.home_team,"roof":roof,"latitude":lat,"longitude":lon}
        if roof == "outdoor":
            try:
                url="https://api.open-meteo.com/v1/forecast?"+urlencode({"latitude":lat,"longitude":lon,"hourly":"temperature_2m,precipitation,wind_speed_10m","timezone":"UTC"})
                payload=(fetch or (lambda u: urlopen(Request(u,headers={"User-Agent":"AgainstAllOdds/0.4"}),timeout=20).read()))(url)
                data=json.loads(payload.decode() if isinstance(payload,bytes) else payload)
                forecast["hourly"]=data.get("hourly",{})
            except (OSError, ValueError, json.JSONDecodeError) as error: forecast["error"]=str(error)
        saved.append({"game_id":game.game_id,"weather_id":store.save_weather(game.game_id,SOURCE,forecast,now)})
    return saved


def weather_summary(snapshot, kickoff):
    """Return the forecast hour closest to kickoff without modifying its raw snapshot."""
    if not snapshot:
        return {}
    data = json.loads(snapshot["forecast"])
    summary = {"weather_snapshot_id": snapshot["id"], "weather_retrieved_at": snapshot["retrieved_at"],
               "weather_source": snapshot["source"], "venue_roof": data.get("roof")}
    if data.get("roof") != "outdoor":
        return summary
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return summary
    target = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(timezone.utc)
    def distance(value):
        return abs((datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) - target).total_seconds())
    index = min(range(len(times)), key=lambda i: distance(times[i]))
    summary.update({"weather_forecast_time": times[index],
                    "weather_temperature_c": _at(hourly.get("temperature_2m"), index),
                    "weather_wind_kph": _at(hourly.get("wind_speed_10m"), index),
                    "weather_precipitation_mm": _at(hourly.get("precipitation"), index)})
    return summary


def _at(values, index):
    return values[index] if isinstance(values, list) and index < len(values) else None


def venue_context(store, game):
    """Pregame schedule context; values are descriptive until forward samples support use."""
    home, away = VENUES.get(game.home_team), VENUES.get(game.away_team)
    if not home:
        return {}
    prior = [g for g in store.games() if g.complete and g.season == game.season and g.gameday < game.gameday]
    def rest(team):
        dates = [g.gameday for g in prior if team in (g.home_team, g.away_team)]
        return (datetime.fromisoformat(game.gameday) - datetime.fromisoformat(max(dates))).days if dates else 7
    home_rest, away_rest = rest(game.home_team), rest(game.away_team)
    return {"venue_roof": home[2], "venue_surface": "turf" if game.home_team in TURF_TEAMS else "grass",
            "away_travel_miles": round(_miles(away, home), 0) if away else None,
            "home_rest_days": home_rest, "away_rest_days": away_rest,
            "rest_day_difference": home_rest - away_rest,
            "timezone_difference_hours": None if game.home_team not in TIMEZONE_OFFSETS or game.away_team not in TIMEZONE_OFFSETS else TIMEZONE_OFFSETS[game.home_team] - TIMEZONE_OFFSETS[game.away_team]}


def _miles(first, second):
    lat1, lon1, *_ = first; lat2, lon2, *_ = second
    a = math.sin(math.radians(lat2-lat1)/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(math.radians(lon2-lon1)/2)**2
    return 3958.8 * 2 * math.asin(math.sqrt(a))
