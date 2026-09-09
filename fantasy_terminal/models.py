"""
models.py - THE DATA CLASSES
============================

These are plain containers.  They hold data and do a little bit of arithmetic
on themselves, nothing more.  The interesting logic lives in graph.py.

    Node          one thing on the board: a player, an O-line unit, a team
                  offense hub, or a team defense (DST)
    Edge          a weighted, directed connection between two Nodes
    Team          an NFL team: roster ids, schedule, bye, ratings
    Contribution  a record of one shock arriving at one node, with the path
                  it travelled - this is what EXPLAIN prints
    Action        one entry in the undo-able log: a news item, a custom link,
                  a weight change ...  SAVE/LOAD write these to JSON
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


# Positions that score fantasy points.  Anything else (OL, OLUNIT, OFF) is a
# "structural" node whose value is a quality grade rather than points.
SCORING_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")


@dataclass
class Node:
    """One row on the board.

    value semantics:
        QB/RB/WR/TE/K/DST  -> projected fantasy points per game (PPR)
        OL                 -> a lineman's quality grade, 0-10 (10 = elite)
        OLUNIT             -> the whole line's grade, 0-10
        OFF                -> team offense rating, roughly team PPG / 3
    """
    id: str                       # unique key, e.g. "KC_QB_PATRICK_MAHOMES"
    name: str                     # display name
    team: str                     # team abbreviation, or "FA" (free agent)
    pos: str                      # QB RB WR TE K DST OL OLUNIT OFF
    depth: int = 1                # depth chart slot (1 = starter)
    base_value: float = 0.0       # projection BEFORE any news
    last_year_ppg: float = 0.0    # last season points per game
    last_year_total: float = 0.0  # last season total points
    games_played: int = 0         # last season games
    note: str = ""                # free text shown on the PLAYER screen
    status: str = "ACTIVE"        # ACTIVE / OUT / RELEASED / SUSPENDED ...

    # --- everything below is filled in by the graph as news is applied ------
    season_delta: float = 0.0                         # change for the whole season
    week_deltas: Dict[int, float] = field(default_factory=dict)  # change for one week only
    contributions: List["Contribution"] = field(default_factory=list)

    # ---- helpers ----------------------------------------------------------
    def add_delta(self, delta: float, week: Optional[int]) -> None:
        """Record a change.  week=None means 'all season'."""
        if week is None:
            self.season_delta += delta
        else:
            self.week_deltas[week] = self.week_deltas.get(week, 0.0) + delta

    def season_value(self, games_in_season: int = 17) -> float:
        """Base + season-long change + the weekly changes averaged out."""
        weekly = sum(self.week_deltas.values()) / games_in_season if self.week_deltas else 0.0
        return self.base_value + self.season_delta + weekly

    def week_value(self, week: int) -> float:
        """Base + season-long change + this week's change (no matchup math)."""
        return self.base_value + self.season_delta + self.week_deltas.get(week, 0.0)

    def reset(self) -> None:
        """Wipe all news effects (used when the log is replayed)."""
        self.season_delta = 0.0
        self.week_deltas = {}
        self.contributions = []
        self.status = "ACTIVE"

    @property
    def slot(self) -> str:
        """Position + depth, e.g. 'WR1', 'RB2'.  Structural nodes have no number."""
        if self.pos in ("OLUNIT", "OFF", "DST"):
            return self.pos
        return f"{self.pos}{self.depth}"

    @property
    def is_scoring(self) -> bool:
        return self.pos in SCORING_POSITIONS


@dataclass
class Edge:
    """A directed connection:  src moves by X  ->  dst moves by weight * X."""
    src: str                      # Node.id of the thing that moved
    dst: str                      # Node.id of the thing that reacts
    weight: float                 # multiplier (negative = they compete)
    label: str = ""               # human text, e.g. "THROWS TO", "FACES WK 3"
    week: Optional[int] = None    # if set, the edge only matters in that week
    only_from_outside: bool = False  # see config: OFF_TO_* edges
    custom: bool = False          # True if added by the user with LINK

    @property
    def key(self):
        return (self.src, self.dst, self.week)


@dataclass
class Team:
    """An NFL team and its schedule."""
    abbr: str
    name: str
    division: str
    bye_week: int
    off_rating: float             # last year's offensive strength (~PPG / 3)
    ol_grade: float               # last year's O-line grade, 0-10
    schedule: Dict[int, str] = field(default_factory=dict)   # week -> opponent abbr
    home: Dict[int, bool] = field(default_factory=dict)      # week -> True if home game

    @property
    def off_node_id(self) -> str:
        return f"{self.abbr}_OFF"

    @property
    def dst_node_id(self) -> str:
        return f"{self.abbr}_DST"

    @property
    def ol_node_id(self) -> str:
        return f"{self.abbr}_OLUNIT"


@dataclass
class Contribution:
    """One shock arriving at one node.

    path        list of node ids from the news source to this node
    path_deltas the size of the shock at each step of that path
    delta       the amount this node moved (== path_deltas[-1])
    week        which week it applies to (None = season)
    """
    event_id: int
    event_label: str
    path: List[str]
    path_deltas: List[float]
    delta: float
    week: Optional[int]


@dataclass
class Action:
    """One entry in the undo-able log.

    kind    NEWS | LINK | UNLINK | WEIGHT
    params  everything needed to replay it, e.g.
            NEWS:   {"target": node_id, "type": "OUT", "magnitude": 0, "weeks": [3], "note": "...", "team": "KC"}
            LINK:   {"src": id, "dst": id, "weight": 0.3, "label": "...", "week": None}
            UNLINK: {"src": id, "dst": id}
            WEIGHT: {"key": "WR1_TO_QB1", "value": 0.6}
    """
    id: int
    kind: str
    params: dict
    stamp: str = ""               # ISO time string, for display only

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Action":
        return Action(id=d["id"], kind=d["kind"], params=d["params"], stamp=d.get("stamp", ""))
