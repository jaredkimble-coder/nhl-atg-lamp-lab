#!/usr/bin/env python3
"""
NHL ATG Lamp Lab -- live odds refresh.

Reads players_base.json (season-long model output: base%, adj%, matchup
multipliers -- these do NOT change intra-day) and template.html (the
static page shell), fetches TODAY's real schedule and live DraftKings
anytime-goal-scorer odds from the Optic Odds API, matches players to
those odds by name (exact then fuzzy, to survive minor spelling
differences between data sources), recomputes edge/EV, drops any
player with no live odds line right now, and writes the result to
index.html for GitHub Pages to serve.

This script does NOT re-run the Poisson/matchup model itself -- that
still requires a manual refresh (see the RotoWire pipeline). It only
keeps live odds and the "who's actually priced right now" filter
fresh, on whatever schedule the GitHub Actions workflow runs this on.
"""
import os
import sys
import json
import math
import difflib
import datetime
import urllib.request
import urllib.parse

API_KEY = os.environ.get("OPTIC_ODDS_API_KEY")
API_BASE = "https://api.opticodds.com/api/v3"
BOOKS = ["draftkings"]

TEAM_ABBR = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF",
    "Carolina Hurricanes": "CAR", "Columbus Blue Jackets": "CBJ",
    "Calgary Flames": "CGY", "Chicago Blackhawks": "CHI", "Colorado Avalanche": "COL",
    "Dallas Stars": "DAL", "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM",
    "Florida Panthers": "FLA", "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN",
    "Montreal Canadiens": "MTL", "New Jersey Devils": "NJD", "Nashville Predators": "NSH",
    "New York Islanders": "NYI", "New York Rangers": "NYR", "Ottawa Senators": "OTT",
    "Philadelphia Flyers": "PHI", "Pittsburgh Penguins": "PIT", "Seattle Kraken": "SEA",
    "San Jose Sharks": "SJS", "St. Louis Blues": "STL", "Tampa Bay Lightning": "TBL",
    "Toronto Maple Leafs": "TOR", "Utah Mammoth": "UTA", "Vancouver Canucks": "VAN",
    "Vegas Golden Knights": "VGK", "Winnipeg Jets": "WPG", "Washington Capitals": "WSH",
}


def http_get(url):
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def get_todays_games():
    now = datetime.datetime.now(datetime.timezone.utc)
    after = now.strftime("%Y-%m-%dT00:00:00Z")
    before = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%dT10:00:00Z")
    qs = urllib.parse.urlencode({
        "sport": "hockey", "league": "nhl",
        "start_date_after": after, "start_date_before": before,
    })
    data = http_get(f"{API_BASE}/fixtures?{qs}")
    games = []
    for g in data.get("data", []):
        if g.get("status") == "completed":
            continue
        home = g["home_competitors"][0]["name"] if g.get("home_competitors") else ""
        away = g["away_competitors"][0]["name"] if g.get("away_competitors") else ""
        games.append({
            "id": g["id"],
            "home": TEAM_ABBR.get(home, home),
            "away": TEAM_ABBR.get(away, away),
        })
    return games


def get_live_odds_map(fixture_ids):
    odds_map = {}
    for fid in fixture_ids:
        for book in BOOKS:
            qs = urllib.parse.urlencode({
                "fixture_id": fid, "market": "anytime_goal_scorer", "sportsbook": book,
            })
            try:
                data = http_get(f"{API_BASE}/fixtures/odds?{qs}")
            except Exception as e:
                print(f"  odds fetch failed for {fid}/{book}: {e}", file=sys.stderr)
                continue
            for item in data.get("data", []):
                for o in item.get("odds", []):
                    name = o.get("name")
                    price = o.get("price")
                    if name and price is not None:
                        odds_map[name.lower()] = price
    return odds_map


def implied_prob(price):
    if price > 0:
        return 100 / (price + 100) * 100
    return abs(price) / (abs(price) + 100) * 100


def refresh_players(pool, odds_map, opp_map):
    odds_keys = list(odds_map.keys())
    kept = []
    for p in pool:
        # only consider players whose team is actually playing today
        if p["t"] not in opp_map:
            continue
        p = dict(p)
        p["opp"] = opp_map[p["t"]]

        key = p["n"].lower()
        price = odds_map.get(key)
        if price is None:
            close = difflib.get_close_matches(key, odds_keys, n=1, cutoff=0.85)
            if close:
                price = odds_map[close[0]]

        if price is None:
            continue  # no live odds anywhere -- drop from tonight's board

        p["dk"] = price
        edge = round(p["adj"] - implied_prob(price), 1)
        p["edge"] = edge
        p["ev"] = 1 if edge > 0 else 0
        kept.append(p)

    kept.sort(key=lambda x: -x["adj"])
    return kept


def main():
    if not API_KEY:
        print("OPTIC_ODDS_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "players_base.json")) as f:
        pool = json.load(f)
    with open(os.path.join(here, "template.html")) as f:
        template = f.read()

    games = get_todays_games()
    if not games:
        print("No games today -- leaving existing index.html untouched.")
        return

    opp_map = {}
    games_out = []
    for g in games:
        opp_map[g["home"]] = g["away"]
        opp_map[g["away"]] = g["home"]
        games_out.append({"away": g["away"], "home": g["home"]})

    fixture_ids = [g["id"] for g in games]
    odds_map = get_live_odds_map(fixture_ids)
    print(f"Live odds available for {len(odds_map)} players across {len(games)} games.")

    fresh_players = refresh_players(pool, odds_map, opp_map)
    print(f"Board after refresh: {len(fresh_players)} players (had {len(pool)} in base pool).")

    now = datetime.datetime.now(datetime.timezone.utc)
    seed_data = {
        "date": now.strftime("%Y-%m-%d"),
        "games": games_out,
        "players": fresh_players,
        "killers": [],
        "updatedAt": now.isoformat(),
    }

    out_html = template.replace(
        "__SEED_DATA_JSON__", json.dumps(seed_data, separators=(",", ":"))
    )

    with open(os.path.join(here, "index.html"), "w") as f:
        f.write(out_html)
    print("Wrote index.html.")


if __name__ == "__main__":
    main()
