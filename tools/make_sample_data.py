"""
tools/make_sample_data.py - GENERATE defenses.csv AND schedule.csv
==================================================================

Run:   python tools/make_sample_data.py

Reads data/teams.csv (for the list of teams and their `def_strength` 1-10)
and writes:

    data/defenses.csv   points allowed per game to each position (last year)
                        derived from def_strength with a little seeded noise
    data/schedule.csv   an 18-week sample schedule, 17 games + 1 bye per team

The schedule is a SAMPLE (a round-robin, not the real NFL formula).  Replace
data/schedule.csv with the real one when you have it - the format is just
week,home,away and the loader does not care where it came from.

The output is deterministic (fixed random seed) so re-running it gives the
same files.
"""

import csv
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")

# League-average points allowed per game to each position (PPR).  Every
# defense is scaled up or down from these based on def_strength.
LEAGUE_AVG = {"QB": 17.5, "RB": 22.0, "WR": 30.0, "TE": 9.5, "K": 8.0}


def make_defenses(teams, rng):
    rows = []
    for t in teams:
        strength = float(t["def_strength"])            # 1 (bad) .. 10 (elite)
        # 5.5 is average.  Each point of strength = 6% fewer points allowed.
        scale = 1.0 + (5.5 - strength) * 0.06
        row = {"team": t["abbr"]}
        for pos, avg in LEAGUE_AVG.items():
            noise = rng.uniform(-0.10, 0.10)           # defenses are not uniform vs every position
            row[f"vs_{pos.lower()}"] = round(avg * scale * (1 + noise), 1)
        row["dst_ppg"] = round(4.0 + strength * 0.7 + rng.uniform(-0.4, 0.4), 1)
        rows.append(row)
    return rows


def round_robin(abbrs):
    """Circle method: returns a list of rounds, each a list of (a, b) pairs."""
    teams = list(abbrs)
    n = len(teams)
    rounds = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = teams[i], teams[n - 1 - i]
            # alternate home/away so nobody is always home
            pairs.append((a, b) if (r + i) % 2 == 0 else (b, a))
        rounds.append(pairs)
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]   # rotate all but the first
    return rounds


def assign_byes(rounds, abbrs, bye_weeks=range(5, 15), max_per_week=2):
    """Pick one matchup per team (inside bye_weeks) to cancel so each team
    gets exactly one bye.  Both teams in a cancelled game are on bye."""
    remaining = set(abbrs)
    per_week = {w: 0 for w in bye_weeks}
    cancelled = set()

    def solve(remaining):
        if not remaining:
            return True
        team = sorted(remaining)[0]
        for w in bye_weeks:
            if per_week[w] >= max_per_week:
                continue
            for (a, b) in rounds[w - 1]:
                if team in (a, b) and a in remaining and b in remaining:
                    cancelled.add((w, a, b))
                    per_week[w] += 1
                    if solve(remaining - {a, b}):
                        return True
                    cancelled.discard((w, a, b))
                    per_week[w] -= 1
        return False

    if not solve(remaining):
        raise SystemExit("could not assign byes - try different bye_weeks")
    return cancelled


def make_schedule(abbrs, rng):
    shuffled = list(abbrs)
    rng.shuffle(shuffled)
    rounds = round_robin(shuffled)[:18]                # 18 weeks
    cancelled = assign_byes(rounds, abbrs)
    rows = []
    for w, pairs in enumerate(rounds, start=1):
        for (home, away) in pairs:
            if (w, home, away) in cancelled:
                continue
            rows.append({"week": w, "home": home, "away": away})
    return rows


def main():
    rng = random.Random(2026)
    with open(os.path.join(DATA, "teams.csv"), newline="") as f:
        teams = list(csv.DictReader(f))
    abbrs = [t["abbr"] for t in teams]

    defenses = make_defenses(teams, rng)
    with open(os.path.join(DATA, "defenses.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(defenses[0].keys()))
        w.writeheader()
        w.writerows(defenses)

    schedule = make_schedule(abbrs, rng)
    with open(os.path.join(DATA, "schedule.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["week", "home", "away"])
        w.writeheader()
        w.writerows(schedule)
    print(f"wrote {len(defenses)} defenses and {len(schedule)} games")


if __name__ == "__main__":
    sys.exit(main())
