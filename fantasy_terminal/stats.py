"""
stats.py - PROJECTIONS, STRENGTH OF SCHEDULE, MATCHUPS, RANKINGS
================================================================

Pure functions that read the graph and return numbers.  Nothing in here
changes the graph.

    league_averages(g)                 -> avg points allowed per position
    matchup_adjustment(g, node, week)  -> +/- points for this week's opponent
    projected_week(g, node, week)      -> full weekly projection (BYE -> None)
    projected_season(g, node)          -> season points-per-game projection
    strength_of_schedule(g, node)      -> (sos_score, rank, label)
    rankings(g, pos, week=None)        -> nodes sorted best-first
    movers(g, week=None)               -> nodes sorted by biggest change
"""

from typing import Dict, List, Optional, Tuple

from .config import STATS
from .models import Node

# Positions that have a "points allowed vs X" column in defenses.csv
MATCHUP_POSITIONS = ("QB", "RB", "WR", "TE", "K")


class DefenseProfile:
    """Points allowed per game by a team's defense to each position (last year)."""
    def __init__(self, team: str, vs: Dict[str, float]):
        self.team = team
        self.vs = vs   # {"QB": 17.2, "RB": 21.0, "WR": 29.5, "TE": 9.1, "K": 7.5}


def league_averages(g) -> Dict[str, float]:
    """Average points allowed to each position across all defenses."""
    profiles: Dict[str, DefenseProfile] = getattr(g, "defense_profiles", {})
    avg: Dict[str, float] = {}
    for pos in MATCHUP_POSITIONS:
        vals = [p.vs.get(pos, 0.0) for p in profiles.values() if pos in p.vs]
        avg[pos] = sum(vals) / len(vals) if vals else 0.0
    offs = [t.off_rating for t in g.teams.values()]
    avg["OFF"] = sum(offs) / len(offs) if offs else 0.0
    return avg


def opponent(g, node: Node, week: int) -> Optional[str]:
    """Who does this node's team play in `week`?  None on a bye / unknown."""
    team = g.teams.get(node.team)
    if team is None:
        return None
    return team.schedule.get(week)


def matchup_adjustment(g, node: Node, week: int) -> float:
    """How much this week's opponent moves the projection (points).

    Skill players: compare opponent's points-allowed-to-my-position with the
    league average.  DST: compare the opponent's offense rating with average.
    Scaled by config STATS["MATCHUP_SENSITIVITY"].
    """
    opp = opponent(g, node, week)
    if opp is None:
        return 0.0
    avg = league_averages(g)
    sens = STATS["MATCHUP_SENSITIVITY"]
    base = node.base_value + node.season_delta
    if node.pos in MATCHUP_POSITIONS:
        prof = g.defense_profiles.get(opp)
        if prof is None or avg.get(node.pos, 0) == 0:
            return 0.0
        ratio = prof.vs.get(node.pos, avg[node.pos]) / avg[node.pos] - 1.0
        return base * ratio * sens
    if node.pos == "DST":
        opp_team = g.teams.get(opp)
        if opp_team is None or avg["OFF"] == 0:
            return 0.0
        # a weak opposing offense (rating below average) is GOOD for a DST
        ratio = 1.0 - opp_team.off_rating / avg["OFF"]
        return base * ratio * sens
    return 0.0


def is_bye(g, node: Node, week: int) -> bool:
    team = g.teams.get(node.team)
    return team is not None and team.bye_week == week


def projected_week(g, node: Node, week: int) -> Optional[float]:
    """Weekly projection = base + season news + this week's news + matchup.
    Returns None on a bye week or for free agents."""
    if node.team not in g.teams or is_bye(g, node, week):
        return None
    value = node.week_value(week)
    if value <= 0.01:            # OUT / released this week: no matchup bonus for a player who is not playing
        return 0.0
    return value + matchup_adjustment(g, node, week)


def projected_season(g, node: Node) -> float:
    return node.season_value(int(STATS["GAMES_IN_SEASON"]))


def week_change(node: Node, week: int) -> float:
    """News-driven change for this week only (season change + week change)."""
    return node.season_delta + node.week_deltas.get(week, 0.0)


def strength_of_schedule(g, node: Node) -> Tuple[float, str]:
    """Average matchup difficulty over the whole schedule.

    Returns (score, label).  score is the average % of league-average points
    the opponents allow to this position:  +8.0 means opponents give up 8%
    MORE than average (easy schedule), -8.0 means harder.
    """
    team = g.teams.get(node.team)
    if team is None:
        return 0.0, "N/A"
    avg = league_averages(g)
    pcts: List[float] = []
    for week, opp in team.schedule.items():
        if node.pos in MATCHUP_POSITIONS:
            prof = g.defense_profiles.get(opp)
            if prof and avg.get(node.pos):
                pcts.append((prof.vs.get(node.pos, avg[node.pos]) / avg[node.pos] - 1.0) * 100)
        elif node.pos == "DST":
            ot = g.teams.get(opp)
            if ot and avg["OFF"]:
                pcts.append((1.0 - ot.off_rating / avg["OFF"]) * 100)
    if not pcts:
        return 0.0, "N/A"
    score = sum(pcts) / len(pcts)
    if score > 3:
        label = "EASY"
    elif score < -3:
        label = "HARD"
    else:
        label = "AVG"
    return score, label


def sos_table(g, pos: str) -> List[Tuple[str, float, str]]:
    """Strength of schedule for every team for one position, easiest first."""
    rows = []
    for abbr, team in g.teams.items():
        probe = Node(id="probe", name="probe", team=abbr, pos=pos)
        score, label = strength_of_schedule(g, probe)
        rows.append((abbr, score, label))
    rows.sort(key=lambda r: -r[1])
    return rows


def rankings(g, pos: Optional[str] = None, week: Optional[int] = None) -> List[Tuple[Node, float]]:
    """Scoring nodes sorted by projection, best first."""
    out = []
    for n in g.nodes.values():
        if not n.is_scoring or n.team not in g.teams:
            continue
        if pos and n.pos != pos:
            continue
        val = projected_week(g, n, week) if week else projected_season(g, n)
        if val is None:
            continue
        out.append((n, val))
    out.sort(key=lambda t: -t[1])
    return out


def movers(g, week: Optional[int] = None, pos: Optional[str] = None) -> List[Tuple[Node, float]]:
    """Nodes sorted by the size of their news-driven change (biggest first)."""
    out = []
    for n in g.nodes.values():
        if pos and n.pos != pos:
            continue
        if not pos and not n.is_scoring:
            continue
        chg = week_change(n, week) if week else (projected_season(g, n) - n.base_value)
        if abs(chg) < 0.005:
            continue
        out.append((n, chg))
    out.sort(key=lambda t: -abs(t[1]))
    return out
