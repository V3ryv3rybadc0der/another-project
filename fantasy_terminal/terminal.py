"""
terminal.py - THE COMMAND LINE  (FFT> prompt)
=============================================

This file turns typed commands into screens.  Each command is a method named
cmd_<NAME>; `run_line()` looks the method up by the first word you type, so
ADDING A COMMAND is:  write `def cmd_FOO(self, args, opts)` and it works.
(HELP lists them automatically from the docstrings.)

State kept by the terminal (this is what SAVE/LOAD writes to JSON):
    self.actions               the undo-able log of news / links / weight edits
    self.weight_overrides      SET WEIGHT changes
    self.prop_overrides        SET MAX_DEPTH / MIN_DELTA / DAMPING changes
    self.week                  the "current week" used by TICKER / RANK

UNDO works by rebuilding the graph from the CSVs and replaying the log minus
the last entry - simple, and it means every number on screen is always the
result of exactly the actions in NEWS LIST.

Command grammar:
    words are split like a shell, so quote names with spaces if a command
    needs several names:   LINK "Trent Williams" "Brock Purdy" 0.4
    single-name commands (PLAYER, EXPLAIN, EDGES, IMPACT, NEWS ADD) accept
    unquoted names:        PLAYER brock purdy
    options look like      --weeks 3     --weeks 1-4    --note "hamstring"
"""

import json
import os
import shlex
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .config import DISPLAY, FILES, STATS, WEIGHTS, PROPAGATION
from .data_loader import load_graph, slug
from .models import Action, Node
from .news import NEWS_TYPES
from . import stats as S
from . import ui
from .ui import C


class CommandError(Exception):
    """Raised for user mistakes; the message is printed in red."""


# ---------------------------------------------------------------------------
# small parsing helpers
# ---------------------------------------------------------------------------
def split_opts(tokens: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """['a', 'b', '--weeks', '3', '--note', 'x'] -> (['a','b'], {'weeks':'3','note':'x'})
    A flag with no value (e.g. --dry) is stored as 'true'."""
    pos, opts = [], {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            key = t[2:].lower()
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                opts[key] = tokens[i + 1]
                i += 2
            else:
                opts[key] = "true"
                i += 1
        else:
            pos.append(t)
            i += 1
    return pos, opts


def parse_weeks(text: Optional[str]) -> List[int]:
    """'3' -> [3];  '1-4' -> [1,2,3,4];  '3,5' -> [3,5];  None -> [] (season)."""
    if not text:
        return []
    out: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


def is_number(t: str) -> bool:
    try:
        float(t)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# the terminal
# ---------------------------------------------------------------------------
class Terminal:
    def __init__(self, files: Optional[dict] = None):
        self.files = files or {}
        self.actions: List[Action] = []
        self.weight_overrides: Dict[str, float] = {}
        self.prop_overrides: Dict[str, float] = {}
        self.week = 1
        self.next_action_id = 1
        self.graph = None
        self.rebuild()

    # ------------------------------------------------------------------
    # state management
    # ------------------------------------------------------------------
    def rebuild(self) -> None:
        """Fresh graph from CSV + overrides, then replay the whole action log."""
        self.graph = load_graph(files=self.files,
                                weights={**WEIGHTS, **self.weight_overrides},
                                propagation={**PROPAGATION, **self.prop_overrides})
        for a in self.actions:
            self._execute(a)

    def _record(self, kind: str, params: dict) -> Action:
        """Execute an action and append it to the log."""
        a = Action(id=self.next_action_id, kind=kind, params=params,
                   stamp=datetime.now().strftime("%Y-%m-%d %H:%M"))
        self._execute(a)
        self.actions.append(a)
        self.next_action_id += 1
        return a

    def _execute(self, a: Action) -> None:
        """Apply one action to the current graph (used live and on replay)."""
        g = self.graph
        p = a.params
        if a.kind == "NEWS":
            node = g.nodes[p["target"]]
            fn, _help = NEWS_TYPES[p["type"]]
            label = f"#{a.id} {p['type']} {node.name}"
            if p.get("note"):
                label += f" - {p['note']}"
            fn(g, node, float(p.get("magnitude") or 0.0), list(p.get("weeks") or []), label, a.id,
               team=p.get("team"), depth=int(p.get("depth") or 1),
               value=(float(p["value"]) if p.get("value") not in (None, "") else None))
        elif a.kind == "LINK":
            g.add_custom_edge(p["src"], p["dst"], float(p["weight"]), p.get("label") or "CUSTOM",
                              p.get("week"))
        elif a.kind == "UNLINK":
            g.remove_custom_edge(p["src"], p["dst"], p.get("week"))
        elif a.kind == "ADD":
            nid = p["id"]
            if nid not in g.nodes:
                g.add_node(Node(id=nid, name=p["name"], team="FA", pos=p["pos"], depth=1,
                                base_value=float(p["proj"]), last_year_ppg=float(p.get("last") or 0),
                                note=p.get("note") or "Added at runtime"))
            if p.get("team") in g.teams:
                g.move_player(nid, p["team"], int(p.get("depth") or 1), f"#{a.id} ADD {p['name']}", event_id=a.id)
        elif a.kind == "WEIGHT":
            # already applied through rebuild(); nothing to do live because
            # cmd_SET calls rebuild() after storing the override
            pass

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------
    def resolve(self, text: str, pos: Optional[str] = None) -> Node:
        """Turn typed text into exactly one node or raise a helpful error."""
        text = text.strip()
        if not text:
            raise CommandError("need a player / node name")
        # team shortcuts:  'KC OFF' 'KC DST' 'KC OL'
        parts = text.upper().split()
        if len(parts) == 2 and parts[0] in self.graph.teams:
            t = self.graph.teams[parts[0]]
            alias = {"OFF": t.off_node_id, "OFFENSE": t.off_node_id, "DST": t.dst_node_id,
                     "D": t.dst_node_id, "OL": t.ol_node_id, "OLINE": t.ol_node_id,
                     "O-LINE": t.ol_node_id, "LINE": t.ol_node_id}
            if parts[1] in alias and alias[parts[1]] in self.graph.nodes:
                return self.graph.nodes[alias[parts[1]]]
        found = self.graph.find(text, pos)
        if not found:
            raise CommandError(f"no node matches '{text}'")
        if len(found) > 1 and found[0].name.upper() != text.upper():
            opts = ", ".join(f"{n.name} ({n.team} {n.slot})" for n in found[:6])
            raise CommandError(f"'{text}' is ambiguous: {opts}")
        return found[0]

    def resolve_team(self, text: str):
        t = text.strip().upper()
        if t in self.graph.teams:
            return self.graph.teams[t]
        for team in self.graph.teams.values():
            if t in team.name.upper():
                return team
        raise CommandError(f"unknown team '{text}' (use the abbreviation, e.g. KC)")

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    def run_line(self, line: str) -> str:
        """Run one typed line and return the text to print."""
        line = line.strip()
        if not line or line.startswith("#"):
            return ""
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            return C.red(f"parse error: {e}")
        cmd = tokens[0].upper()
        aliases = {"P": "PLAYER", "T": "TEAM", "MOV": "TICKER", "MOVERS": "TICKER", "Q": "QUIT",
                   "EXIT": "QUIT", "?": "HELP", "N": "NEWS", "R": "RANK", "X": "EXPLAIN",
                   "WHATIF": "IMPACT", "SIM": "IMPACT"}
        cmd = aliases.get(cmd, cmd)
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            return C.red(f"unknown command '{tokens[0]}'  (type HELP)")
        args, opts = split_opts(tokens[1:])
        try:
            return fn(args, opts) or ""
        except CommandError as e:
            return C.red(f"error: {e}")
        except (KeyError, ValueError) as e:
            return C.red(f"error: {e}")

    def banner(self) -> str:
        n = len(self.actions)
        return ui.header("FANTASY FOOTBALL TERMINAL",
                         f"WEEK {self.week}   NEWS ITEMS {n}   {datetime.now().strftime('%H:%M')}   HELP for commands")

    def repl(self) -> None:
        """Interactive loop.  Ctrl-D or QUIT to leave."""
        print(self.banner())
        print(C.dim("Try:  TICKER   PLAYER mahomes   TEAM KC   NEWS ADD trent williams RELEASED   EXPLAIN purdy"))
        while True:
            try:
                line = input(C.amber(DISPLAY["PROMPT"]))
            except (EOFError, KeyboardInterrupt):
                print()
                break
            out = self.run_line(line)
            if out == "__QUIT__":
                break
            if out:
                print(out)

    # ==================================================================
    # COMMANDS  (docstring first line = HELP text)
    # ==================================================================
    def cmd_QUIT(self, args, opts):
        """QUIT                          leave the terminal"""
        return "__QUIT__"

    def cmd_HELP(self, args, opts):
        """HELP [NEWS|WEIGHTS]           this list / the news types / the weight keys"""
        if args and args[0].upper() == "NEWS":
            rows = [(C.white(k), v[1]) for k, v in NEWS_TYPES.items()]
            lines = [ui.table(["TYPE", "WHAT IT DOES"], rows),
                     "",
                     C.dim("NEWS ADD <name> <TYPE> [magnitude | TEAM] [--weeks 3 | 1-4] [--depth N] [--value X] [--note \"...\"]"),
                     C.dim("Targets can be players or team nodes: 'KC OFF', 'KC DST', 'KC OL'")]
            return ui.panel("NEWS TYPES", lines)
        if args and args[0].upper() == "WEIGHTS":
            return self.cmd_WEIGHTS([], {})
        lines = []
        for name in sorted(dir(self)):
            if name.startswith("cmd_"):
                doc = (getattr(self, name).__doc__ or "").strip().splitlines()[0]
                lines.append(doc)
        lines += ["", C.dim("Aliases: P=PLAYER T=TEAM MOV=TICKER R=RANK X=EXPLAIN SIM=IMPACT Q=QUIT"),
                  C.dim("Options: --weeks 3 | --weeks 1-4 | --week 5 | --rows 30 | --note \"text\"")]
        return ui.panel("COMMANDS", lines)

    # ---------------- boards -------------------------------------------
    def cmd_TICKER(self, args, opts):
        """TICKER [POS] [--week N]       biggest movers board (season and this week)"""
        g = self.graph
        pos = args[0].upper() if args else None
        week = int(opts.get("week", self.week))
        rows_n = int(opts.get("rows", DISPLAY["DEFAULT_ROWS"]))
        movers = S.movers(g, pos=pos)
        if not movers:
            movers = [(n, 0.0) for n, _ in S.rankings(g, pos)[:rows_n]]
            note = C.dim("board is flat - no news yet.  showing top projections.  NEWS ADD ... to move it")
        else:
            note = C.dim(f"{len(movers)} nodes moved by news.  EXPLAIN <name> shows why")
        rows = []
        for n, chg in movers[:rows_n]:
            season = S.projected_season(g, n)
            wk = S.projected_week(g, n, week)
            rows.append([ui.arrow(chg), C.white(n.name), n.team, n.slot,
                         ui.fmt_num(season), ui.fmt_chg(chg), ui.pct(chg, n.base_value),
                         ui.fmt_num(wk), ui.fmt_chg(S.week_change(n, week)),
                         C.dim(n.status if n.status != "ACTIVE" else "")])
        t = ui.table(["", "NAME", "TM", "SLOT", "PROJ", "CHG", "CHG%", f"WK{week}", "WKCHG", "STATUS"],
                     rows, ["<", "<", "<", "<", ">", ">", ">", ">", ">", "<"])
        return "\n".join([self.banner(), ui.panel("TICKER - BIGGEST MOVERS", [t, note])])

    def cmd_RANK(self, args, opts):
        """RANK <POS|ALL> [--week N]     projections ranked (QB RB WR TE K DST)"""
        g = self.graph
        pos = args[0].upper() if args else "ALL"
        pos = None if pos == "ALL" else pos
        week = int(opts["week"]) if "week" in opts else None
        rows_n = int(opts.get("rows", DISPLAY["DEFAULT_ROWS"]))
        ranked = S.rankings(g, pos, week)
        rows = []
        for i, (n, val) in enumerate(ranked[:rows_n], start=1):
            chg = (val - (S.projected_week(g, n, week) - S.week_change(n, week) - 0) if week else val - n.base_value) if False else \
                  (S.week_change(n, week) if week else S.projected_season(g, n) - n.base_value)
            sos_score, sos_label = S.strength_of_schedule(g, n)
            opp = S.opponent(g, n, week) if week else ""
            rows.append([f"{i}", C.white(n.name), n.team, n.slot, ui.fmt_num(val), ui.fmt_chg(chg),
                         ui.fmt_num(n.last_year_ppg), f"{sos_score:+.1f}% {sos_label}",
                         (opp or "") if week else ""])
        title = f"RANK {pos or 'ALL'} - " + (f"WEEK {week}" if week else "SEASON PROJECTION")
        t = ui.table(["#", "NAME", "TM", "SLOT", "PROJ", "CHG", "LY PPG", "SOS", "OPP" if week else ""],
                     rows, ["<", "<", "<", "<", ">", ">", ">", "<", "<"])
        return ui.panel(title, [t])

    def cmd_SOS(self, args, opts):
        """SOS [POS]                     strength of schedule by team (default WR)"""
        pos = (args[0].upper() if args else "WR")
        rows = []
        for abbr, score, label in S.sos_table(self.graph, pos):
            col = C.green if label == "EASY" else (C.red if label == "HARD" else C.dim)
            rows.append([abbr, self.graph.teams[abbr].name, f"{score:+.1f}%", col(label),
                         f"WK {self.graph.teams[abbr].bye_week}"])
        t = ui.table(["TM", "TEAM", "VS AVG", "RATING", "BYE"], rows, ["<", "<", ">", "<", "<"])
        return ui.panel(f"STRENGTH OF SCHEDULE - {pos}  (+ = opponents allow more = easier)", [t])

    # ---------------- detail screens -------------------------------------
    def cmd_PLAYER(self, args, opts):
        """PLAYER <name>                 full quote screen for a player / team node"""
        g = self.graph
        n = self.resolve(" ".join(args))
        week = int(opts.get("week", self.week))
        season = S.projected_season(g, n)
        chg = season - n.base_value
        unit = "PPG" if n.is_scoring else "GRADE"
        team = g.teams.get(n.team)
        status = n.status if n.status == "ACTIVE" else C.red(n.status)
        # --- quote panel
        q = [f"{C.dim('TEAM')} {n.team:<5} {C.dim('SLOT')} {n.slot:<6} {C.dim('STATUS')} {status}",
             f"{C.dim('PROJ ' + unit)} {C.white(ui.fmt_num(season, 2)):<14} {C.dim('BASE')} {n.base_value:<8.2f} "
             f"{C.dim('CHG')} {ui.fmt_chg(chg)}  {ui.pct(chg, n.base_value)}"]
        if team:
            wk_val = S.projected_week(g, n, week)
            opp = S.opponent(g, n, week)
            adj = S.matchup_adjustment(g, n, week) if opp else 0.0
            ha = "vs" if team.home.get(week) else "@"
            q.append(f"{C.dim(f'WEEK {week}')} {C.white(ui.fmt_num(wk_val, 2)):<14} "
                     f"{C.dim('OPP')} {(ha + ' ' + opp) if opp else 'BYE':<8} "
                     f"{C.dim('MATCHUP')} {ui.fmt_chg(adj)}  {C.dim('NEWS')} {ui.fmt_chg(S.week_change(n, week))}")
        if n.is_scoring:
            sos_score, sos_label = S.strength_of_schedule(g, n)
            q.append(f"{C.dim('LAST YEAR')} {n.last_year_ppg:.1f} ppg  {n.last_year_total:.1f} pts  "
                     f"{n.games_played} gms      {C.dim('SOS')} {sos_score:+.1f}% {sos_label}")
        if n.note:
            q.append(C.dim(f"NOTE {n.note}"))
        out = [ui.header(f"{n.name}", f"{n.team} {n.slot}"), ui.panel("QUOTE", q)]
        # --- schedule panel (scoring players only)
        if team and n.is_scoring:
            rows = []
            for wk in range(1, int(STATS["WEEKS_IN_SEASON"]) + 1):
                opp = team.schedule.get(wk)
                if opp is None:
                    rows.append([f"{wk}", C.dim("BYE"), "", "", "", ""])
                    continue
                prof = g.defense_profiles.get(opp)
                allowed = prof.vs.get(n.pos) if (prof and n.pos in prof.vs) else None
                rows.append([f"{wk}", ("vs " if team.home.get(wk) else "@ ") + opp,
                             ui.fmt_num(allowed) if allowed is not None else "",
                             ui.fmt_chg(S.matchup_adjustment(g, n, wk)),
                             ui.fmt_chg(n.week_deltas.get(wk, 0.0)),
                             C.white(ui.fmt_num(S.projected_week(g, n, wk)))])
            out.append(ui.panel("SCHEDULE", [ui.table(["WK", "OPP", "OPP ALLOWS", "MATCHUP", "WK NEWS", "PROJ"],
                                                      rows, ["<", "<", ">", ">", ">", ">"])]))
        # --- connections
        out.append(ui.panel("CONNECTIONS", [self._edge_table(n, limit=12)]))
        # --- news impact
        out.append(ui.panel("NEWS IMPACT", [self._explain_table(n, limit=8)]))
        return "\n".join(out)

    def cmd_TEAM(self, args, opts):
        """TEAM <ABBR>                   roster board, ratings, schedule and SOS"""
        g = self.graph
        if not args:
            raise CommandError("TEAM needs an abbreviation, e.g. TEAM KC")
        team = self.resolve_team(args[0])
        week = int(opts.get("week", self.week))
        off = g.nodes[team.off_node_id]
        olu = g.nodes[team.ol_node_id]
        dst = g.nodes.get(team.dst_node_id)
        head = [f"{C.dim('OFFENSE RATING')} {ui.fmt_num(S.projected_season(g, off), 2)} {ui.fmt_chg(off.season_delta)}   "
                f"{C.dim('O-LINE GRADE')} {ui.fmt_num(S.projected_season(g, olu), 2)} {ui.fmt_chg(olu.season_delta)}   "
                f"{C.dim('DST PROJ')} {ui.fmt_num(S.projected_season(g, dst), 2) if dst else 'n/a'}   "
                f"{C.dim('BYE')} WK {team.bye_week}"]
        rows = []
        for n in g.roster(team.abbr):
            if n.pos in ("OFF", "OLUNIT"):
                continue
            season = S.projected_season(g, n)
            rows.append([n.slot, C.white(n.name), ui.fmt_num(season), ui.fmt_chg(season - n.base_value),
                         ui.fmt_num(S.projected_week(g, n, week)), ui.fmt_num(n.last_year_ppg),
                         C.dim(n.status if n.status != "ACTIVE" else "")])
        roster = ui.table(["SLOT", "NAME", "PROJ", "CHG", f"WK{week}", "LY PPG", "STATUS"], rows,
                          ["<", "<", ">", ">", ">", ">", "<"])
        sched = "  ".join(f"{wk}:{'' if team.home.get(wk) else '@'}{opp}" for wk, opp in sorted(team.schedule.items()))
        sos = "  ".join(f"{p} {S.strength_of_schedule(g, Node(id='x', name='x', team=team.abbr, pos=p))[0]:+.0f}%"
                        for p in ("QB", "RB", "WR", "TE", "DST"))
        return "\n".join([ui.header(f"{team.name}", f"{team.abbr}  {team.division}"),
                          ui.panel("TEAM", head + ["", roster]),
                          ui.panel("SCHEDULE", [sched, C.dim("SOS " + sos)])])

    def cmd_SCHED(self, args, opts):
        """SCHED <ABBR>                  week-by-week opponents with defense ratings"""
        g = self.graph
        if not args:
            raise CommandError("SCHED needs a team, e.g. SCHED KC")
        team = self.resolve_team(args[0])
        avg = S.league_averages(g)
        rows = []
        for wk in range(1, int(STATS["WEEKS_IN_SEASON"]) + 1):
            opp = team.schedule.get(wk)
            if opp is None:
                rows.append([f"{wk}", C.dim("BYE"), "", "", "", "", ""])
                continue
            prof = g.defense_profiles.get(opp)
            cells = []
            for p in ("QB", "RB", "WR", "TE"):
                v = prof.vs.get(p, 0) if prof else 0
                pct = (v / avg[p] - 1) * 100 if avg.get(p) else 0
                s = f"{v:.1f} ({pct:+.0f}%)"
                cells.append(C.green(s) if pct > 4 else (C.red(s) if pct < -4 else s))
            rows.append([f"{wk}", ("vs " if team.home.get(wk) else "@ ") + opp] + cells + [g.teams[opp].name])
        t = ui.table(["WK", "OPP", "ALLOWS QB", "ALLOWS RB", "ALLOWS WR", "ALLOWS TE", "OPPONENT"], rows)
        return ui.panel(f"{team.abbr} SCHEDULE  (green = soft matchup, red = tough)", [t])

    def cmd_WEEK(self, args, opts):
        """WEEK [N]                      show / set the current week used by boards"""
        if args:
            self.week = int(args[0])
        return C.amber(f"current week: {self.week}")

    # ---------------- graph inspection ---------------------------------
    def _edge_table(self, n: Node, limit: int = 40) -> str:
        g = self.graph
        rows = []
        outs = sorted(g.out_edges.get(n.id, []), key=lambda e: (-abs(e.weight), e.week or 0))
        ins = sorted(g.in_edges.get(n.id, []), key=lambda e: (-abs(e.weight), e.week or 0))
        for e in outs[:limit]:
            t = g.nodes[e.dst]
            rows.append([C.amber("→"), C.white(t.name), t.team, t.slot, ui.fmt_chg(e.weight),
                         e.label + (C.dim(" [custom]") if e.custom else "")])
        for e in ins[:limit]:
            s = g.nodes[e.src]
            rows.append([C.cyan("←"), C.white(s.name), s.team, s.slot, ui.fmt_chg(e.weight),
                         e.label + (C.dim(" [custom]") if e.custom else "")])
        if not rows:
            return C.dim("no connections")
        more = ""
        if len(outs) > limit or len(ins) > limit:
            more = C.dim(f"\n... {len(outs)} out / {len(ins)} in total - EDGES <name> shows all")
        return ui.table(["", "NODE", "TM", "SLOT", "WEIGHT", "RELATION"], rows,
                        ["<", "<", "<", "<", ">", "<"]) + more

    def cmd_EDGES(self, args, opts):
        """EDGES <name>                  every connection into / out of a node with weights"""
        n = self.resolve(" ".join(args))
        return ui.panel(f"CONNECTIONS - {n.name}  (→ this node affects, ← affected by)",
                        [self._edge_table(n, limit=200)])

    def _explain_table(self, n: Node, limit: int = 30) -> str:
        g = self.graph
        if not n.contributions:
            return C.dim("no news has touched this node")
        rows = []
        for c in sorted(n.contributions, key=lambda c: -abs(c.delta))[:limit]:
            chain = C.dim(" → ").join(f"{g.nodes[p].name} {ui.fmt_chg(d)}" for p, d in zip(c.path, c.path_deltas))
            rows.append([ui.fmt_chg(c.delta), "SEASON" if c.week is None else f"WK {c.week}",
                         C.amber(c.event_label), chain])
        # totals by event
        by_event: Dict[str, float] = {}
        for c in n.contributions:
            by_event[c.event_label] = by_event.get(c.event_label, 0.0) + (
                c.delta if c.week is None else c.delta / STATS["GAMES_IN_SEASON"])
        totals = "   ".join(f"{ui.fmt_chg(v)} {C.dim(k)}" for k, v in sorted(by_event.items(), key=lambda kv: -abs(kv[1]))[:6])
        return ui.table(["DELTA", "WHEN", "EVENT", "CHAIN (how it got here)"], rows) + "\n" + \
            C.dim("season-equivalent totals by event: ") + totals

    def cmd_EXPLAIN(self, args, opts):
        """EXPLAIN <name>                why a node moved: every news chain that reached it"""
        n = self.resolve(" ".join(args))
        return ui.panel(f"EXPLAIN - {n.name}  season chg {ui.fmt_chg(S.projected_season(self.graph, n) - n.base_value)}",
                        [self._explain_table(n, limit=int(opts.get("rows", 30)))])

    def cmd_IMPACT(self, args, opts):
        """IMPACT <name> <delta> [--week N]   what-if: preview the ripples WITHOUT applying"""
        if len(args) < 2 or not is_number(args[-1]):
            raise CommandError("usage: IMPACT <name> <delta>   e.g. IMPACT trent williams -8")
        n = self.resolve(" ".join(args[:-1]))
        delta = float(args[-1])
        week = int(opts["week"]) if "week" in opts else None
        contribs = self.graph.propagate(n.id, delta, week, 0, "WHAT-IF")
        totals: Dict[Tuple[str, Optional[int]], float] = {}
        depth: Dict[Tuple[str, Optional[int]], int] = {}
        for c in contribs:
            key = (c.path[-1], c.week)
            totals[key] = totals.get(key, 0.0) + c.delta
            depth[key] = min(depth.get(key, 99), len(c.path) - 1)
        rows = []
        for (nid, wk), d in sorted(totals.items(), key=lambda kv: -abs(kv[1]))[:int(opts.get("rows", 40))]:
            t = self.graph.nodes[nid]
            rows.append([ui.arrow(d), C.white(t.name), t.team, t.slot, ui.fmt_chg(d),
                         "SEASON" if wk is None else f"WK {wk}", C.dim(f"{depth[(nid, wk)]} hop(s)")])
        return ui.panel(f"WHAT-IF: {n.name} {delta:+.2f}" + (f" in week {week}" if week else "") +
                        f"  -> {len(totals)} nodes touched (nothing applied)",
                        [ui.table(["", "NODE", "TM", "SLOT", "DELTA", "WHEN", "DISTANCE"], rows,
                                  ["<", "<", "<", "<", ">", "<", "<"])])

    # ---------------- news -----------------------------------------------
    def cmd_NEWS(self, args, opts):
        """NEWS ADD <name> <TYPE> [mag|TEAM] [--weeks N]   add news;  NEWS LIST / UNDO / DEL <id> / CLEAR"""
        sub = args[0].upper() if args else "LIST"
        if sub == "LIST":
            if not self.actions:
                return C.dim("no news yet.  HELP NEWS for the types.")
            rows = []
            for a in self.actions:
                p = a.params
                if a.kind == "NEWS":
                    desc = f"{p['type']} {self.graph.nodes[p['target']].name}"
                    extra = " ".join(x for x in [
                        (f"-> {p['team']}" if p.get("team") else ""),
                        (f"mag {p['magnitude']}" if p.get("magnitude") else ""),
                        (f"weeks {p['weeks']}" if p.get("weeks") else ""),
                        (f"\"{p['note']}\"" if p.get("note") else "")] if x)
                else:
                    desc, extra = a.kind, json.dumps(p)
                rows.append([f"{a.id}", C.dim(a.stamp), C.white(desc), extra])
            return ui.panel("NEWS LOG", [ui.table(["ID", "TIME", "ITEM", "DETAILS"], rows)])
        if sub == "UNDO":
            if not self.actions:
                raise CommandError("nothing to undo")
            a = self.actions.pop()
            self.rebuild()
            return C.amber(f"undid #{a.id} {a.kind} - graph rebuilt")
        if sub == "DEL":
            if len(args) < 2:
                raise CommandError("NEWS DEL <id>")
            wanted = int(args[1])
            before = len(self.actions)
            self.actions = [a for a in self.actions if a.id != wanted]
            if len(self.actions) == before:
                raise CommandError(f"no action with id {wanted}")
            self.rebuild()
            return C.amber(f"removed #{wanted} - graph rebuilt")
        if sub == "CLEAR":
            self.actions = []
            self.rebuild()
            return C.amber("news log cleared - graph back to base projections")
        if sub != "ADD":
            raise CommandError("NEWS ADD | LIST | UNDO | DEL <id> | CLEAR")
        # ---- NEWS ADD <name words...> <TYPE> [magnitude | TEAM] ----
        rest = args[1:]
        type_idx = next((i for i, t in enumerate(rest) if t.upper() in NEWS_TYPES), None)
        if type_idx is None or type_idx == 0:
            raise CommandError("usage: NEWS ADD <name> <TYPE> ...   (HELP NEWS lists the types)")
        node = self.resolve(" ".join(rest[:type_idx]))
        ntype = rest[type_idx].upper()
        tail = rest[type_idx + 1:]
        magnitude = 0.0
        team = None
        for t in tail:
            if is_number(t):
                magnitude = float(t)
            elif t.upper() in self.graph.teams:
                team = t.upper()
        if ntype in ("SIGNED", "TRADED") and team is None:
            raise CommandError(f"{ntype} needs a team: NEWS ADD {node.name} {ntype} KC")
        params = {"target": node.id, "type": ntype, "magnitude": magnitude,
                  "weeks": parse_weeks(opts.get("weeks") or opts.get("week")), "team": team,
                  "depth": int(opts.get("depth", 1)), "value": opts.get("value"), "note": opts.get("note", "")}
        before = {nid: S.projected_season(self.graph, x) for nid, x in self.graph.nodes.items()}
        a = self._record("NEWS", params)
        after = {nid: S.projected_season(self.graph, x) for nid, x in self.graph.nodes.items()}
        moved = sorted(((nid, after[nid] - before[nid]) for nid in after if abs(after[nid] - before[nid]) > 0.005),
                       key=lambda kv: -abs(kv[1]))
        rows = [[ui.arrow(d), C.white(self.graph.nodes[nid].name), self.graph.nodes[nid].team,
                 self.graph.nodes[nid].slot, ui.fmt_chg(d)] for nid, d in moved[:15]]
        body = [ui.table(["", "NODE", "TM", "SLOT", "SEASON CHG"], rows, ["<", "<", "<", "<", ">"])] if rows else \
               [C.dim("no season-level movement (weekly-only news shows on the week boards)")]
        return ui.panel(f"NEWS #{a.id} APPLIED: {ntype} {node.name}  -> {len(moved)} nodes moved", body)

    # ---------------- graph editing ------------------------------------
    def cmd_ADD(self, args, opts):
        """ADD <name> <TEAM> <POS> <proj> [--depth N] [--last X]   create a new player node"""
        if len(args) < 4:
            raise CommandError('usage: ADD "First Last" KC WR 12.5 --depth 2')
        name, team, pos, proj = args[0], args[1].upper(), args[2].upper(), float(args[3])
        if team not in self.graph.teams and team != "FA":
            raise CommandError(f"unknown team {team}")
        nid = f"{pos}_{slug(name)}"
        if nid in self.graph.nodes:
            raise CommandError(f"{name} already exists - use NEWS ADD ... SIGNED instead")
        a = self._record("ADD", {"id": nid, "name": name, "team": team, "pos": pos, "proj": proj,
                                 "depth": int(opts.get("depth", 1)), "last": float(opts.get("last", 0)),
                                 "note": opts.get("note", "Added at runtime")})
        return C.amber(f"#{a.id} added {name} to {team} as {pos}{opts.get('depth', 1)} @ {proj}")

    def cmd_LINK(self, args, opts):
        """LINK <src> <dst> <weight> [--label ..] [--week N]   add a custom connection"""
        if len(args) < 3:
            raise CommandError('usage: LINK "Trent Williams" "Brock Purdy" 0.4 --label "extra"')
        src, dst, w = self.resolve(args[0]), self.resolve(args[1]), float(args[2])
        week = int(opts["week"]) if "week" in opts else None
        a = self._record("LINK", {"src": src.id, "dst": dst.id, "weight": w,
                                  "label": opts.get("label", "CUSTOM"), "week": week})
        return C.amber(f"#{a.id} linked {src.name} → {dst.name} @ {w:+.2f}")

    def cmd_UNLINK(self, args, opts):
        """UNLINK <src> <dst> [--week N]   remove a connection (custom or standard)"""
        if len(args) < 2:
            raise CommandError('usage: UNLINK "A" "B"')
        src, dst = self.resolve(args[0]), self.resolve(args[1])
        week = int(opts["week"]) if "week" in opts else None
        a = self._record("UNLINK", {"src": src.id, "dst": dst.id, "week": week})
        return C.amber(f"#{a.id} unlinked {src.name} → {dst.name}")

    def cmd_WEIGHTS(self, args, opts):
        """WEIGHTS [filter]              show every weight key (SET WEIGHT to change)"""
        flt = args[0].upper() if args else ""
        rows = []
        for k, v in self.graph.weights.items():
            if flt and flt not in k:
                continue
            mark = C.amber("*") if k in self.weight_overrides else ""
            rows.append([C.white(k), ui.fmt_chg(v), mark])
        prop = "   ".join(f"{k}={v}" for k, v in self.graph.propagation.items())
        return ui.panel("WEIGHTS  (* = changed this session)",
                        [ui.table(["KEY", "WEIGHT", ""], rows), "", C.dim("PROPAGATION " + prop),
                         C.dim("SET WEIGHT <KEY> <value>   SET MAX_DEPTH 6   SET MIN_DELTA 0.01   SET DAMPING 0.9")])

    def cmd_SET(self, args, opts):
        """SET WEIGHT <KEY> <v> | SET MAX_DEPTH|MIN_DELTA|DAMPING <v>   tune the model live"""
        if len(args) >= 3 and args[0].upper() == "WEIGHT":
            key, val = args[1].upper(), float(args[2])
            self.weight_overrides[key] = val
            self.rebuild()
            return C.amber(f"{key} = {val:+.2f}  (graph rebuilt, news replayed)")
        if len(args) >= 2 and args[0].upper() in PROPAGATION:
            key, val = args[0].upper(), float(args[1])
            self.prop_overrides[key] = val
            self.rebuild()
            return C.amber(f"{key} = {val}  (graph rebuilt, news replayed)")
        raise CommandError("SET WEIGHT <KEY> <value>  |  SET MAX_DEPTH 6  |  SET MIN_DELTA 0.01  |  SET DAMPING 0.9")

    # ---------------- persistence --------------------------------------
    def cmd_SAVE(self, args, opts):
        """SAVE [file]                   write the news log + weight changes to JSON"""
        path = args[0] if args else FILES["default_save"]
        data = {"week": self.week, "weight_overrides": self.weight_overrides,
                "prop_overrides": self.prop_overrides, "actions": [a.to_dict() for a in self.actions]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return C.amber(f"saved {len(self.actions)} actions to {path}")

    def cmd_LOAD(self, args, opts):
        """LOAD [file]                   load a saved session (replaces the current log)"""
        path = args[0] if args else FILES["default_save"]
        if not os.path.exists(path):
            raise CommandError(f"no such file {path}")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.week = int(data.get("week", 1))
        self.weight_overrides = {k: float(v) for k, v in data.get("weight_overrides", {}).items()}
        self.prop_overrides = {k: float(v) for k, v in data.get("prop_overrides", {}).items()}
        self.actions = [Action.from_dict(d) for d in data.get("actions", [])]
        self.next_action_id = max([a.id for a in self.actions] + [0]) + 1
        self.rebuild()
        return C.amber(f"loaded {len(self.actions)} actions from {path}")

    def cmd_RESET(self, args, opts):
        """RESET                         wipe news, links and weight changes"""
        self.actions, self.weight_overrides, self.prop_overrides = [], {}, {}
        self.next_action_id = 1
        self.rebuild()
        return C.amber("reset to base projections")
