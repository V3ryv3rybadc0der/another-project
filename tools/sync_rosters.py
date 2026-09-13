"""
tools/sync_rosters.py - UPDATE players.csv FROM THE LIVE LEAGUE
===============================================================

    python3 tools/sync_rosters.py --dry-run     show what would change
    python3 tools/sync_rosters.py               apply it
    python3 tools/sync_rosters.py --depth       also renumber depth charts

Players move.  The roster file that ships with this project was written at
one moment in time, so as trades and signings happen it drifts out of date -
and a player listed on the wrong team gets the wrong opponent every week,
which quietly poisons his matchup and his strength of schedule.

This reads the live league from the Sleeper API (free, no key) and rewrites
the `team` column in data/players.csv to match reality.

Safety rules, same as everywhere else in this project:
  * a player is matched on normalized name AND position, never name alone,
    so the Josh Allen who plays quarterback cannot inherit the linebacker's
    team
  * a name that matches two players at the same position is left alone and
    reported, rather than guessed at
  * a player Sleeper does not list is left alone (he may be retired, or
    simply someone you added by hand)
  * --dry-run prints the diff and writes nothing

`--depth` additionally renumbers each team's depth chart from Sleeper's
depth_chart_order.  That is more opinionated than the team fix, so it is
off by default.
"""

import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fantasy_terminal import feeds                    # noqa: E402
from fantasy_terminal.ingest import normalize_name    # noqa: E402

DATA = os.path.join(ROOT, "data")
SKILL = ("QB", "RB", "WR", "TE", "K")


def build_index(players: dict) -> dict:
    """(normalized name, position) -> [sleeper records]"""
    idx = {}
    for p in players.values():
        if not p.get("full_name") or not p.get("team"):
            continue
        if p.get("position") not in SKILL:
            continue
        idx.setdefault((normalize_name(p["full_name"]), p["position"]), []).append(p)
    return idx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    ap.add_argument("--depth", action="store_true", help="also renumber depth charts")
    ap.add_argument("--force", action="store_true", help="re-download rather than use the cache")
    args = ap.parse_args()

    path = os.path.join(DATA, "players.csv")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)

    with open(os.path.join(DATA, "teams.csv"), newline="", encoding="utf-8") as f:
        known = {r["abbr"].upper() for r in csv.DictReader(f)}

    try:
        players = feeds.fetch_sleeper_players(force=args.force)
    except feeds.FeedError as e:
        print(f"could not reach Sleeper: {e}", file=sys.stderr)
        return 1
    idx = build_index(players)

    moved, ambiguous, missing, depth_changes = [], [], [], []
    for row in rows:
        pos = row["pos"].strip().upper()
        if pos not in SKILL:
            continue                       # offensive linemen are not in Sleeper's skill set
        hits = idx.get((normalize_name(row["name"]), pos), [])
        if not hits:
            missing.append(row["name"])
            continue
        if len(hits) > 1:
            ambiguous.append(f'{row["name"]} ({pos}) matches {len(hits)} players')
            continue
        live = hits[0]
        new_team = (live.get("team") or "").upper()
        if new_team and new_team in known and new_team != row["team"].strip().upper():
            moved.append((row["name"], row["team"], new_team))
            row["team"] = new_team
        if args.depth:
            order = live.get("depth_chart_order")
            if order and str(order) != str(row.get("depth", "")).strip():
                depth_changes.append((row["name"], row.get("depth"), order))
                row["depth"] = str(order)

    print(f"{len(rows)} rows read")
    print(f"\nteam changes ({len(moved)}):")
    for name, old, new in moved:
        print(f"   {name:26s} {old:4s} -> {new}")
    if args.depth:
        print(f"\ndepth changes ({len(depth_changes)}):")
        for name, old, new in depth_changes[:20]:
            print(f"   {name:26s} {old} -> {new}")
    if ambiguous:
        print(f"\nleft alone, ambiguous ({len(ambiguous)}):")
        for a in ambiguous:
            print("   " + a)
    if missing:
        print(f"\nleft alone, not in Sleeper ({len(missing)}): {', '.join(missing[:12])}"
              + (" ..." if len(missing) > 12 else ""))

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0
    if not moved and not depth_changes:
        print("\nnothing to change")
        return 0

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
