"""
rosters.py - FANTASY ROSTERS (your team, your opponent, a trade partner)
========================================================================

Everything up to now has been about the whole NFL.  This file is about WHO
OWNS WHOM in your league, so the terminal can answer the questions that
actually matter on a Sunday:

    * what is my starting lineup worth this week?
    * how do I stack up against the guy I am playing?
    * if I trade Kelce for Gibbs, do I get better or worse?
    * that injury just now - did it hit my team or his?

A Roster is just a named list of players.  Call them what you like:
MYTEAM, OPPONENT, TRADE_TARGET, or a league-mate's name.

The important idea here is the LINEUP.  A roster total means nothing on its
own because you cannot start six running backs - you start a fixed set of
slots.  best_lineup() picks the highest-scoring legal lineup out of a
roster, which is what "my team is worth 118 points" actually means.

    Roster                 name + owner + the players on it
    best_lineup()          highest-scoring legal starting lineup
    roster_value()         that lineup's total for a week
    compare()              head to head between two rosters
    evaluate_trade()       what a proposed swap does to both sides
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from .config import LINEUP
from .models import Node
from . import stats as S


@dataclass
class Roster:
    """One fantasy team.

    name      short key you type, e.g. MYTEAM
    owner     free text, e.g. "me" or "Dave"
    player_ids  node ids, in the order you added them
    """
    name: str
    owner: str = ""
    player_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Roster":
        return Roster(name=d["name"], owner=d.get("owner", ""),
                      player_ids=list(d.get("player_ids", [])))

    def players(self, graph) -> List[Node]:
        """The Node objects, skipping any that no longer exist."""
        return [graph.nodes[i] for i in self.player_ids if i in graph.nodes]


# ---------------------------------------------------------------------------
# LINEUP CONSTRUCTION
# ---------------------------------------------------------------------------
def _slot_order() -> List[str]:
    """Expand the LINEUP config into a list of slots to fill, e.g.
    ['QB','RB','RB','WR','WR','WR','TE','FLEX','K','DST'].

    Fixed positions are filled before FLEX, because a FLEX slot should get
    the best player LEFT OVER, not steal a starter from a fixed slot.
    """
    slots = []
    for pos, count in LINEUP["SLOTS"].items():
        if pos == "FLEX":
            continue
        slots.extend([pos] * int(count))
    slots.extend(["FLEX"] * int(LINEUP["SLOTS"].get("FLEX", 0)))
    return slots


def best_lineup(graph, roster: Roster, week: Optional[int] = None
                ) -> Tuple[List[Tuple[str, Node, float]], List[Tuple[Node, float]], float]:
    """Pick the highest-scoring legal lineup from a roster.

    Returns (starters, bench, total) where
        starters = [(slot, node, points), ...]
        bench    = [(node, points), ...] sorted best first
        total    = sum of the starters' points

    A player on a bye (or with no game that week) scores 0 and will not be
    started unless there is nobody else for the slot.
    """
    scored: List[Tuple[Node, float]] = []
    for n in roster.players(graph):
        if week:
            val = S.projected_week(graph, n, week)
            val = 0.0 if val is None else val          # bye week
        else:
            val = S.projected_season(graph, n)
        scored.append((n, val))
    scored.sort(key=lambda t: -t[1])

    used: set = set()
    starters: List[Tuple[str, Node, float]] = []
    for slot in _slot_order():
        eligible = (LINEUP["FLEX_POSITIONS"] if slot == "FLEX" else (slot,))
        pick = next(((n, v) for n, v in scored
                     if n.id not in used and n.pos in eligible), None)
        if pick is None:
            starters.append((slot, None, 0.0))          # empty slot
            continue
        used.add(pick[0].id)
        starters.append((slot, pick[0], pick[1]))
    bench = [(n, v) for n, v in scored if n.id not in used]
    total = sum(v for _, n, v in starters if n is not None)
    return starters, bench, total


def roster_value(graph, roster: Roster, week: Optional[int] = None) -> float:
    """Just the starting lineup total."""
    return best_lineup(graph, roster, week)[2]


def roster_change(graph, roster: Roster, week: Optional[int] = None) -> float:
    """How much NEWS has moved this roster's starters.

    Deliberately excludes the matchup adjustment: that is not news, it is
    just who they happen to be playing. This answers "did today's headlines
    help or hurt my team".
    """
    starters, _bench, _total = best_lineup(graph, roster, week)
    out = 0.0
    for _slot, n, _v in starters:
        if n is None:
            continue
        out += S.week_change(n, week) if week else (S.projected_season(graph, n) - n.base_value)
    return out


# ---------------------------------------------------------------------------
# HEAD TO HEAD
# ---------------------------------------------------------------------------
def compare(graph, a: Roster, b: Roster, week: Optional[int] = None) -> dict:
    """Head-to-head between two rosters for a week.

    Returns a dict with each side's lineup, total, and the margin, plus a
    per-slot breakdown so you can see where the matchup is won and lost.
    """
    a_start, a_bench, a_total = best_lineup(graph, a, week)
    b_start, b_bench, b_total = best_lineup(graph, b, week)
    rows = []
    for (slot, an, av), (_slot, bn, bv) in zip(a_start, b_start):
        rows.append({"slot": slot, "a": an, "a_pts": av, "b": bn, "b_pts": bv,
                     "edge": av - bv})
    return {"a": a, "b": b, "a_total": a_total, "b_total": b_total,
            "margin": a_total - b_total, "rows": rows,
            "a_bench": a_bench, "b_bench": b_bench}


# ---------------------------------------------------------------------------
# TRADES
# ---------------------------------------------------------------------------
def evaluate_trade(graph, a: Roster, a_sends: List[Node],
                   b: Roster, b_sends: List[Node], week: Optional[int] = None) -> dict:
    """What a proposed swap does to BOTH sides' starting lineups.

    Nothing is actually moved - this builds throwaway copies of the two
    rosters and prices them, so you can look before you leap.

    Returns before/after totals and the delta for each side.
    """
    a_ids = [i for i in a.player_ids if i not in {n.id for n in a_sends}] + [n.id for n in b_sends]
    b_ids = [i for i in b.player_ids if i not in {n.id for n in b_sends}] + [n.id for n in a_sends]
    a_after = Roster(name=a.name, owner=a.owner, player_ids=a_ids)
    b_after = Roster(name=b.name, owner=b.owner, player_ids=b_ids)

    a_before_v = roster_value(graph, a, week)
    b_before_v = roster_value(graph, b, week)
    a_after_v = roster_value(graph, a_after, week)
    b_after_v = roster_value(graph, b_after, week)
    return {
        "a": a, "b": b, "a_sends": a_sends, "b_sends": b_sends,
        "a_before": a_before_v, "a_after": a_after_v, "a_delta": a_after_v - a_before_v,
        "b_before": b_before_v, "b_after": b_after_v, "b_delta": b_after_v - b_before_v,
        "a_lineup_after": best_lineup(graph, a_after, week),
        "b_lineup_after": best_lineup(graph, b_after, week),
    }


# ---------------------------------------------------------------------------
# WHO OWNS THIS PLAYER
# ---------------------------------------------------------------------------
def owners_of(rosters: Dict[str, Roster], node_id: str) -> List[str]:
    """Every roster holding this player - used to tag the movers board so
    you can see at a glance whether news helped you or your opponent."""
    return [r.name for r in rosters.values() if node_id in r.player_ids]
