#!/usr/bin/env python3
"""
NHL ATG Lamp Lab -- live odds refresh (GitHub Actions).

Inputs (both committed in this repo):
  players_base.json  season-long model output, rebuilt by n8n a few times a day
  template.html      the real Lamp Lab widget, with a __SEED_DATA_JSON__ placeholder

Every run: fetch today's schedule (US Eastern date), live DraftKings anytime-goal-scorer
odds and game totals from Optic Odds, recompute edge/EV, drop anyone with no live line,
and write index.html for GitHub Pages.
The model itself (Poisson rates, matchup multipliers) is NOT recomputed here.
"""
import os, sys, json, difflib, datetime, urllib.request, urllib.parse
from zoneinfo import ZoneInfo

API_KEY = os.environ.get("OPTIC_ODDS_API_KEY")
API_BASE = "https://api.opticodds.com/api/v3"
ET = ZoneInfo("America/New_York")

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
    et_today = datetime.datetime.now(ET).date()
    after = et_today.strftime("%Y-%m-%dT00:00:00Z")
    before = (et_today + datetime.timedelta(days=1)).strftime("%Y-%m-%dT10:00:00Z")
    qs = urllib.parse.urlencode({"sport": "hockey", "league": "nhl",
                                 "start_date_after": after, "start_date_before": before})
    games = []
    for g in http_get(f"{API_BASE}/fixtures?{qs}").get("data", []):
        # Only games that haven't started: once a game is underway, the sportsbook prices are
        # in-play, and comparing them with the pre-game model produces fake "edges".
        if g.get("status") in ("completed", "live", "cancelled", "postponed", "suspended"):
            continue
        try:
            start = datetime.datetime.fromisoformat(g["start_date"].replace("Z", "+00:00"))
            if start <= datetime.datetime.now(datetime.timezone.utc):
                continue
        except Exception:
            pass
        home = g["home_competitors"][0]["name"] if g.get("home_competitors") else ""
        away = g["away_competitors"][0]["name"] if g.get("away_competitors") else ""
        games.append({"id": g["id"], "home": TEAM_ABBR.get(home, home), "away": TEAM_ABBR.get(away, away)})
    return games


def fetch_odds(fixture_id, market):
    qs = urllib.parse.urlencode({"fixture_id": fixture_id, "market": market, "sportsbook": "draftkings"})
    try:
        return http_get(f"{API_BASE}/fixtures/odds?{qs}")
    except Exception as e:
        print(f"  {market} fetch failed for {fixture_id}: {e}", file=sys.stderr)
        return {}


def get_market_data(games):
    odds_map, totals = {}, {}
    for g in games:
        for item in fetch_odds(g["id"], "anytime_goal_scorer").get("data", []):
            for o in item.get("odds", []):
                if o.get("name") and o.get("price") is not None:
                    odds_map[o["name"].lower()] = o["price"]
        for item in fetch_odds(g["id"], "total_goals").get("data", []):
            for o in item.get("odds", []):
                if o.get("points") and "Over" in str(o.get("name", "")):
                    totals[g["away"] + "-" + g["home"]] = o["points"]
    return odds_map, totals


def implied_prob(price):
    return 100 / (price + 100) * 100 if price > 0 else abs(price) / (abs(price) + 100) * 100


def refresh_players(pool, odds_map, opp_map):
    keys = list(odds_map)
    kept = []
    for p in pool:
        if p["t"] not in opp_map:
            continue
        if p.get("opp") and opp_map[p["t"]] != p["opp"]:
            # Pool was built for a different slate (rolled over before n8n rebuilt it).
            # Its matchup numbers would not match the opponent label, so skip until n8n catches up.
            continue
        p = dict(p)
        p["opp"] = opp_map[p["t"]]
        key = p["n"].lower()
        price = odds_map.get(key)
        if price is None:
            close = difflib.get_close_matches(key, keys, n=1, cutoff=0.85)
            if close:
                price = odds_map[close[0]]
        if price is None:
            continue
        p["dk"] = price
        p["edge"] = round(p["adj"] - implied_prob(price), 1)
        p["ev"] = 1 if p["edge"] > 0 else 0
        p["hot"], p["due"], p["dtd"] = 0, 0, 0  # placeholders until L10 / injury feeds exist
        p.pop("lam", None)
        kept.append(p)
    kept.sort(key=lambda x: -x["adj"])
    return kept


def main():
    if not API_KEY:
        print("OPTIC_ODDS_API_KEY not set", file=sys.stderr); sys.exit(1)
    here = os.path.dirname(os.path.abspath(__file__))
    pool = json.load(open(os.path.join(here, "players_base.json")))
    template = open(os.path.join(here, "template.html")).read()

    games = get_todays_games()
    if not games:
        print("No games today -- leaving existing index.html untouched."); return
    opp_map = {}
    for g in games:
        opp_map[g["home"]] = g["away"]; opp_map[g["away"]] = g["home"]

    odds_map, totals = get_market_data(games)
    print(f"Live odds for {len(odds_map)} players across {len(games)} games; totals for {len(totals)} games.")
    if not odds_map:
        print("No odds returned -- leaving existing index.html untouched."); return

    players = refresh_players(pool, odds_map, opp_map)
    print(f"Board: {len(players)} players (pool had {len(pool)}).")
    if not players:
        print("Nothing to show -- leaving existing index.html untouched."); return

    killers = [p["n"] for p in players if p.get("sp", {}).get("gp", 0) >= 2 and p.get("sp", {}).get("atg", 0) >= 50]
    now_et = datetime.datetime.now(ET)
    seed = {
        "date": now_et.strftime("%b %-d, %Y, %-I:%M %p ET"),
        "games": [{"away": g["away"], "home": g["home"]} for g in games],
        "totals": totals,
        "killers": killers,
        "players": players,
    }
    out = template.replace("__SEED_DATA_JSON__", json.dumps(seed, separators=(",", ":")))
    open(os.path.join(here, "index.html"), "w").write(out)
    print("Wrote index.html.")


if __name__ == "__main__":
    main()
