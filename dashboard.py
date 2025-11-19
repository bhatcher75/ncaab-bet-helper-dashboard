import os
import re
from datetime import date, datetime
import requests
from flask import Flask, render_template_string, request
from zoneinfo import ZoneInfo

# ---------------- CONFIG ----------------

# NCAA API
NCAA_BASE_URL = "https://ncaa-api.henrygd.me"

# Odds API
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "a5712dc701f3c6ebb31db584979436b7")  # <-- PUT YOUR KEY HERE
ODDS_BASE_URL = "https://api.the-odds-api.com/v4/sports/basketball_ncaab"
ODDS_REGIONS = "us"
ODDS_MARKETS = "totals"   # full-game totals for the main /odds call

# Try books in this order: DK -> FD -> BetMGM -> others
BOOKMAKER_PRIORITY = [
    "draftkings",
    "fanduel",
    "betmgm",
    "betonlineag",
    "pointsbetus",
    "caesars",
    "betrivers",
    "unibet_us",
    "wynnbet",
    "barstool",
]

# Keywords that indicate a field-goal attempt (non-free-throw)
SHOT_KEYWORDS = [
    "jumper",
    "jump shot",
    "jumpshot",
    "layup",
    "dunk",
    "hook shot",
    "hookshot",
    "tip-in",
    "tip in",
    "putback",
    "2 pointer",
    "two pointer",
    "2pt",
    "three pointer",
    "3 pointer",
    "3pt",
    "three-point",
]

# Turnover phrase tuning
TURNOVER_POSITIVE_PHRASES = [
    "turnover by",
    "turnover on",
    "shot clock turnover",
    "team turnover",
    "lost ball turnover",
    "bad pass turnover",
    "traveling turnover",
    "offensive foul turnover",
    "backcourt turnover",
    "five second turnover",
    "three second turnover",
]

TURNOVER_IGNORE_PHRASES = [
    "points off turnovers",
    "turnover margin",
    "points via turnovers",
]

app = Flask(__name__)


# ---------------- NCAA HELPERS ----------------

def get_ncaab_scoreboard_for_today():
    """Fetch today's NCAA D1 men's basketball scoreboard (US Eastern time)."""
    # Use America/New_York so Render doesn't flip to 'tomorrow' at UTC midnight
    eastern_now = datetime.now(ZoneInfo("America/New_York"))
    d = eastern_now.date()  # get the date in Eastern time

    year, month, day = d.year, d.month, d.day
    url = f"{NCAA_BASE_URL}/scoreboard/basketball-men/d1/{year}/{month:02d}/{day:02d}/all-conf"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()



def get_game_play_by_play(game_url_path):
    if not game_url_path:
        return None
    full_url = NCAA_BASE_URL + game_url_path + "/play-by-play"
    try:
        resp = requests.get(full_url, timeout=10)
        resp.raise_for_status()
    except Exception:
        return None
    data = resp.json()
    if not isinstance(data, dict):
        return None
    return data


def compute_first_half_stats_from_pbp(pbp_data):
    """
    Compute:
      - FGA (non-FT field goal attempts)
      - FTA (free throw attempts)
      - TO (turnovers)
      - 1H scores
      - integer = FGA + (FTA / 2) + TO
    """
    periods = pbp_data.get("periods", [])
    if not periods:
        return None

    # Find the 1st half period (periodNumber == 1)
    first_half = None
    for p in periods:
        if str(p.get("periodNumber")) == "1":
            first_half = p
            break

    if first_half is None:
        return None

    plays = first_half.get("playbyplayStats", [])
    fga = 0
    fta = 0
    turnovers = 0
    last_home_score = 0
    last_visitor_score = 0

    for play in plays:
        desc = play.get("eventDescription") or ""
        if not desc:
            desc = play.get("visitorText") or play.get("homeText") or ""
        desc_lower = desc.lower()

        # Keep scores updated as we walk the plays
        last_home_score = play.get("homeScore", last_home_score)
        last_visitor_score = play.get("visitorScore", last_visitor_score)

        # ---------- FREE THROWS (FTA) ----------
        if "free throw" in desc_lower:
            if "both free throws" in desc_lower:
                fta += 2
            elif "all three free throws" in desc_lower or "all 3 free throws" in desc_lower:
                fta += 3
            elif "three free throws" in desc_lower or "3 free throws" in desc_lower:
                fta += 3
            elif "two free throws" in desc_lower or "2 free throws" in desc_lower:
                fta += 2
            else:
                fta += 1

        # ---------- FIELD GOAL ATTEMPTS (non-FT FGA) ----------
        has_shot_word = any(k in desc_lower for k in SHOT_KEYWORDS)
        if has_shot_word and "free throw" not in desc_lower:
            fga += 1

        # ---------- TURNOVERS (refined) ----------
        if "turnover" in desc_lower:
            # Ignore non-event commentary about turnovers
            if any(bad in desc_lower for bad in TURNOVER_IGNORE_PHRASES):
                continue

            # Count only if it looks like an actual event
            positive_match = any(p in desc_lower for p in TURNOVER_POSITIVE_PHRASES)

            # Fallback: a line starting with "turnover" is almost always an event
            starts_with_turnover = desc_lower.strip().startswith("turnover")

            if positive_match or starts_with_turnover:
                turnovers += 1

    integer_value = fga + (fta / 2.0) + turnovers

    return {
        "fga": fga,
        "fta": fta,
        "turnovers": turnovers,
        "home_pts_1h": last_home_score,
        "away_pts_1h": last_visitor_score,
        "integer": integer_value,
    }


# ---------------- ODDS API HELPERS ----------------

def normalize_team_name(name: str) -> str:
    """Normalize team names so NCAA vs Odds API names match more often."""
    if not name:
        return ""
    s = name.lower()
    s = s.replace("&", "and")
    # Remove punctuation
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    # Drop common noise words
    for word in ["university", "college", "state", "st", "st.", "the", "of"]:
        s = re.sub(rf"\b{word}\b", " ", s)
    # Collapse spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def teams_match_name(ncaa_name, odds_name):
    """Looser match: normalize, then check substring or ≥2 common tokens."""
    a = normalize_team_name(ncaa_name)
    b = normalize_team_name(odds_name)
    if not a or not b:
        return False

    # Direct substring either way
    if a in b or b in a:
        return True

    # Token overlap
    set_a = set(a.split())
    set_b = set(b.split())
    common = set_a & set_b
    return len(common) >= 2


def fetch_odds_games_today():
    """
    Fetch Odds API full-game totals for NCAAB and keep only events
    whose commence_time falls on 'today' in the local timezone.
    """
    today_local = date.today()

    url = f"{ODDS_BASE_URL}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_REGIONS,
        "markets": ODDS_MARKETS,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []

    filtered = []
    for event in data:
        ct = event.get("commence_time")
        if not ct:
            continue
        try:
            dt_utc = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            continue
        dt_local = dt_utc.astimezone()  # system local TZ
        if dt_local.date() == today_local:
            filtered.append(event)

    return filtered


def find_matching_odds_event(odds_games, ncaa_home_names, ncaa_away_names):
    """
    Try to match by:
      - Normal home/away,
      - Or swapped home/away (in case the book lists them reversed).
    ncaa_*_names should be a list: [short, full, char6, seo]
    """
    for event in odds_games:
        odds_home = event.get("home_team", "")
        odds_away = event.get("away_team", "")

        # Normal alignment
        home_ok = any(teams_match_name(n, odds_home) for n in ncaa_home_names if n)
        away_ok = any(teams_match_name(n, odds_away) for n in ncaa_away_names if n)

        if home_ok and away_ok:
            return event

        # Swapped alignment
        home_ok_swapped = any(teams_match_name(n, odds_away) for n in ncaa_home_names if n)
        away_ok_swapped = any(teams_match_name(n, odds_home) for n in ncaa_away_names if n)

        if home_ok_swapped and away_ok_swapped:
            return event

    return None


def extract_full_game_total_with_book(event):
    """
    Look through bookmakers in priority order
    and return (total_points, bookmaker_name) for the first one found.
    """
    bookmakers = event.get("bookmakers", [])
    bm_by_key = {bm.get("key"): bm for bm in bookmakers}

    for key in BOOKMAKER_PRIORITY:
        bm = bm_by_key.get(key)
        if not bm:
            continue
        for market in bm.get("markets", []):
            if market.get("key") == "totals":
                outcomes = market.get("outcomes", [])
                if not outcomes:
                    continue
                point = outcomes[0].get("point")
                if point is not None:
                    return float(point), bm.get("title") or key
    return None, None


# ---------------- BETTING LOGIC ----------------

def evaluate_bet(integer_value, home_pts_1h, away_pts_1h, derived_2h_line):
    """
    New GO / NO-GO rules:

      1) abs(integer - 2H line) < 11
      2) 1st half score diff >= 6
    """
    diff_line = abs(integer_value - derived_2h_line)
    score_diff = abs(home_pts_1h - away_pts_1h)

    # GO if both conditions are true
    qualifies = (diff_line >= 6) and (score_diff < 11)

    # Lean direction still based on integer vs 2H line
    if integer_value > derived_2h_line:
        lean = "OVER"
    elif integer_value < derived_2h_line:
        lean = "UNDER"
    else:
        lean = "NEUTRAL"

    return {
        "qualifies": qualifies,
        "diff_line": diff_line,
        "score_diff": score_diff,
        "lean": lean,
    }



# ---------------- DASHBOARD CORE ----------------

def build_dashboard_rows():
    rows = []

    # NCAA games
    try:
        scoreboard = get_ncaab_scoreboard_for_today()
        games = scoreboard.get("games", [])
    except Exception as e:
        return [], f"Error loading NCAA scoreboard: {e}"

    # Odds games
    try:
        odds_games = fetch_odds_games_today()
    except Exception as e:
        return [], f"Error loading Odds API data: {e}"

    for gwrap in games:
        g = gwrap.get("game", {})

        away = g.get("away", {})
        home = g.get("home", {})

        away_names = away.get("names", {}) or {}
        home_names = home.get("names", {}) or {}

        away_short = away_names.get("short") or "Away"
        away_full = away_names.get("full")
        away_char6 = away_names.get("char6")
        away_seo = away_names.get("seo")

        home_short = home_names.get("short") or "Home"
        home_full = home_names.get("full")
        home_char6 = home_names.get("char6")
        home_seo = home_names.get("seo")

        state = (g.get("gameState") or "").upper()
        period = (g.get("currentPeriod") or "").upper()
        game_path = g.get("url")

        # ---------- FILTER: ONLY SHOW 1ST HALF OR HALFTIME ----------
        period_clean = period.replace(".", "").strip()

        is_first_half = (
            "1" in period_clean and
            "FINAL" not in period_clean and
            "OT" not in period_clean and
            "HALF" not in period_clean
        )
        is_halftime = "HALF" in period_clean

        if not (is_first_half or is_halftime):
            continue

        # Defaults
        stats_1h = None
        full_game_total = None
        full_game_book = None
        derived_2h_line = None
        eval_result = None

        # 1H stats from NCAA if PBP exists
        if g.get("hasPbp", True):
            pbp = get_game_play_by_play(game_path)
            if pbp:
                stats_1h = compute_first_half_stats_from_pbp(pbp)

        # Match to Odds API game using more name variants
        odds_event = find_matching_odds_event(
            odds_games,
            [home_short, home_full, home_char6, home_seo],
            [away_short, away_full, away_char6, away_seo],
        )

        if odds_event:
            full_game_total, full_game_book = extract_full_game_total_with_book(odds_event)

        # Derive our own 2H line:
        if stats_1h and full_game_total is not None:
            half_points = stats_1h["home_pts_1h"] + stats_1h["away_pts_1h"]
            derived_2h_line = full_game_total - half_points

        # Evaluate bet if we have stats and a derived 2H line
        if stats_1h and derived_2h_line is not None:
            eval_result = evaluate_bet(
                stats_1h["integer"],
                stats_1h["home_pts_1h"],
                stats_1h["away_pts_1h"],
                derived_2h_line,
            )

        rows.append({
            "matchup": f"{away_short} @ {home_short}",
            "state": state,
            "period": period,
            "integer": stats_1h["integer"] if stats_1h else None,
            "fga": stats_1h["fga"] if stats_1h else None,
            "fta": stats_1h["fta"] if stats_1h else None,
            "to": stats_1h["turnovers"] if stats_1h else None,
            "half_score": (
                f"{stats_1h['away_pts_1h']}-{stats_1h['home_pts_1h']}"
                if stats_1h else None
            ),
            "full_game_total": full_game_total,
            "full_game_book": full_game_book,
            "derived_2h_line": derived_2h_line,
            "qualifies": eval_result["qualifies"] if eval_result else None,
            "lean": eval_result["lean"] if eval_result else None,
            "diff_line": eval_result["diff_line"] if eval_result else None,
            "score_diff": eval_result["score_diff"] if eval_result else None,
        })

    return rows, None


# ---------------- FLASK ROUTES & TEMPLATE ----------------

TEMPLATE = """
<!doctype html>
<html>
<head>
    <title>NCAAB 1H Integer Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; padding: 20px; }
        h1 { margin-bottom: 0; }
        .subtitle { color: #555; margin-top: 5px; margin-bottom: 20px; }
        table { border-collapse: collapse; width: 100%; margin-top: 10px; }
        th, td { border: 1px solid #ccc; padding: 6px 8px; font-size: 14px; text-align: center; }
        th { background: #f4f4f4; }
        .qual-yes { background: #d4f8d4; font-weight: bold; }
        .qual-no { background: #fbe4e4; }
        .btn-refresh {
            display: inline-block;
            padding: 6px 12px;
            margin-top: 10px;
            border-radius: 4px;
            border: 1px solid #888;
            background: #fafafa;
            cursor: pointer;
        }
        .error { color: red; margin-top: 10px; }
        .note { color: #666; font-size: 13px; margin-top: 10px; }
    </style>
</head>
<body>
    <h1>RVP 2nd Half Picks - Powered by BH </h1>
    <div class="subtitle">Date: {{ today }}</div>

    <form method="post">
        <button class="btn-refresh" type="submit">Refresh</button>
    </form>

    {% if error %}
        <div class="error">{{ error }}</div>
    {% endif %}

    <div class="note">
        Showing only games in 1st Half or at Halftime.<br>
        
    </div>

    <table>
        <tr>
            <th>Matchup</th>
            <th>State</th>
            <th>Period</th>
            <th>1H Score</th>
            <th>FGA</th>
            <th>FTA</th>
            <th>TO</th>
            <th>Integer<br>(FGA + FTA/2 + TO)</th>
            <th>Full-Game Total</th>
            <th>Book</th>
            <th>Derived 2H Line</th>
            <th>|Int - 2H Line|</th>
            <th>1H Score Diff</th>
            <th>Qualifies?</th>
            <th>Lean</th>
        </tr>
        {% for row in rows %}
        {% set qual_class = '' %}
        {% if row.qualifies is not none %}
            {% if row.qualifies %}
                {% set qual_class = 'qual-yes' %}
            {% else %}
                {% set qual_class = 'qual-no' %}
            {% endif %}
        {% endif %}
        <tr class="{{ qual_class }}">
            <td>{{ row.matchup }}</td>
            <td>{{ row.state }}</td>
            <td>{{ row.period }}</td>
            <td>{{ row.half_score or '-' }}</td>
            <td>{{ row.fga if row.fga is not none else '-' }}</td>
            <td>{{ row.fta if row.fta is not none else '-' }}</td>
            <td>{{ row.to if row.to is not none else '-' }}</td>
            <td>
                {% if row.integer is not none %}
                    {{ "%.1f" | format(row.integer) }}
                {% else %}
                    -
                {% endif %}
            </td>
            <td>{{ row.full_game_total if row.full_game_total is not none else '-' }}</td>
            <td>{{ row.full_game_book or '-' }}</td>
            <td>
                {% if row.derived_2h_line is not none %}
                    {{ "%.1f" | format(row.derived_2h_line) }}
                {% else %}
                    -
                {% endif %}
            </td>
            <td>
                {% if row.diff_line is not none %}
                    {{ "%.1f" | format(row.diff_line) }}
                {% else %}
                    -
                {% endif %}
            </td>
            <td>
                {% if row.score_diff is not none %}
                    {{ row.score_diff }}
                {% else %}
                    -
                {% endif %}
            </td>
            <td>
                {% if row.qualifies is none %}
                    -
                {% elif row.qualifies %}
                    YES
                {% else %}
                    NO
                {% endif %}
            </td>
            <td>{{ row.lean or '-' }}</td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""
@app.route("/test-odds")
def test_odds():
    """
    Simple check page for The Odds API.
    Go to /test-odds in your browser to see if it works.
    """
    try:
        games = fetch_odds_games_today()
        return f"OK - got {len(games)} events from The Odds API.", 200
    except Exception as e:
        return f"ERROR talking to The Odds API: {e}", 500
@app.route("/test-ncaa")
def test_ncaa():
    """
    Simple check page for the NCAA scoreboard.
    Go to /test-ncaa in your browser to see if it works.
    """
    try:
        scoreboard = get_ncaab_scoreboard_for_today()
        games = scoreboard.get("games", [])
        return f"OK - got {len(games)} games from the NCAA API.", 200
    except Exception as e:
        return f"ERROR talking to NCAA API: {e}", 500

@app.route("/", methods=["GET", "POST"])
def index():
    rows, error = build_dashboard_rows()
    return render_template_string(
        TEMPLATE,
        rows=rows,
        error=error,
        today=date.today().isoformat()
    )


if __name__ == "__main__":
    app.run(debug=True)
