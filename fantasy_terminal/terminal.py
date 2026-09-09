"""
terminal.py - THE COMMAND LINE  (FFT> prompt)
=============================================

This file turns typed commands into screens.  Each command is a method named
cmd_<NAME>; `run_line()` looks the method up by the first word you type, so
ADDING A COMMAND is:  write `def cmd_FOO(self, args, opts)` and it works.
(HELP lists them automatically from the docstrings.)

Every full screen is built with self.screen(function_name, *blocks), which
adds the furniture a finance terminal has:

    ┌ amber status bar   FFT  <FUNCTION>          WK 3  NEWS 4  13:59
    │ ticker strip       J.ALLEN 24.0 ▲+0.0 │ L.JACKSON 23.0 ▲+0.0 │ ...
    │ ...the screen body: sections, tables, charts, side-by-side panels...
    └ menu bar           1) HOME  2) TICKER  3) RANK ALL ... Q) QUIT

State kept by the terminal (this is what SAVE/LOAD writes to JSON):
    self.actions               the undo-able log of news / links / weight edits
    self.weight_overrides      SET WEIGHT changes
    self.prop_overrides        SET MAX_DEPTH / MIN_DELTA / DAMPING changes
    self.week                  the "current week" used by the boards

UNDO works by rebuilding the graph from the CSVs and replaying the log minus
the last entry - simple, and it means every number on screen is always the
result of exactly the actions in NEWS LIST.

Command grammar:
    words are split like a shell, so quote names with spaces if a command
    needs several names:   LINK "Trent Williams" "Brock Purdy" 0.4
    single-name commands (PLAYER, EXPLAIN, EDGES, IMPACT, NEWS ADD) accept
    unquoted names:        PLAYER brock purdy
    options look like      --weeks 3     --weeks 1-4    --note "hamstring"
    a bare number picks that item from the bottom menu
"""

import json
import os
import shlex
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .config import DISPLAY, FILES, INGEST, STATS, WEIGHTS, PROPAGATION
from .data_loader import load_graph, slug
from .models import Action, Node
from .news import NEWS_TYPES
from . import feeds
from . import ingest
from . import stats as S
from . import ui
from .ui import C


# The numbered function menu at the bottom of every screen.  Typing just the
# number runs the command, like picking an item off a terminal menu.
MENU = [("1", "HOME"), ("2", "TICKER"), ("3", "RANK ALL"), ("4", "RANK QB"), ("5", "RANK RB"),
        ("6", "RANK WR"), ("7", "SOS"), ("8", "NEWS LIST"), ("9", "FEED LIST"), ("0", "HELP")]


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


def short_name(n: Node) -> str:
    """'Patrick Mahomes' -> 'P.MAHOMES' for the ticker strip."""
    if n.is_scoring and " " in n.name and n.pos != "DST":
        parts = n.name.split()
        return (parts[0][0] + "." + parts[-1]).upper()
    return n.name.upper()


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
        # The news review queue: Proposals from feeds waiting for approval.
        # Nothing here has touched the graph yet - see cmd_FEED.
        self.proposals: List[ingest.Proposal] = []
        self.skipped: List[dict] = []
        self.next_proposal_id = 1
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
            pass   # weights are applied by rebuild() through weight_overrides

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
        for key, command in MENU:          # a bare menu number -> that command
            if line == key:
                line = command
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            return C.red(f"parse error: {e}")
        cmd = tokens[0].upper()
        aliases = {"P": "PLAYER", "T": "TEAM", "MOV": "TICKER", "MOVERS": "TICKER", "Q": "QUIT",
                   "EXIT": "QUIT", "?": "HELP", "N": "NEWS", "R": "RANK", "X": "EXPLAIN",
                   "WHATIF": "IMPACT", "SIM": "IMPACT", "H": "HOME", "MON": "HOME", "DASH": "HOME"}
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

    # ------------------------------------------------------------------
    # screen furniture: status bar, ticker strip, menu
    # ------------------------------------------------------------------
    def status_bar(self, function: str) -> str:
        """Amber bar: function name left, week / news count / clock right."""
        return ui.header("FFT", function,
                         f"WK {self.week}  NEWS {len(self.actions)}  {datetime.now().strftime('%H:%M')}")

    def ticker_strip(self) -> str:
        """One line of the biggest movers (or top projections when nothing moved)."""
        g = self.graph
        cells_n = int(DISPLAY.get("TICKER_CELLS", 12))
        items = S.movers(g)[:cells_n]
        if not items:
            items = [(n, 0.0) for n, _ in S.rankings(g)[:cells_n]]
        cells = [f"{C.amber(short_name(n)[:12])} {C.white(f'{S.projected_season(g, n):.1f}')} "
                 f"{ui.arrow(chg)}{ui.fmt_chg(chg, 1)}" for n, chg in items]
        return ui.ticker_strip(cells)

    def menu_bar(self) -> str:
        return ui.menu_bar([f"{k}) {v}" for k, v in MENU] + ["Q) QUIT"])

    def screen(self, function: str, *blocks: str) -> str:
        """Wrap content blocks in the standard screen: status bar, ticker, body, menu."""
        body = "\n".join(b for b in blocks if b)
        return ui.paint("\n".join([self.status_bar(function), self.ticker_strip(), ui.rule(), body,
                                   self.menu_bar()]))

    def repl(self) -> None:
        """Interactive loop.  Ctrl-D or QUIT to leave."""
        clear = ui.CLEAR if DISPLAY.get("CLEAR_SCREEN", True) else ""
        print(clear + self.cmd_HOME([], {}))
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
                # full screens (built by self.screen) replace the display;
                # one-line replies (errors, "saved", ...) stay inline
                full = ui.strip(out).lstrip().startswith("FFT")
                print((clear if full else "") + out)

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
            lines = [ui.table(["TYPE", "WHAT IT DOES"], rows), "",
                     C.dim("NEWS ADD <name> <TYPE> [magnitude | TEAM] [--weeks 3 | 1-4] [--depth N] [--value X] [--note \"...\"]"),
                     C.dim("Targets can be players or team nodes: 'KC OFF', 'KC DST', 'KC OL'")]
            return self.screen("HELP NEWS", ui.panel("NEWS TYPES", lines))
        if args and args[0].upper() == "WEIGHTS":
            return self.cmd_WEIGHTS([], {})
        lines = []
        for name in sorted(dir(self)):
            if name.startswith("cmd_"):
                doc = (getattr(self, name).__doc__ or "").strip().splitlines()[0]
                cmd, _, rest = doc.partition("  ")
                lines.append(C.white(cmd) + "  " + rest.strip())
        lines += ["", C.dim("Aliases: P=PLAYER T=TEAM MOV=TICKER R=RANK X=EXPLAIN SIM=IMPACT H=HOME Q=QUIT"),
                  C.dim("Options: --weeks 3 | --weeks 1-4 | --week 5 | --rows 30 | --note \"text\""),
                  C.dim("Type a menu number (1-9) to jump to that screen.")]
        return self.screen("HELP", ui.panel("COMMANDS", lines))

    # ---------------- HOME dashboard ------------------------------------
    def cmd_HOME(self, args, opts):
        """HOME                          dashboard: movers, news feed, top plays, week matchups"""
        g = self.graph
        week = self.week
        w = ui.width()
        left_w = (w - 2) // 2
        right_w = w - 2 - left_w

        # --- top-left: biggest movers
        movers = S.movers(g)[:10]
        if movers:
            rows = [[ui.arrow(chg), C.white(n.name[:20]), n.team, n.slot, ui.fmt_num(S.projected_season(g, n)),
                     ui.fmt_chg(chg), ui.pct(chg, n.base_value)] for n, chg in movers]
            mv = ui.table(["", "NAME", "TM", "SLOT", "PROJ", "CHG", "CHG%"], rows,
                          ["<", "<", "<", "<", ">", ">", ">"])
        else:
            mv = C.dim("board is flat - add news with NEWS ADD <name> <TYPE>")
        movers_panel = ui.panel("BIGGEST MOVERS", [mv], left_w)

        # --- top-right: news feed (latest first)
        feed_rows = []
        for a in reversed(self.actions[-10:]):
            p = a.params
            if a.kind == "NEWS":
                item = f"{p['type']} {g.nodes[p['target']].name}"
                if p.get("team"):
                    item += f" → {p['team']}"
                if p.get("weeks"):
                    item += f" wk {','.join(map(str, p['weeks']))}"
            else:
                item = f"{a.kind} {json.dumps(p)[:40]}"
            feed_rows.append([C.dim(a.stamp[-5:]), C.amber(f"#{a.id}"), C.white(item[:34]),
                              C.dim((p.get("note") or "")[:right_w - 50])])
        feed = ui.table(["TIME", "ID", "HEADLINE", "NOTE"], feed_rows) if feed_rows else \
            C.dim("no headlines yet.  HELP NEWS lists the types.")
        feed_panel = ui.panel("NEWS FEED", [feed], right_w)

        # --- bottom-left: top plays this week, one per position, with bars
        play_rows = []
        overall = S.rankings(g, None, week)
        vmax = overall[0][1] if overall else 1.0
        for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
            top = S.rankings(g, pos, week)[:3]
            for n, val in top:
                opp = S.opponent(g, n, week) or ""
                play_rows.append([C.amber(pos), C.white(n.name[:20]), n.team, opp, ui.fmt_num(val),
                                  ui.bar(val, vmax, 12)])
        plays = ui.table(["POS", f"WEEK {week} TOP PLAYS", "TM", "OPP", "PROJ", ""], play_rows,
                         ["<", "<", "<", "<", ">", "<"])
        plays_panel = ui.panel(f"WEEK {week} - TOP PLAYS BY POSITION", [plays], left_w)

        # --- bottom-right: this week's games with offense ratings and DST projections
        game_rows, seen = [], set()
        for abbr, team in sorted(g.teams.items()):
            opp = team.schedule.get(week)
            if not opp or abbr in seen:
                continue
            seen.update({abbr, opp})
            home, away = (abbr, opp) if team.home.get(week) else (opp, abbr)
            ho, ao = g.nodes[g.teams[home].off_node_id], g.nodes[g.teams[away].off_node_id]
            hd, ad = g.nodes.get(g.teams[home].dst_node_id), g.nodes.get(g.teams[away].dst_node_id)
            game_rows.append([C.white(away), ui.fmt_num(S.projected_season(g, ao), 1),
                              C.dim("@"), C.white(home), ui.fmt_num(S.projected_season(g, ho), 1),
                              ui.fmt_num(S.projected_week(g, ad, week)) if ad else "",
                              ui.fmt_num(S.projected_week(g, hd, week)) if hd else ""])
        byes = ", ".join(a for a, t in sorted(g.teams.items()) if t.bye_week == week)
        games = ui.table(["AWAY", "OFF", "", "HOME", "OFF", "AWAY DST", "HOME DST"], game_rows,
                         ["<", ">", "<", "<", ">", ">", ">"])
        games_panel = ui.panel(f"WEEK {week} - GAMES", [games, C.dim(f"BYE: {byes or 'none'}")], right_w)

        top = ui.columns([movers_panel, feed_panel], [left_w, right_w])
        bottom = ui.columns([plays_panel, games_panel], [left_w, right_w])
        return self.screen("HOME", top, "", bottom)

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
        vmax = max(abs(chg) for _, chg in movers) or 1.0
        rows = []
        for n, chg in movers[:rows_n]:
            season = S.projected_season(g, n)
            wk = S.projected_week(g, n, week)
            rows.append([ui.arrow(chg), C.white(n.name), n.team, n.slot,
                         ui.fmt_num(season), ui.fmt_chg(chg), ui.pct(chg, n.base_value),
                         ui.dbar(chg, vmax, 17),
                         ui.fmt_num(wk), ui.fmt_chg(S.week_change(n, week)),
                         C.dim(n.status if n.status != "ACTIVE" else "")])
        t = ui.table(["", "NAME", "TM", "SLOT", "PROJ", "CHG", "CHG%", "", f"WK{week}", "WKCHG", "STATUS"],
                     rows, ["<", "<", "<", "<", ">", ">", ">", "<", ">", ">", "<"])
        return self.screen("TICKER", ui.panel("BIGGEST MOVERS" + (f" - {pos}" if pos else ""), [t, note]))

    def cmd_RANK(self, args, opts):
        """RANK <POS|ALL> [--week N]     projections ranked with bar chart (QB RB WR TE K DST)"""
        g = self.graph
        pos = args[0].upper() if args else "ALL"
        pos = None if pos == "ALL" else pos
        week = int(opts["week"]) if "week" in opts else None
        rows_n = int(opts.get("rows", DISPLAY["DEFAULT_ROWS"]))
        ranked = S.rankings(g, pos, week)
        vmax = ranked[0][1] if ranked else 1.0
        rows = []
        for i, (n, val) in enumerate(ranked[:rows_n], start=1):
            chg = S.week_change(n, week) if week else S.projected_season(g, n) - n.base_value
            sos_score, sos_label = S.strength_of_schedule(g, n)
            opp = (S.opponent(g, n, week) or "") if week else ""
            rows.append([C.amber(f"{i}"), C.white(n.name), n.team, n.slot, ui.fmt_num(val), ui.bar(val, vmax, 24),
                         ui.fmt_chg(chg), ui.fmt_num(n.last_year_ppg), f"{sos_score:+.1f}% {sos_label}", opp])
        title = f"RANK {pos or 'ALL'} - " + (f"WEEK {week}" if week else "SEASON PROJECTION")
        t = ui.table(["#", "NAME", "TM", "SLOT", "PROJ", "", "CHG", "LY PPG", "SOS", "OPP" if week else ""],
                     rows, ["<", "<", "<", "<", ">", "<", ">", ">", "<", "<"])
        return self.screen(f"RANK {pos or 'ALL'}", ui.panel(title, [t]))

    def cmd_SOS(self, args, opts):
        """SOS [POS]                     strength of schedule by team (default WR)"""
        pos = (args[0].upper() if args else "WR")
        table_rows = S.sos_table(self.graph, pos)
        vmax = max(abs(r[1]) for r in table_rows) or 1.0
        rows = []
        for abbr, score, label in table_rows:
            col = C.green if label == "EASY" else (C.red if label == "HARD" else C.dim)
            rows.append([C.white(abbr), self.graph.teams[abbr].name, f"{score:+.1f}%",
                         ui.dbar(score, vmax, 17), col(label),
                         f"WK {self.graph.teams[abbr].bye_week}"])
        t = ui.table(["TM", "TEAM", "VS AVG", "", "RATING", "BYE"], rows, ["<", "<", ">", "<", "<", "<"])
        return self.screen(f"SOS {pos}",
                           ui.panel(f"STRENGTH OF SCHEDULE - {pos}  (+ = opponents allow more = easier)", [t]))

    # ---------------- detail screens -------------------------------------
    def cmd_PLAYER(self, args, opts):
        """PLAYER <name>                 quote screen: projection, chart, schedule, connections, news"""
        g = self.graph
        n = self.resolve(" ".join(args))
        week = int(opts.get("week", self.week))
        season = S.projected_season(g, n)
        chg = season - n.base_value
        unit = "PPG" if n.is_scoring else "GRADE"
        team = g.teams.get(n.team)
        status = C.green("ACTIVE") if n.status == "ACTIVE" else C.red(n.status)
        w = ui.width()
        left_w = (w - 2) // 2
        right_w = w - 2 - left_w

        # --- quote block: two label/value pairs per line (label amber, value white)
        sep = "     "
        q = [ui.kv("TEAM", C.white(n.team)) + sep + ui.kv("SLOT", C.white(n.slot)) + sep + ui.kv("STATUS", status),
             ui.kv(f"PROJ {unit}", C.white(f"{season:.2f}")) + sep + ui.kv("BASE", f"{n.base_value:.2f}"),
             ui.kv("CHG", ui.fmt_chg(chg)) + sep + ui.kv("CHG%", ui.pct(chg, n.base_value))]
        if team:
            wk_val = S.projected_week(g, n, week)
            opp = S.opponent(g, n, week)
            adj = S.matchup_adjustment(g, n, week) if opp else 0.0
            ha = "vs" if team.home.get(week) else "@"
            q.append(ui.kv(f"WEEK {week}", ui.fmt_num(wk_val, 2)) + sep +
                     ui.kv("OPP", C.white(f"{ha} {opp}") if opp else C.dim("BYE")))
            q.append(ui.kv("MATCHUP", ui.fmt_chg(adj)) + sep + ui.kv("WK NEWS", ui.fmt_chg(S.week_change(n, week))))
        if n.is_scoring:
            sos_score, sos_label = S.strength_of_schedule(g, n)
            q.append(ui.kv("LAST YR", C.white(f"{n.last_year_ppg:.1f}") + f" ppg / {n.last_year_total:.1f} pts / {n.games_played} gms"))
            q.append(ui.kv("SOS", f"{sos_score:+.1f}% {sos_label}"))
        if n.note:
            q.append(C.dim(f"NOTE  {n.note}"))
        quote = ui.panel("QUOTE", q, left_w)

        # --- weekly projection chart (right of the quote)
        if team and n.is_scoring:
            weeks = list(range(1, int(STATS["WEEKS_IN_SEASON"]) + 1))
            vals = [S.projected_week(g, n, wk) for wk in weeks]
            chart = ui.vchart(vals, [str(wk) for wk in weeks], height=6, baseline=n.base_value)
            legend = C.dim(f"weekly projection, green >= base {n.base_value:.1f}, red below   spark ") + ui.sparkline(vals)
            chart_panel = ui.panel("WEEKLY PROJECTION", [chart, legend], right_w)
        else:
            chart_panel = ui.panel("VALUE", [C.dim("structural node - value is a grade, not points")], right_w)
        top = ui.columns([quote, chart_panel], [left_w, right_w])

        # --- schedule (left) and connections (right)
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
                             f"{allowed:.1f}" if allowed is not None else "",
                             ui.fmt_chg(S.matchup_adjustment(g, n, wk)),
                             ui.fmt_chg(n.week_deltas.get(wk, 0.0)),
                             ui.fmt_num(S.projected_week(g, n, wk))])
            sched = ui.panel("SCHEDULE", [ui.table(["WK", "OPP", "ALLOWS", "MATCHUP", "NEWS", "PROJ"], rows,
                                                   ["<", "<", ">", ">", ">", ">"])], left_w)
        else:
            sched = ui.panel("ROSTER", [self._roster_table(n.team, week)] if team else [C.dim("free agent")], left_w)
        conns = ui.panel("CONNECTIONS  (→ affects, ← affected by)", [self._edge_table(n, limit=9)], right_w)
        middle = ui.columns([sched, conns], [left_w, right_w])

        # --- news impact full width
        impact = ui.panel("NEWS IMPACT", [self._explain_table(n, limit=6)])
        return self.screen(f"PLAYER {n.name.upper()}", top, "", middle, "", impact)

    def _roster_table(self, abbr: str, week: int) -> str:
        g = self.graph
        rows = []
        for n in g.roster(abbr):
            if n.pos in ("OFF", "OLUNIT"):
                continue
            season = S.projected_season(g, n)
            rows.append([C.amber(n.slot), C.white(n.name), ui.fmt_num(season), ui.fmt_chg(season - n.base_value),
                         ui.fmt_num(S.projected_week(g, n, week)), f"{n.last_year_ppg:.1f}",
                         C.dim(n.status if n.status != "ACTIVE" else "")])
        return ui.table(["SLOT", "NAME", "PROJ", "CHG", f"WK{week}", "LY PPG", "STATUS"], rows,
                        ["<", "<", ">", ">", ">", ">", "<"])

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
        w = ui.width()
        left_w = (w - 2) * 3 // 5
        right_w = w - 2 - left_w

        head = [ui.kv("OFFENSE", ui.fmt_num(S.projected_season(g, off), 2) + " " + ui.fmt_chg(off.season_delta)) + "   " +
                ui.kv("O-LINE", ui.fmt_num(S.projected_season(g, olu), 2) + " " + ui.fmt_chg(olu.season_delta)) + "   " +
                ui.kv("DST", ui.fmt_num(S.projected_season(g, dst), 2) if dst else C.dim("n/a")) + "   " +
                ui.kv("BYE", C.white(f"WK {team.bye_week}")), ""]
        roster = ui.panel("ROSTER", head + [self._roster_table(team.abbr, week)], left_w)

        avg = S.league_averages(g)
        srows = []
        for wk in range(1, int(STATS["WEEKS_IN_SEASON"]) + 1):
            opp = team.schedule.get(wk)
            if opp is None:
                srows.append([f"{wk}", C.dim("BYE"), "", ""])
                continue
            prof = g.defense_profiles.get(opp)
            allowed = sum(prof.vs.get(p, 0) for p in ("QB", "RB", "WR", "TE")) if prof else 0
            avg_all = sum(avg.get(p, 0) for p in ("QB", "RB", "WR", "TE")) or 1
            pct = (allowed / avg_all - 1) * 100
            cell = f"{pct:+.0f}%"
            cell = C.green(cell) if pct > 3 else (C.red(cell) if pct < -3 else C.dim(cell))
            srows.append([f"{wk}", ("vs " if team.home.get(wk) else "@ ") + opp, cell, ui.dbar(pct, 20, 15)])
        sos_line = "  ".join(f"{C.amber(p)} {S.strength_of_schedule(g, Node(id='x', name='x', team=team.abbr, pos=p))[0]:+.0f}%"
                             for p in ("QB", "RB", "WR", "TE", "DST"))
        sched = ui.panel("SCHEDULE  (opp points allowed vs avg)",
                         [ui.table(["WK", "OPP", "VS AVG", ""], srows), "", C.dim("SOS ") + sos_line], right_w)
        return self.screen(f"TEAM {team.abbr} - {team.name.upper()}",
                           ui.columns([roster, sched], [left_w, right_w]))

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
            rows.append([f"{wk}", C.white(("vs " if team.home.get(wk) else "@ ") + opp)] + cells + [C.dim(g.teams[opp].name)])
        t = ui.table(["WK", "OPP", "ALLOWS QB", "ALLOWS RB", "ALLOWS WR", "ALLOWS TE", "OPPONENT"], rows)
        return self.screen(f"SCHED {team.abbr}",
                           ui.panel(f"{team.abbr} SCHEDULE  (green = soft matchup, red = tough)", [t]))

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
        return self.screen(f"EDGES {n.name.upper()}",
                           ui.panel(f"CONNECTIONS - {n.name}  (→ this node affects, ← affected by)",
                                    [self._edge_table(n, limit=200)]))

    def _explain_table(self, n: Node, limit: int = 30) -> str:
        g = self.graph
        if not n.contributions:
            return C.dim("no news has touched this node")
        rows = []
        for c in sorted(n.contributions, key=lambda c: -abs(c.delta))[:limit]:
            chain = C.dim(" → ").join(f"{g.nodes[p].name} {ui.fmt_chg(d)}" for p, d in zip(c.path, c.path_deltas))
            rows.append([ui.fmt_chg(c.delta), "SEASON" if c.week is None else f"WK {c.week}",
                         C.amber(c.event_label[:40]), chain])
        by_event: Dict[str, float] = {}
        for c in n.contributions:
            by_event[c.event_label] = by_event.get(c.event_label, 0.0) + (
                c.delta if c.week is None else c.delta / STATS["GAMES_IN_SEASON"])
        totals = "   ".join(f"{ui.fmt_chg(v)} {C.dim(k[:40])}"
                            for k, v in sorted(by_event.items(), key=lambda kv: -abs(kv[1]))[:4])
        return ui.table(["DELTA", "WHEN", "EVENT", "CHAIN (how it got here)"], rows) + "\n" + \
            C.dim("season-equivalent totals by event: ") + totals

    def cmd_EXPLAIN(self, args, opts):
        """EXPLAIN <name>                why a node moved: every news chain that reached it"""
        n = self.resolve(" ".join(args))
        chg = S.projected_season(self.graph, n) - n.base_value
        return self.screen(f"EXPLAIN {n.name.upper()}",
                           ui.panel(f"EXPLAIN - {n.name}  season chg {ui.strip(ui.fmt_chg(chg))}",
                                    [self._explain_table(n, limit=int(opts.get("rows", 30)))]))

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
        vmax = max(abs(d) for d in totals.values()) or 1.0
        rows = []
        for (nid, wk), d in sorted(totals.items(), key=lambda kv: -abs(kv[1]))[:int(opts.get("rows", 40))]:
            t = self.graph.nodes[nid]
            rows.append([ui.arrow(d), C.white(t.name), t.team, t.slot, ui.fmt_chg(d),
                         ui.dbar(d, vmax, 17),
                         "SEASON" if wk is None else f"WK {wk}", C.dim(f"{depth[(nid, wk)]} hop(s)")])
        title = f"WHAT-IF: {n.name} {delta:+.2f}" + (f" in week {week}" if week else "") + \
            f"  →  {len(totals)} nodes touched (nothing applied)"
        return self.screen("IMPACT", ui.panel(title, [ui.table(["", "NODE", "TM", "SLOT", "DELTA", "", "WHEN", "DISTANCE"],
                                                                rows, ["<", "<", "<", "<", ">", "<", "<", "<"])]))

    # ---------------- news -----------------------------------------------
    def cmd_NEWS(self, args, opts):
        """NEWS ADD <name> <TYPE> [mag|TEAM] [--weeks N]   add news;  NEWS LIST / UNDO / DEL <id> / CLEAR"""
        sub = args[0].upper() if args else "LIST"
        if sub == "LIST":
            if not self.actions:
                return self.screen("NEWS", ui.panel("NEWS LOG", [C.dim("no news yet.  HELP NEWS for the types.")]))
            rows = []
            for a in self.actions:
                p = a.params
                if a.kind == "NEWS":
                    desc = f"{p['type']} {self.graph.nodes[p['target']].name}"
                    extra = " ".join(x for x in [
                        (f"→ {p['team']}" if p.get("team") else ""),
                        (f"mag {p['magnitude']}" if p.get("magnitude") else ""),
                        (f"weeks {p['weeks']}" if p.get("weeks") else ""),
                        (f"\"{p['note']}\"" if p.get("note") else "")] if x)
                else:
                    desc, extra = a.kind, json.dumps(p)
                rows.append([C.amber(f"#{a.id}"), C.dim(a.stamp), C.white(desc), extra])
            return self.screen("NEWS", ui.panel("NEWS LOG", [ui.table(["ID", "TIME", "ITEM", "DETAILS"], rows)]))
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
        vmax = max((abs(d) for _, d in moved), default=1.0) or 1.0
        rows = [[ui.arrow(d), C.white(self.graph.nodes[nid].name), self.graph.nodes[nid].team,
                 self.graph.nodes[nid].slot, ui.fmt_chg(d), ui.dbar(d, vmax, 21)]
                for nid, d in moved[:20]]
        body = [ui.table(["", "NODE", "TM", "SLOT", "SEASON CHG", ""], rows, ["<", "<", "<", "<", ">", "<"])] if rows else \
               [C.dim("no season-level movement (weekly-only news shows on the week boards and PLAYER charts)")]
        return self.screen("NEWS ADD",
                           ui.panel(f"NEWS #{a.id} APPLIED: {ntype} {node.name}  →  {len(moved)} nodes moved", body))

    # ---------------- live news ingestion ------------------------------
    def cmd_FEED(self, args, opts):
        """FEED FETCH|LIST|APPLY|DROP|SKIPPED|CLEAR   pull real NFL news into a review queue"""
        sub = args[0].upper() if args else "LIST"

        # ---- FEED FETCH [--ai] [--force] -------------------------------
        if sub == "FETCH":
            use_ai = "ai" in opts
            force = "force" in opts
            lines, errors = [], []

            # 1) Sleeper injury report - structured, free, no AI needed.
            try:
                injuries = feeds.fetch_sleeper_injuries(force=force)
                props, skipped = ingest.match_sleeper_injuries(
                    self.graph, injuries, self.week, self.next_proposal_id)
                self.proposals.extend(props)
                self.skipped.extend(skipped)
                self.next_proposal_id += len(props)
                lines.append(f"{C.amber('SLEEPER')}  {len(injuries)} injury designations  "
                             f"→  {C.white(str(len(props)))} proposals, {len(skipped)} skipped")
            except feeds.FeedError as e:
                errors.append(f"Sleeper: {e}")

            # 2) RSS headlines - free text, needs the AI reader to interpret.
            if use_ai:
                items, feed_errs = feeds.fetch_all_rss()
                errors.extend(feed_errs)
                roster = [n.name for n in self.graph.nodes.values()
                          if n.pos not in ("OFF", "OLUNIT", "DST")]
                results, ai_errs = ingest.extract_with_claude(
                    items, roster, self.week, max_items=int(opts.get("max", INGEST["MAX_HEADLINES"])))
                errors.extend(ai_errs)
                props, skipped = ingest.ai_results_to_proposals(
                    self.graph, results, self.week, self.next_proposal_id)
                self.proposals.extend(props)
                self.skipped.extend(skipped)
                self.next_proposal_id += len(props)
                lines.append(f"{C.amber('HEADLINES')}  {len(items)} stories read by AI  "
                             f"→  {C.white(str(len(props)))} proposals, {len(skipped)} skipped")
            else:
                lines.append(C.dim("HEADLINES  skipped - add --ai to read RSS headlines with Claude"))

            for e in errors:
                lines.append(C.red("! " + str(e)[:110]))
            lines.append("")
            lines.append(C.dim(f"{len(self.proposals)} proposals in the queue.  "
                               f"Nothing has changed yet - FEED LIST to review, FEED APPLY to commit."))
            return self.screen("FEED FETCH", ui.panel("FETCHED LIVE NFL NEWS", lines))

        # ---- FEED LIST -------------------------------------------------
        if sub == "LIST":
            pending = [p for p in self.proposals if not p.applied]
            if not pending:
                return self.screen("FEED", ui.panel("NEWS REVIEW QUEUE", [
                    C.dim("queue is empty.  FEED FETCH pulls the live injury report."),
                    C.dim("FEED FETCH --ai also reads RSS headlines with Claude.")]))
            rows = []
            for p in pending:
                flag = C.green("OK") if p.confidence >= INGEST["AUTO_MIN_CONFIDENCE"] else C.amber("CHECK")
                rows.append([C.amber(f"#{p.id}"), flag, f"{p.confidence:.2f}",
                             C.dim(p.origin.upper()), C.white(p.node_name), p.team,
                             p.news_type, C.dim(p.headline[:52])])
            t = ui.table(["ID", "", "CONF", "VIA", "PLAYER", "TM", "TYPE", "HEADLINE"], rows)
            return self.screen("FEED", ui.panel(f"NEWS REVIEW QUEUE - {len(pending)} pending", [
                t, "",
                C.dim("FEED APPLY ALL | FEED APPLY HIGH (conf >= "
                      f"{INGEST['AUTO_MIN_CONFIDENCE']}) | FEED APPLY 3 | FEED DROP 3 | FEED SKIPPED")]))

        # ---- FEED APPLY <id|ALL|HIGH> ----------------------------------
        if sub == "APPLY":
            target = (args[1].upper() if len(args) > 1 else "").strip()
            pending = [p for p in self.proposals if not p.applied]
            if not target:
                raise CommandError("FEED APPLY ALL | HIGH | <id>")
            if target == "ALL":
                chosen = pending
            elif target == "HIGH":
                chosen = [p for p in pending if p.confidence >= INGEST["AUTO_MIN_CONFIDENCE"]]
            else:
                chosen = [p for p in pending if str(p.id) == target]
                if not chosen:
                    raise CommandError(f"no pending proposal #{target}")
            if not chosen:
                raise CommandError("nothing to apply")
            rows = []
            for p in chosen:
                # Run the proposal's command through the normal command path,
                # so a fed-in item is indistinguishable from one you typed.
                out = self.run_line(p.command)
                ok = "error" not in ui.strip(out).lower()[:60]
                p.applied = ok
                rows.append([C.amber(f"#{p.id}"), C.green("APPLIED") if ok else C.red("FAILED"),
                             C.white(p.node_name), p.news_type, C.dim(p.command[:66])])
            return self.screen("FEED APPLY", ui.panel(
                f"APPLIED {sum(1 for p in chosen if p.applied)} OF {len(chosen)} PROPOSALS",
                [ui.table(["ID", "STATUS", "PLAYER", "TYPE", "COMMAND"], rows), "",
                 C.dim("TICKER shows what moved.  NEWS UNDO reverses the last one.")]))

        # ---- FEED DROP <id> --------------------------------------------
        if sub == "DROP":
            if len(args) < 2:
                raise CommandError("FEED DROP <id>")
            before = len(self.proposals)
            self.proposals = [p for p in self.proposals if str(p.id) != args[1]]
            if len(self.proposals) == before:
                raise CommandError(f"no proposal #{args[1]}")
            return C.amber(f"dropped proposal #{args[1]}")

        # ---- FEED SKIPPED ----------------------------------------------
        if sub == "SKIPPED":
            if not self.skipped:
                return C.dim("nothing was skipped")
            rows = [[C.white(s.get("name", "?")), s.get("team", ""), s.get("pos", ""),
                     s.get("status", ""), C.dim(s.get("why", ""))] for s in self.skipped[:40]]
            return self.screen("FEED SKIPPED", ui.panel(
                f"SKIPPED - {len(self.skipped)} items the feed could not safely map",
                [ui.table(["NAME", "TM", "POS", "STATUS", "WHY"], rows), "",
                 C.dim("'not on our roster' just means the player is not in data/players.csv.")]))

        # ---- FEED CLEAR ------------------------------------------------
        if sub == "CLEAR":
            n = len(self.proposals)
            self.proposals, self.skipped = [], []
            return C.amber(f"cleared {n} proposals (applied news is untouched - use NEWS UNDO for that)")

        raise CommandError("FEED FETCH [--ai] [--force] | LIST | APPLY <id|ALL|HIGH> | DROP <id> | SKIPPED | CLEAR")

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
            rows.append([C.white(k), ui.fmt_chg(v), ui.bar(abs(v), 1.0, 12, "green" if v > 0 else "red"), mark])
        # three columns of weights so they fit on one screen
        t = ui.table(["KEY", "WEIGHT", "", ""], rows)
        lines = t.split("\n")
        head, body = lines[:2], lines[2:]
        per = (len(body) + 2) // 3
        cols = ["\n".join(head + body[i:i + per]) for i in range(0, len(body), per)]
        prop = "   ".join(f"{k}={v}" for k, v in self.graph.propagation.items())
        return self.screen("WEIGHTS", ui.panel("WEIGHTS  (* = changed this session)", [
            ui.columns(cols), "", C.dim("PROPAGATION " + prop),
            C.dim("SET WEIGHT <KEY> <value>   SET MAX_DEPTH 6   SET MIN_DELTA 0.01   SET DAMPING 0.9")]))

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
