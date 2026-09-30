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
import os, sys, json, difflib, datetime, unicodedata, urllib.request, urllib.parse
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


# Common first-name nicknames that are not simple prefixes (Matt/Matthew, Zach/Zachary are handled by the prefix rule).
NICKNAMES = {frozenset(p) for p in [("nick", "nicholas"), ("mike", "michael"), ("jake", "jacob"),
                                     ("tony", "anthony"), ("bobby", "robert"), ("bob", "robert")]}


def norm(name):
    """Lower-case, accent-free, punctuation-free form of a player name, for cross-source matching."""
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch)).lower()
    return " ".join("".join(ch if ch.isalnum() or ch == " " else " " for ch in n).split())


def name_match(a, b, cutoff=0.9):
    """Same player? Exact/near spelling match, or a nickname (same last name and one first name is a
    prefix of the other: Matt/Matthew, Zach/Zachary, Nick/Nicholas). Only ever used within one team."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= cutoff:
        return True
    ta, tb = na.split(), nb.split()
    if len(ta) == len(tb) >= 2 and ta[-1] == tb[-1] and ta[1:-1] == tb[1:-1]:
        fa, fb = ta[0], tb[0]
        if frozenset((fa, fb)) in NICKNAMES:
            return True
        short, long_ = (fa, fb) if len(fa) <= len(fb) else (fb, fa)
        return len(short) >= 3 and long_.startswith(short)
    return False


def get_teams_played_yesterday():
    """Teams with a game on the previous US-Eastern day (for the 'Opp on B2B' filter)."""
    et_today = datetime.datetime.now(ET).date()
    after = (et_today - datetime.timedelta(days=1)).strftime("%Y-%m-%dT09:00:00Z")
    before = et_today.strftime("%Y-%m-%dT09:00:00Z")
    qs = urllib.parse.urlencode({"sport": "hockey", "league": "nhl",
                                 "start_date_after": after, "start_date_before": before})
    teams = set()
    for g in http_get(f"{API_BASE}/fixtures?{qs}").get("data", []):
        if g.get("status") in ("cancelled", "postponed"):
            continue
        for side in ("home_competitors", "away_competitors"):
            if g.get(side):
                nm = g[side][0]["name"]
                teams.add(TEAM_ABBR.get(nm, nm))
    return teams


def load_pp1():
    """PP1 units by team from lineups.json (published by n8n from DailyFaceOff), or None if unavailable."""
    try:
        d = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lineups.json")))
        teams = d.get("teams", {})
        if len(teams) < 26:
            print("lineups.json has too few teams -- PP1 falls back to the built-in list.", file=sys.stderr)
            return None
        return {t: e.get("pp1", []) for t, e in teams.items() if len(e.get("pp1", [])) >= 3}
    except FileNotFoundError:
        print("lineups.json not found -- PP1 falls back to the built-in list.", file=sys.stderr)
    except Exception as e:
        print(f"lineups.json unreadable ({e}) -- PP1 falls back to the built-in list.", file=sys.stderr)
    return None


def load_injuries(max_age_hours=8):
    """Injury status by team from lineups.json (DailyFaceOff, refreshed by n8n).
    Returns {team: {normalized_name: 'out' | 'dtd'}} or None if the file is missing/stale.
    Stale data is ignored on purpose: a frozen injury list is worse than none."""
    try:
        d = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lineups.json")))
    except Exception as e:
        print(f"lineups.json unavailable for injuries ({e}) -- skipping injury handling.", file=sys.stderr)
        return None
    try:
        age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(d["updatedAt"].replace("Z", "+00:00"))
        if age > datetime.timedelta(hours=max_age_hours):
            print(f"lineups.json is {age.total_seconds()/3600:.1f}h old -- skipping injury handling.", file=sys.stderr)
            return None
    except Exception:
        print("lineups.json has no valid timestamp -- skipping injury handling.", file=sys.stderr)
        return None
    out = {}
    for team, e in d.get("teams", {}).items():
        m = {}
        for inj in e.get("injuries", []):
            status = (inj.get("s") or "").lower()
            if status in ("out", "ir"):
                m[norm(inj["n"])] = "out"                      # out always wins
            elif status == "dtd" or inj.get("gtd"):
                m.setdefault(norm(inj["n"]), "dtd")
        out[team] = m
    return out


def injury_status(team_map, name):
    """'out', 'dtd' or None for a player, matched by name within their own team only."""
    if not team_map:
        return None
    n = norm(name)
    if n in team_map:
        return team_map[n]
    for k, v in team_map.items():
        if name_match(n, k, 0.93):
            return v
    return None


def load_streaks(max_age_hours=30):
    """Hot / Due flags from streaks.json (computed by n8n twice a day). Returns (hot_keys, due_keys) or (None, None)
    when the file is missing or stale -- a frozen streak list is worse than none."""
    try:
        d = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "streaks.json")))
        age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(d["updatedAt"].replace("Z", "+00:00"))
        if age > datetime.timedelta(hours=max_age_hours):
            print(f"streaks.json is {age.total_seconds()/3600:.1f}h old -- Hot/Due flags left off.", file=sys.stderr)
            return None, None
        return set(d.get("hot", [])), set(d.get("due", []))
    except FileNotFoundError:
        print("streaks.json not found -- Hot/Due flags left off.", file=sys.stderr)
    except Exception as e:
        print(f"streaks.json unreadable ({e}) -- Hot/Due flags left off.", file=sys.stderr)
    return None, None


def fetch_odds(fixture_id, market):
    qs = urllib.parse.urlencode({"fixture_id": fixture_id, "market": market, "sportsbook": "draftkings"})
    try:
        return http_get(f"{API_BASE}/fixtures/odds?{qs}")
    except Exception as e:
        print(f"  {market} fetch failed for {fixture_id}: {e}", file=sys.stderr)
        return {}


def get_market_data(games):
    odds_map, totals, odds_names = {}, {}, {}
    for g in games:
        for item in fetch_odds(g["id"], "anytime_goal_scorer").get("data", []):
            for o in item.get("odds", []):
                if o.get("name") and o.get("price") is not None:
                    odds_map[o["name"].lower()] = o["price"]
                    odds_names[o["name"].lower()] = o["name"]
        for item in fetch_odds(g["id"], "total_goals").get("data", []):
            for o in item.get("odds", []):
                if o.get("points") and "Over" in str(o.get("name", "")):
                    totals[g["away"] + "-" + g["home"]] = o["points"]
    return odds_map, totals, odds_names


def implied_prob(price):
    return 100 / (price + 100) * 100 if price > 0 else abs(price) / (abs(price) + 100) * 100


RENAMES = []
REMOVED_OUT = []
FLAGGED_DTD = []


def refresh_players(pool, odds_map, opp_map, odds_names=None, pp1_by_team=None, b2b_teams=None, injuries=None, hot_keys=None, due_keys=None):
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
        streak_key = norm(p["n"]) + "|" + ("D" if p.get("p") == "D" else "F")
        key = p["n"].lower()
        price = odds_map.get(key)
        if price is None:
            close = difflib.get_close_matches(key, keys, n=1, cutoff=0.85)
            if close:
                price = odds_map[close[0]]
                if odds_names and odds_names.get(close[0]) and odds_names[close[0]] != p["n"]:
                    RENAMES.append((p["n"], odds_names[close[0]]))
                    p["n"] = odds_names[close[0]]  # use the sportsbook's correct spelling (fixes dropped letters/accents)
        if price is None:
            continue
        if pp1_by_team is not None and p["t"] in pp1_by_team:
            p["pp1"] = 1 if any(name_match(p["n"], n) for n in pp1_by_team[p["t"]]) else 0
        if b2b_teams is not None:
            p["b2b"] = 1 if p["opp"] in b2b_teams else 0
        p["dk"] = price
        p["edge"] = round(p["adj"] - implied_prob(price), 1)
        p["ev"] = 1 if p["edge"] > 0 else 0
        p["hot"] = 1 if (hot_keys is not None and streak_key in hot_keys) else 0
        p["due"] = 1 if (due_keys is not None and streak_key in due_keys) else 0
        p["dtd"] = 0
        if injuries is not None:
            status = injury_status(injuries.get(p["t"]), p["n"])
            if status == "out":
                REMOVED_OUT.append(f'{p["n"]} ({p["t"]})')
                continue
            if status == "dtd":
                p["dtd"] = 1
                FLAGGED_DTD.append(f'{p["n"]} ({p["t"]})')
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

    odds_map, totals, odds_names = get_market_data(games)
    print(f"Live odds for {len(odds_map)} players across {len(games)} games; totals for {len(totals)} games.")
    if not odds_map:
        print("No odds returned -- leaving existing index.html untouched."); return

    pp1_by_team = load_pp1()
    try:
        b2b_teams = get_teams_played_yesterday()
    except Exception as e:
        print(f"yesterday's schedule unavailable ({e}) -- 'Opp on B2B' filter left off.", file=sys.stderr)
        b2b_teams = None
    injuries = load_injuries()
    hot_keys, due_keys = load_streaks()
    players = refresh_players(pool, odds_map, opp_map, odds_names, pp1_by_team, b2b_teams, injuries, hot_keys, due_keys)
    print(f"Streaks: Hot {sum(p.get('hot', 0) for p in players)}: {', '.join(p['n'] for p in players if p.get('hot')) or 'none'} | Due {sum(p.get('due', 0) for p in players)}: {', '.join(p['n'] for p in players if p.get('due')) or 'none'}")
    if injuries is not None:
        print(f"Injuries: removed {len(REMOVED_OUT)} out/IR: {', '.join(REMOVED_OUT) or 'none'} | flagged {len(FLAGGED_DTD)} day-to-day: {', '.join(FLAGGED_DTD) or 'none'}")
    if RENAMES:
        print("Names corrected to the sportsbook's spelling:", "; ".join(f"{a} -> {b}" for a, b in RENAMES))
    print(f"PP1 flagged: {sum(p.get('pp1', 0) for p in players)} | opponents on a back-to-back: {sorted(b2b_teams) if b2b_teams is not None else 'n/a'}")
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
    dvp = "null"
    try:
        dj = json.load(open(os.path.join(here, "defense.json")))
        teams_d = dj.get("teams", {})
        ok = len(teams_d) == 32 and all(
            isinstance(v.get(p), (int, float)) for v in teams_d.values() for p in ("C", "LW", "RW", "D"))
        if ok:
            dvp = json.dumps(teams_d, separators=(",", ":"))
        else:
            print("defense.json failed validation -- Def vs. Position tab keeps its built-in table.", file=sys.stderr)
    except FileNotFoundError:
        print("defense.json not found -- Def vs. Position tab keeps its built-in table.", file=sys.stderr)
    except Exception as e:
        print(f"defense.json unreadable ({e}) -- Def vs. Position tab keeps its built-in table.", file=sys.stderr)
    out = template.replace("__DVP_JSON__", dvp).replace("__SEED_DATA_JSON__", json.dumps(seed, separators=(",", ":")))
    open(os.path.join(here, "index.html"), "w").write(out)
    print("Wrote index.html.")


if __name__ == "__main__":
    main()
