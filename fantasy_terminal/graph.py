"""
graph.py - THE IMPACT GRAPH
===========================

This is the engine.  It holds all Nodes and Edges, knows how to WIRE a team
(create all the standard connections from a roster + schedule) and how to
PROPAGATE a shock through the connections.

Key methods (what you would call from other code):

    g = ImpactGraph()
    g.add_node(node) / g.add_team(team)
    g.wire_all()                      # build every standard edge from the rosters
    g.rewire_team("KC")               # rebuild one team's edges after a roster move
    g.add_custom_edge(src, dst, w)    # user-defined connection (LINK command)
    contribs = g.propagate(node_id, delta, week)   # compute ripples (no side effects)
    g.apply(contribs)                 # write those ripples onto the nodes
    g.shock(node_id, delta, week, ...)             # propagate + apply in one go
    g.find(text)                      # fuzzy lookup of a node by name
    g.move_player(node_id, team, depth)            # trade / sign
    g.release_player(node_id)
"""

from collections import deque, defaultdict
import difflib
from typing import Dict, List, Optional, Tuple

from .config import WEIGHTS, PROPAGATION
from .models import Node, Edge, Team, Contribution


class ImpactGraph:
    def __init__(self, weights: Optional[dict] = None, propagation: Optional[dict] = None):
        # Copies so the terminal can change values at runtime without touching config.py
        self.weights: Dict[str, float] = dict(weights or WEIGHTS)
        self.propagation: Dict[str, float] = dict(propagation or PROPAGATION)

        self.nodes: Dict[str, Node] = {}
        self.teams: Dict[str, Team] = {}
        # Edges are stored twice for quick lookup in both directions.
        self.out_edges: Dict[str, List[Edge]] = defaultdict(list)
        self.in_edges: Dict[str, List[Edge]] = defaultdict(list)
        self.next_event_id = 1

    # ======================================================================
    # NODES / TEAMS
    # ======================================================================
    def add_node(self, node: Node) -> Node:
        self.nodes[node.id] = node
        return node

    def add_team(self, team: Team) -> Team:
        self.teams[team.abbr] = team
        # Every team automatically gets an OFFENSE hub node and an O-LINE unit
        # node so that shocks have somewhere to flow through.
        if team.off_node_id not in self.nodes:
            self.add_node(Node(id=team.off_node_id, name=f"{team.abbr} OFFENSE", team=team.abbr,
                               pos="OFF", base_value=team.off_rating,
                               note="Team offense hub. Value ~ team points per game / 3."))
        if team.ol_node_id not in self.nodes:
            self.add_node(Node(id=team.ol_node_id, name=f"{team.abbr} O-LINE", team=team.abbr,
                               pos="OLUNIT", base_value=team.ol_grade,
                               note="Whole offensive line grade 0-10."))
        return team

    def roster(self, abbr: str) -> List[Node]:
        """All nodes on a team, sorted by position then depth."""
        order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "OL": 5, "OLUNIT": 6, "OFF": 7, "DST": 8}
        return sorted((n for n in self.nodes.values() if n.team == abbr),
                      key=lambda n: (order.get(n.pos, 9), n.depth, n.name))

    def players_at(self, abbr: str, pos: str) -> List[Node]:
        """Nodes of one position on one team, starter first."""
        return sorted((n for n in self.nodes.values() if n.team == abbr and n.pos == pos),
                      key=lambda n: n.depth)

    def find(self, text: str, pos: Optional[str] = None) -> List[Node]:
        """Fuzzy name lookup.  Returns a list of candidate nodes (best first).

        Matches, in order:  exact id, exact name, name contains text,
        then difflib close matches.  Case-insensitive.
        """
        t = text.strip().upper().replace("_", " ")
        if not t:
            return []
        pool = [n for n in self.nodes.values() if pos is None or n.pos == pos]
        if text.strip().upper() in self.nodes:
            return [self.nodes[text.strip().upper()]]
        exact = [n for n in pool if n.name.upper() == t]
        if exact:
            return exact
        contains = [n for n in pool if t in n.name.upper()]
        if contains:
            return sorted(contains, key=lambda n: (len(n.name), n.name))
        # last-name style match: every word of the query appears in the name
        words = t.split()
        wordy = [n for n in pool if all(w in n.name.upper() for w in words)]
        if wordy:
            return wordy
        names = {n.name.upper(): n for n in pool}
        close = difflib.get_close_matches(t, list(names.keys()), n=5, cutoff=0.6)
        return [names[c] for c in close]

    # ======================================================================
    # EDGES
    # ======================================================================
    def _add_edge(self, edge: Edge) -> Edge:
        # Replace an existing edge with the same (src, dst, week) rather than duplicate it.
        self._remove_edge(edge.src, edge.dst, edge.week)
        self.out_edges[edge.src].append(edge)
        self.in_edges[edge.dst].append(edge)
        return edge

    def _remove_edge(self, src: str, dst: str, week: Optional[int] = None) -> bool:
        before = len(self.out_edges[src])
        self.out_edges[src] = [e for e in self.out_edges[src] if not (e.dst == dst and e.week == week)]
        self.in_edges[dst] = [e for e in self.in_edges[dst] if not (e.src == src and e.week == week)]
        return len(self.out_edges[src]) != before

    def add_custom_edge(self, src: str, dst: str, weight: float, label: str = "CUSTOM",
                        week: Optional[int] = None) -> Edge:
        """LINK command: a user-defined connection that survives rewiring."""
        return self._add_edge(Edge(src=src, dst=dst, weight=weight, label=label, week=week, custom=True))

    def remove_custom_edge(self, src: str, dst: str, week: Optional[int] = None) -> bool:
        return self._remove_edge(src, dst, week)

    def w(self, key: str, default: float = 0.0) -> float:
        """Look up a weight by key, e.g. w('WR1_TO_QB1')."""
        return float(self.weights.get(key, default))

    def _link(self, src: Node, dst: Node, key: str, label: str, week: Optional[int] = None,
              only_from_outside: bool = False) -> None:
        """Create a standard edge if the weight key exists and is non-zero."""
        weight = self.w(key)
        if weight == 0.0 or src.id == dst.id:
            return
        self._add_edge(Edge(src=src.id, dst=dst.id, weight=weight, label=label,
                            week=week, only_from_outside=only_from_outside))

    # ----------------------------------------------------------------------
    # AUTO-WIRING: turn rosters + schedule into edges
    # ----------------------------------------------------------------------
    def wire_all(self) -> None:
        for abbr in self.teams:
            self.rewire_team(abbr)

    def unwire_team(self, abbr: str) -> None:
        """Remove every NON-custom edge that starts or ends on this team."""
        ids = {n.id for n in self.nodes.values() if n.team == abbr}
        for nid in list(ids):
            self.out_edges[nid] = [e for e in self.out_edges[nid] if e.custom]
            self.in_edges[nid] = [e for e in self.in_edges[nid] if e.custom]
        # Also drop edges from other teams' nodes that point INTO this team's nodes
        # (e.g. other offenses -> our DST) so rewire can rebuild them cleanly.
        for nid, edges in self.out_edges.items():
            if nid not in ids:
                self.out_edges[nid] = [e for e in edges if e.custom or e.dst not in ids]
        for nid, edges in self.in_edges.items():
            if nid not in ids:
                self.in_edges[nid] = [e for e in edges if e.custom or e.src not in ids]

    def rewire_team(self, abbr: str) -> None:
        """Build all the standard connections for one team.

        This is the "who affects whom" rulebook.  Each block below creates one
        family of edges using the weight keys in config.WEIGHTS.
        """
        team = self.teams[abbr]
        self.unwire_team(abbr)

        off = self.nodes[team.off_node_id]
        olu = self.nodes[team.ol_node_id]
        dst = self.nodes.get(team.dst_node_id)
        qbs = self.players_at(abbr, "QB")
        rbs = self.players_at(abbr, "RB")
        wrs = self.players_at(abbr, "WR")
        tes = self.players_at(abbr, "TE")
        ks = self.players_at(abbr, "K")
        ols = self.players_at(abbr, "OL")
        catchers = wrs + tes
        skill = qbs + rbs + wrs + tes + ks

        # 1) QB <-> pass catchers / backs / kicker  (both directions, different weights)
        for qb in qbs[:1]:                       # only the starter drives the passing game
            for p in catchers + rbs + ks:
                self._link(qb, p, f"{qb.slot}_TO_{p.slot}", "THROWS TO" if p.pos != "K" else "SETS UP")
                self._link(p, qb, f"{p.slot}_TO_{qb.slot}", "TARGET OF" if p.pos != "K" else "")
            # QB1 -> QB2 (backup inherits when starter goes down)
            for backup in qbs[1:]:
                self._link(qb, backup, f"QB1_TO_{backup.slot}", "BACKUP")

        # 2) Competition for touches inside a position group
        for group in (rbs, wrs, tes):
            for a in group:
                for b in group:
                    if a is not b:
                        self._link(a, b, f"{a.slot}_TO_{b.slot}", "SHARES TOUCHES")
        # WR <-> TE target competition
        for wr in wrs:
            for te in tes:
                self._link(wr, te, f"{wr.slot}_TO_{te.slot}", "SHARES TARGETS")
                self._link(te, wr, f"{te.slot}_TO_{wr.slot}", "SHARES TARGETS")

        # 3) Offensive line:  linemen -> line unit -> QB / RBs / WR1
        for ol in ols:
            self._link(ol, olu, f"{ol.slot}_TO_OLUNIT", "ANCHORS")
        for qb in qbs[:1]:
            self._link(olu, qb, f"OLUNIT_TO_{qb.slot}", "PROTECTS")
        for rb in rbs:
            self._link(olu, rb, f"OLUNIT_TO_{rb.slot}", "RUN BLOCKS FOR")
        for wr in wrs[:1]:
            self._link(olu, wr, f"OLUNIT_TO_{wr.slot}", "BUYS TIME FOR")

        # 4) Everyone -> team OFFENSE hub, and hub -> everyone (outside shocks only)
        for p in skill:
            self._link(p, off, f"{p.slot}_TO_OFF", "DRIVES")
            self._link(off, p, f"OFF_TO_{p.slot}", "LIFTS", only_from_outside=True)
        self._link(olu, off, "OLUNIT_TO_OFF", "DRIVES")

        # 5) Own defense benefits a little from its own offense
        if dst is not None:
            self._link(off, dst, "OFF_TO_OWN_DST", "GIVES LEADS TO")

        # 6) Schedule: our offense <-> the defense we face each week (week-tagged edges)
        for week, opp in team.schedule.items():
            opp_team = self.teams.get(opp)
            if opp_team is None:
                continue
            opp_dst = self.nodes.get(opp_team.dst_node_id)
            opp_off = self.nodes.get(opp_team.off_node_id)
            if opp_dst is not None:
                self._link(off, opp_dst, "OFF_TO_OPP_DST", f"FACES WK {week}", week=week)
            if dst is not None and opp_off is not None:
                self._link(dst, opp_off, "DST_TO_OPP_OFF", f"FACES WK {week}", week=week)
                # the opponent's offense also faces OUR defense that week
                self._link(opp_off, dst, "OFF_TO_OPP_DST", f"FACES WK {week}", week=week)
            if opp_dst is not None and dst is not None:
                self._link(opp_dst, off, "DST_TO_OPP_OFF", f"FACES WK {week}", week=week)

    # ======================================================================
    # PROPAGATION - the heart of the terminal
    # ======================================================================
    def propagate(self, source_id: str, delta: float, week: Optional[int] = None,
                  event_id: int = 0, event_label: str = "", include_source: bool = True
                  ) -> List[Contribution]:
        """Compute how a shock at `source_id` ripples through the graph.

        Returns a list of Contribution objects and does NOT change any node.
        Call apply() to write them, or use shock() which does both.

        Rules (all tunable in config.PROPAGATION):
          * breadth-first walk along out-going edges
          * each hop multiplies the shock by the edge weight (and DAMPING)
          * stop when the shock is smaller than MIN_DELTA or deeper than MAX_DEPTH
          * never revisit a node already on the current path (no loops)
          * week-tagged edges: a season-wide shock crossing a "FACES WK 3" edge
            becomes a week-3 shock; a week-5 shock cannot cross a week-3 edge
          * OFF -> player edges are skipped when the shock started on that same
            team (see config comment on OFF_TO_*)
        """
        max_depth = int(self.propagation["MAX_DEPTH"])
        min_delta = float(self.propagation["MIN_DELTA"])
        damping = float(self.propagation["DAMPING"])
        origin = self.nodes[source_id]
        contributions: List[Contribution] = []

        # queue items: (node_id, delta_here, week_context, path_ids, path_deltas)
        queue = deque([(source_id, delta, week, [source_id], [delta])])
        while queue:
            nid, d, wk, path, pdeltas = queue.popleft()
            if include_source or len(path) > 1:
                contributions.append(Contribution(event_id=event_id, event_label=event_label,
                                                  path=list(path), path_deltas=list(pdeltas),
                                                  delta=d, week=wk))
            if len(path) - 1 >= max_depth:
                continue
            for e in self.out_edges.get(nid, []):
                if e.dst in path:                       # no cycles
                    continue
                target = self.nodes.get(e.dst)
                if target is None:
                    continue
                # week logic
                if e.week is not None:
                    if wk is None:
                        new_wk = e.week                 # season shock becomes a weekly one
                    elif wk == e.week:
                        new_wk = wk
                    else:
                        continue                        # different week: irrelevant
                else:
                    new_wk = wk
                # OFF -> own players only for shocks that came from outside the team
                if e.only_from_outside and target.team == origin.team:
                    continue
                nd = d * e.weight * damping
                if abs(nd) < min_delta:
                    continue
                queue.append((e.dst, nd, new_wk, path + [e.dst], pdeltas + [nd]))
        return contributions

    def apply(self, contributions: List[Contribution]) -> None:
        """Write a list of contributions onto the nodes."""
        for c in contributions:
            node = self.nodes[c.path[-1]]
            node.add_delta(c.delta, c.week)
            node.contributions.append(c)

    def shock(self, source_id: str, delta: float, week: Optional[int] = None,
              label: str = "", include_source: bool = True, event_id: Optional[int] = None
              ) -> List[Contribution]:
        """propagate() + apply() in one call.  Returns what was applied."""
        if event_id is None:
            event_id = self.next_event_id
            self.next_event_id += 1
        contribs = self.propagate(source_id, delta, week, event_id, label, include_source)
        self.apply(contribs)
        return contribs

    # ======================================================================
    # ROSTER MOVES (used by the news types SIGN / TRADE / RELEASE)
    # ======================================================================
    def _shift_depth_chart(self, abbr: str, pos: str) -> None:
        """Renumber depth so a team's position group is 1, 2, 3 ... with no gaps."""
        for i, n in enumerate(self.players_at(abbr, pos), start=1):
            n.depth = i

    def release_player(self, node_id: str, label: str = "", event_id: Optional[int] = None,
                       week: Optional[int] = None) -> List[Contribution]:
        """Player leaves the team: his value is removed from the team's graph.

        Order matters: propagate FIRST (while the edges still exist), then
        unwire him and renumber the depth chart behind him.
        """
        n = self.nodes[node_id]
        value = n.week_value(week) if week is not None else n.season_value(int(17))
        contribs = self.shock(node_id, -value, week, label, include_source=True, event_id=event_id)
        old_team = n.team
        if old_team in self.teams:
            n.team = "FA"
            n.status = "RELEASED"
            self._shift_depth_chart(old_team, n.pos)
            self.rewire_team(old_team)
            # a free agent keeps only custom edges
            self.out_edges[node_id] = [e for e in self.out_edges[node_id] if e.custom]
            self.in_edges[node_id] = [e for e in self.in_edges[node_id] if e.custom]
        return contribs

    def move_player(self, node_id: str, new_team: str, depth: int = 1, label: str = "",
                    event_id: Optional[int] = None, new_value: Optional[float] = None
                    ) -> List[Contribution]:
        """Trade / signing: move a player to a new team at a depth slot.

        1. the OLD team loses his value (shock -value, not counting himself)
        2. he is re-wired into the NEW team at `depth`; everyone below slides down
        3. the NEW team gains his value (shock +value, not counting himself)
        4. optionally his own projection is changed to `new_value`
        """
        n = self.nodes[node_id]
        contribs: List[Contribution] = []
        value = n.season_value()
        old_team = n.team
        if old_team in self.teams:
            contribs += self.shock(node_id, -value, None, label + " (leaves)", include_source=False, event_id=event_id)
            n.team = "FA"
            self._shift_depth_chart(old_team, n.pos)
            self.rewire_team(old_team)
        # slide the new team's depth chart down to make room
        for other in self.players_at(new_team, n.pos):
            if other.depth >= depth:
                other.depth += 1
        n.team = new_team
        n.depth = depth
        n.status = "ACTIVE"
        self._shift_depth_chart(new_team, n.pos)
        self.rewire_team(new_team)
        if new_value is not None:
            # his own projection changes (better/worse situation)
            own_delta = new_value - n.season_value()
            contribs += self.shock(node_id, own_delta, None, label + " (new role)", include_source=True, event_id=event_id)
            value = n.season_value()
        contribs += self.shock(node_id, value, None, label + " (joins)", include_source=False, event_id=event_id)
        return contribs

    # ======================================================================
    # RESET
    # ======================================================================
    def reset_deltas(self) -> None:
        for n in self.nodes.values():
            n.reset()
        self.next_event_id = 1
