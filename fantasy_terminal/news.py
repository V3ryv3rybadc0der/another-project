"""
news.py - THE NEWS CATALOGUE
============================

This file answers: "when the user types NEWS ADD <player> <TYPE> ..., what
shocks hit the graph?"

Each news type is a small function that receives the graph, the target node,
a magnitude and a list of weeks and returns nothing - it calls graph.shock()
(or move_player / release_player) itself.  To ADD A NEW NEWS TYPE:

    1. write a function like the ones below
    2. register it in NEWS_TYPES at the bottom with a help string

The terminal's HELP NEWS command prints NEWS_TYPES automatically.

Magnitude conventions:
    * for OUT / RELEASED etc. the magnitude is ignored: the player's whole
      current value is removed
    * for HYPE / BUST / CUSTOM the magnitude is points per game
      (defaults to config STATS["DEFAULT_MAGNITUDE"])
    * weeks = [] means "the whole season"; weeks = [3] means week 3 only
"""

from typing import Callable, Dict, List, Optional

from .config import STATS
from .models import Node


def _weeks_or_season(weeks: List[int]) -> List[Optional[int]]:
    """[] -> [None] (season);  [3, 4] -> [3, 4]."""
    return list(weeks) if weeks else [None]


# ---------------------------------------------------------------------------
# Availability news
# ---------------------------------------------------------------------------
def news_out(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Player misses the given weeks (or the whole season): value -> 0."""
    for wk in _weeks_or_season(weeks):
        value = node.week_value(wk) if wk is not None else node.season_value()
        g.shock(node.id, -value, wk, label, event_id=eid)
    if not weeks:
        node.status = "OUT"


def news_questionable(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Player is questionable: remove QUESTIONABLE_HAIRCUT (25%) of his value."""
    frac = STATS["QUESTIONABLE_HAIRCUT"]
    for wk in _weeks_or_season(weeks):
        value = node.week_value(wk) if wk is not None else node.season_value()
        g.shock(node.id, -value * frac, wk, label, event_id=eid)


def news_doubtful(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Player is doubtful: remove DOUBTFUL_HAIRCUT (75%) of his value."""
    frac = STATS["DOUBTFUL_HAIRCUT"]
    for wk in _weeks_or_season(weeks):
        value = node.week_value(wk) if wk is not None else node.season_value()
        g.shock(node.id, -value * frac, wk, label, event_id=eid)


def news_returns(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Player comes back from injury: restore what was removed (value -> base)."""
    for wk in _weeks_or_season(weeks):
        current = node.week_value(wk) if wk is not None else node.season_value()
        g.shock(node.id, node.base_value - current, wk, label, event_id=eid)
    node.status = "ACTIVE"


def news_suspended(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Same as OUT for the given weeks (use --weeks 1-4)."""
    news_out(g, node, magnitude, weeks, label, eid)
    if not weeks:
        node.status = "SUSPENDED"


# ---------------------------------------------------------------------------
# Roster moves
# ---------------------------------------------------------------------------
def news_released(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Team cuts the player: his value leaves the team, depth chart shifts up."""
    g.release_player(node.id, label, event_id=eid)


def news_signed(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int,
                team: Optional[str] = None, depth: int = 1, value: Optional[float] = None, **_):
    """Player signs with / is traded to `team` at depth slot `depth`.

    NEWS ADD <player> SIGNED <TEAM> [--depth 1] [--value 14.5]
    --value sets his NEW projected points on the new team (optional).
    """
    if team is None:
        raise ValueError("SIGNED / TRADED needs a team, e.g. NEWS ADD 'Tee Higgins' SIGNED NYJ")
    g.move_player(node.id, team, depth, label, event_id=eid, new_value=value)


def news_role(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int,
              depth: Optional[int] = None, **_):
    """Depth-chart change on the same team: NEWS ADD <player> ROLE --depth 1
    (promoted to starter).  Everyone else in the group is renumbered."""
    if depth is None:
        raise ValueError("ROLE needs --depth N")
    value = node.season_value()
    g.shock(node.id, -value, None, label + " (old role)", include_source=False, event_id=eid)
    for other in g.players_at(node.team, node.pos):
        if other is not node and other.depth >= depth:
            other.depth += 1
    node.depth = depth
    g._shift_depth_chart(node.team, node.pos)
    g.rewire_team(node.team)
    if magnitude:
        g.shock(node.id, magnitude, None, label + " (own change)", event_id=eid)
    g.shock(node.id, node.season_value(), None, label + " (new role)", include_source=False, event_id=eid)


# ---------------------------------------------------------------------------
# Opinion / environment news
# ---------------------------------------------------------------------------
def news_hype(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Good news: +magnitude points (default from config)."""
    mag = abs(magnitude) if magnitude else STATS["DEFAULT_MAGNITUDE"]
    for wk in _weeks_or_season(weeks):
        g.shock(node.id, mag, wk, label, event_id=eid)


def news_bust(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Bad news: -magnitude points."""
    mag = abs(magnitude) if magnitude else STATS["DEFAULT_MAGNITUDE"]
    for wk in _weeks_or_season(weeks):
        g.shock(node.id, -mag, wk, label, event_id=eid)


def news_custom(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Raw shock: exactly `magnitude` points, sign included.  Works on ANY node
    (players, O-LINE units, OFFENSE hubs, DSTs)."""
    for wk in _weeks_or_season(weeks):
        g.shock(node.id, magnitude, wk, label, event_id=eid)


def news_weather(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Bad weather for a TEAM's game: target the team's OFFENSE hub, e.g.
    NEWS ADD 'BUF OFFENSE' WEATHER 3 --weeks 12   (-3 to the offense in wk 12)."""
    mag = abs(magnitude) if magnitude else STATS["DEFAULT_MAGNITUDE"]
    if not weeks:
        raise ValueError("WEATHER needs --weeks N (it is a single-game event)")
    for wk in weeks:
        g.shock(node.id, -mag, wk, label, event_id=eid)


def news_coach(g, node: Node, magnitude: float, weeks: List[int], label: str, eid: int, **_):
    """Coaching / scheme change for a TEAM: target the OFFENSE hub, signed magnitude.
    NEWS ADD 'CHI OFFENSE' COACH 2   (new OC expected to add ~2 to the offense)."""
    mag = magnitude if magnitude else STATS["DEFAULT_MAGNITUDE"]
    g.shock(node.id, mag, None, label, event_id=eid)


# ---------------------------------------------------------------------------
# REGISTRY - add new types here.  (function, help text, needs_team)
# ---------------------------------------------------------------------------
NewsFn = Callable[..., None]
NEWS_TYPES: Dict[str, tuple] = {
    "OUT":          (news_out,          "Misses the season or --weeks N.  Value -> 0, backups rise."),
    "INJURY":       (news_out,          "Alias of OUT."),
    "QUESTIONABLE": (news_questionable, "Loses 25% of value (season or --weeks)."),
    "DOUBTFUL":     (news_doubtful,     "Loses 75% of value (season or --weeks)."),
    "RETURNS":      (news_returns,      "Back from injury: value restored to base."),
    "SUSPENDED":    (news_suspended,    "Same as OUT; use --weeks 1-4 for a 4 game ban."),
    "RELEASED":     (news_released,     "Cut by the team.  Leaves the graph as a free agent."),
    "CUT":          (news_released,     "Alias of RELEASED."),
    "SIGNED":       (news_signed,       "Joins a team:  NEWS ADD <p> SIGNED <TEAM> [--depth 1] [--value 14]."),
    "TRADED":       (news_signed,       "Alias of SIGNED (moves from old team to new)."),
    "ROLE":         (news_role,         "Depth chart change on same team: ROLE --depth 1 [magnitude]."),
    "HYPE":         (news_hype,         "Positive shock of <magnitude> points (default 2)."),
    "BUST":         (news_bust,         "Negative shock of <magnitude> points (default 2)."),
    "CUSTOM":       (news_custom,       "Raw signed shock of <magnitude> points on ANY node."),
    "WEATHER":      (news_weather,      "Team OFFENSE hub loses <magnitude> in --weeks N."),
    "COACH":        (news_coach,        "Team OFFENSE hub +/- <magnitude> for the season."),
}
