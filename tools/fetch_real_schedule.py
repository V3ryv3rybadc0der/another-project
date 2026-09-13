"""
tools/fetch_real_schedule.py - REPLACE THE SAMPLE SCHEDULE WITH THE REAL ONE
============================================================================

    python3 tools/fetch_real_schedule.py              this season
    python3 tools/fetch_real_schedule.py --year 2026  a specific season
    python3 tools/fetch_real_schedule.py --dry-run    show, do not write

The schedule that ships with this project is a generated round robin, which
means every opponent, every strength-of-schedule number and every weekly
matchup is made up.  This pulls the REAL NFL schedule from ESPN's public
JSON and rewrites data/schedule.csv with it.

It also rewrites the bye weeks, because ESPN reports which teams are off in
each week rather than leaving us to infer it.

Run it once at the start of a season.  The schedule does not change after
that, so there is no need to run it again unless a game gets moved.

ESPN's abbreviations differ from ours in a couple of places (they write WSH
for Washington), so ALIASES below maps them onto the codes in teams.csv.
Add a line there if a future season introduces another mismatch.
"""

import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fantasy_terminal.feeds import FeedError, _get   # noqa: E402

DATA = os.path.join(ROOT, "data")
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

# ESPN's code -> the code used in data/teams.csv
ALIASES = {
    "WSH": "WAS",
    "LA": "LAR",
    "JAC": "JAX",
}

WEEKS = 18


def our_code(abbr: str) -> str:
    return ALIASES.get(abbr.upper(), abbr.upper())


def fetch_week(week: int, year: int) -> tuple:
    """Return (games, bye_teams) for one week of the regular season."""
    url = f"{SCOREBOARD}?seasontype=2&week={week}&dates={year}"
    data = json.loads(_get(url, timeout=30))
    games = []
    for e in data.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in sides or "away" not in sides:
            continue
        games.append({
            "week": week,
            "home": our_code(sides["home"]["team"]["abbreviation"]),
            "away": our_code(sides["away"]["team"]["abbreviation"]),
            "date": (e.get("date") or "")[:10],
        })
    byes = [our_code(t["abbreviation"])
            for t in (data.get("week", {}).get("teamsOnBye") or [])]
    return games, byes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=None, help="season year, e.g. 2026")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = ap.parse_args()

    # Work out the season from ESPN itself if the user did not say.
    year = args.year
    if year is None:
        try:
            state = json.loads(_get("https://api.sleeper.app/v1/state/nfl", timeout=20))
            year = int(state.get("season"))
        except (FeedError, ValueError, TypeError):
            ap.error("could not work out the season - pass --year")

    with open(os.path.join(DATA, "teams.csv"), newline="", encoding="utf-8") as f:
        known = {r["abbr"].upper() for r in csv.DictReader(f)}

    rows, byes, problems = [], {}, []
    for week in range(1, WEEKS + 1):
        try:
            games, week_byes = fetch_week(week, year)
        except FeedError as e:
            problems.append(f"week {week}: {e}")
            continue
        for g in games:
            missing = [c for c in (g["home"], g["away"]) if c not in known]
            if missing:
                problems.append(f"week {week}: unknown team code {missing} - add it to ALIASES")
                continue
            rows.append(g)
        for t in week_byes:
            byes.setdefault(t, week)
        print(f"  week {week:2d}: {len(games):2d} games, {len(week_byes)} on bye")

    if not rows:
        print("no games fetched - nothing written", file=sys.stderr)
        return 1

    # Sanity check before we overwrite anything: every team should play 17.
    played = {}
    for g in rows:
        played[g["home"]] = played.get(g["home"], 0) + 1
        played[g["away"]] = played.get(g["away"], 0) + 1
    odd = {t: n for t, n in played.items() if n != WEEKS - 1}
    print(f"\n{len(rows)} games, {len(played)} teams, {len(byes)} byes recorded")
    if odd:
        print(f"WARNING: these teams do not play {WEEKS - 1} games: {odd}")
    for p in problems:
        print("WARNING:", p)

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    path = os.path.join(DATA, "schedule.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["week", "home", "away", "date"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
