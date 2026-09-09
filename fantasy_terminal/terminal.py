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
import re
import shlex
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .config import DISPLAY, FILES, GAMES, INGEST, LINEUP, STATS, WEIGHTS, PROPAGATION
from .data_loader import load_graph, slug
from .models import Action, Node
from .news import NEWS_TYPES
from . import feeds
from . import ingest
from . import games as gamesmod
from . import rosters as rostermod
from . import stats as S
from . import ui
from .ui import C


# The numbered function menu at the bottom of every screen.  Typing just the
# number runs the command, like picking an item off a terminal menu.
MENU = [("1", "HOME"), ("2", "TICKER"), ("3", "RANK ALL"), ("4", "RANK QB"), ("5", "RANK RB"),
        ("6", "RANK WR"), ("7", "SOS"), ("8", "NEWS LIST"), ("9", "FEED LIST"), ("0", "ROSTER")]


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


# An apostrophe inside a word ("Ja'Marr", "D'Andre") is part of the name, not
# a quote.  Shell-style splitting would choke on it, so we swap those for a
# placeholder before splitting and swap them back afterwards.
_APOS = "\x00APOS\x00"
_INNER_APOS = re.compile(r"(?<=\w)['\u2019](?=\w)")


def smart_split(line: str) -> List[str]:
    """shlex.split, but tolerant of apostrophes inside player names."""
    protected = _INNER_APOS.sub(_APOS, line)
    return [t.replace(_APOS, "'") for t in shlex.split(protected)]


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
        # Your league: named rosters (MYTEAM, OPPONENT, a trade partner...).
        # Saved and restored by SAVE / LOAD along with the news log.
        self.rosters: Dict[str, rostermod.Roster] = {}
        # Tabs: each one remembers the command it is showing, so switching
        # back to it re-runs that command with fresh numbers.
        # [[display name, command line], ...]
        self.tabs: List[List[str]] = [["HOME", "HOME"]]
        self.active_tab = 1
        self._in_tab_switch = False
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
        # T1 / T2 / ... jump straight to that tab
        m = re.fullmatch(r"[Tt](\d+)", line)
        if m:
            line = f"TAB {m.group(1)}"
        try:
            tokens = smart_split(line)
        except ValueError as e:
            return C.red(f"parse error: {e}  (quote names with spaces, e.g. LINK \"A\" \"B\" 0.3)")
        cmd = tokens[0].upper()
        aliases = {"P": "PLAYER", "T": "TEAM", "MOV": "TICKER", "MOVERS": "TICKER", "Q": "QUIT",
                   "EXIT": "QUIT", "?": "HELP", "N": "NEWS", "R": "RANK", "X": "EXPLAIN",
                   "WHATIF": "IMPACT", "SIM": "IMPACT", "MY": "ROSTER", "VS": "MATCHUP", "H": "HOME", "MON": "HOME", "DASH": "HOME"}
        cmd = aliases.get(cmd, cmd)
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            return C.red(f"unknown command '{tokens[0]}'  (type HELP)")
        args, opts = split_opts(tokens[1:])
        # A full-screen command becomes what this tab is showing, so that
        # switching away and back re-runs it rather than losing the view.
        if cmd not in ("TAB", "QUIT", "HELP") and not self._in_tab_switch:
            if self.tabs and 1 <= self.active_tab <= len(self.tabs):
                self.tabs[self.active_tab - 1][1] = line
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

    def chrome_rows(self) -> int:
        """How many rows the furniture eats: status bar, ticker, rule, menu,
        and the input line, plus the tab bar when more than one tab is open."""
        return 6 + (1 if len(self.tabs) > 1 else 0)

    def body_rows(self, panel_chrome: int = 4) -> int:
        """How many DATA rows a board can show and still fit the window.

        panel_chrome covers the panel title, the column headers, the rule
        under them and any footnote, so callers get a straight row count.
        """
        return max(5, ui.height() - self.chrome_rows() - panel_chrome)

    def rows_opt(self, opts: dict, panel_chrome: int = 4) -> int:
        """--rows N if given, otherwise fill the window."""
        if "rows" in opts:
            return int(opts["rows"])
        return self.body_rows(panel_chrome)

    def screen(self, function: str, *blocks: str) -> str:
        """Wrap content blocks in the standard screen: status bar, ticker,
        tab bar (when more than one tab is open), body, menu."""
        body = "\n".join(b for b in blocks if b)
        parts = [self.status_bar(function), self.ticker_strip()]
        if len(self.tabs) > 1:
            parts.append(ui.tab_bar([(t[0], t[1]) for t in self.tabs], self.active_tab))
        parts += [ui.rule(), body, self.menu_bar()]
        return ui.paint("\n".join(parts))

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
                  C.dim("Type a menu number (0-9) to jump to that screen."),
                  C.dim("Tabs: TAB NEW <cmd> opens one, TAB 2 (or T2) switches, TAB CLOSE <n> removes."),
                  C.dim("Boards fill the window - make the window taller and they show more rows.")]
        return self.screen("HELP", ui.panel("COMMANDS", lines))

    # ---------------- HOME dashboard ------------------------------------
    def cmd_HOME(self, args, opts):
        """HOME                          dashboard: movers, news feed, top plays, week matchups"""
        g = self.graph
        week = self.week
        w = ui.width()
        left_w = (w - 2) // 2
        right_w = w - 2 - left_w
        # Split the window between the two rows of panels so the dashboard
        # fills whatever height the terminal has.  Each panel spends 3 rows on
        # its title and column headers, so the rest is data.
        avail = self.body_rows(panel_chrome=1)
        top_h = max(6, avail // 2)
        bottom_h = max(6, avail - top_h - 1)
        top_rows, bottom_rows = max(3, top_h - 3), max(3, bottom_h - 3)

        # --- top-left: biggest movers.  With no news yet the board would be
        # blank, so fall back to the top projections and say so - an empty
        # panel teaches you nothing.
        movers = S.movers(g)[:top_rows]
        if movers:
            rows = [[ui.arrow(chg), C.white(n.name[:20]), n.team, n.slot, ui.fmt_num(S.projected_season(g, n)),
                     ui.fmt_chg(chg), ui.pct(chg, n.base_value)] for n, chg in movers]
            mv_title = "BIGGEST MOVERS"
            note = None
        else:
            rows = [[C.dim(f"{i}"), C.white(n.name[:20]), n.team, n.slot, ui.fmt_num(v),
                     ui.fmt_chg(0.0), C.dim("  -")]
                    for i, (n, v) in enumerate(S.rankings(g)[:top_rows], start=1)]
            mv_title = "TOP PROJECTIONS  (no news yet)"
            note = C.dim("FEED FETCH pulls the live injury report and this becomes a movers board")
        mv = ui.table(["", "NAME", "TM", "SLOT", "PROJ", "CHG", "CHG%"], rows,
                      ["<", "<", "<", "<", ">", ">", ">"])
        movers_panel = ui.fill(ui.panel(mv_title, [mv] + ([note] if note else []), left_w), top_h)

        # --- top-right: news feed (latest first)
        feed_rows = []
        for a in reversed(self.actions[-top_rows:]):
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
        if feed_rows:
            feed = ui.table(["TIME", "ID", "HEADLINE", "NOTE"], feed_rows)
        else:
            feed = "\n".join([
                C.dim("no headlines yet.  Two ways to fill this:"), "",
                C.white("  FEED FETCH") + C.dim("                 today's real injury report"),
                C.white("  FEED FETCH --ai") + C.dim("            also read news headlines"),
                C.white('  NEWS ADD mahomes OUT') + C.dim("       type one in yourself"), "",
                C.dim("HELP NEWS lists every news type."),
            ])
        feed_panel = ui.fill(ui.panel("NEWS FEED", [feed], right_w), top_h)

        # --- bottom-left: top plays this week, one per position, with bars
        play_rows = []
        overall = S.rankings(g, None, week)
        vmax = overall[0][1] if overall else 1.0
        positions = ("QB", "RB", "WR", "TE", "K", "DST")
        per_pos = max(1, bottom_rows // len(positions))
        for pos in positions:
            top = S.rankings(g, pos, week)[:per_pos]
            for n, val in top:
                opp = S.opponent(g, n, week) or ""
                play_rows.append([C.amber(pos), C.white(n.name[:20]), n.team, opp, ui.fmt_num(val),
                                  ui.bar(val, vmax, 12)])
        plays = ui.table(["POS", f"WEEK {week} TOP PLAYS", "TM", "OPP", "PROJ", ""], play_rows,
                         ["<", "<", "<", "<", ">", "<"])
        plays_panel = ui.fill(ui.panel(f"WEEK {week} - TOP PLAYS BY POSITION", [plays], left_w), bottom_h)

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
                         ["<", ">", "<", "<", ">", ">", ">"], max_rows=max(3, bottom_rows - 1))
        games_panel = ui.fill(ui.panel(f"WEEK {week} - GAMES",
                                       [games, C.dim(f"BYE: {byes or 'none'}")], right_w), bottom_h)

        top = ui.columns([movers_panel, feed_panel], [left_w, right_w])
        bottom = ui.columns([plays_panel, games_panel], [left_w, right_w])
        return self.screen("HOME", top, "", bottom)

    # ---------------- tabs ---------------------------------------------
    def cmd_TAB(self, args, opts):
        """TAB [n] | NEW <cmd> | CLOSE <n> | RENAME <n> <name>   several screens at once"""
        sub = args[0].upper() if args else ""

        # ---- TAB NEW <command...> --------------------------------------
        if sub == "NEW":
            if len(args) < 2:
                raise CommandError('TAB NEW <command>   e.g. TAB NEW ROSTER MYTEAM')
            command = " ".join(args[1:])
            name = opts.get("name") or self._tab_name(command)
            self.tabs.append([name, command])
            self.active_tab = len(self.tabs)
            return self._show_tab(self.active_tab)

        # ---- TAB CLOSE <n> ---------------------------------------------
        if sub in ("CLOSE", "DEL"):
            if len(self.tabs) <= 1:
                raise CommandError("cannot close the last tab")
            n = int(args[1]) if len(args) > 1 and is_number(args[1]) else self.active_tab
            if not 1 <= n <= len(self.tabs):
                raise CommandError(f"no tab {n}")
            gone = self.tabs.pop(n - 1)
            self.active_tab = min(self.active_tab, len(self.tabs))
            return C.amber(f"closed tab {n} ({gone[0]})")

        # ---- TAB RENAME <n> <name> -------------------------------------
        if sub == "RENAME":
            if len(args) < 3:
                raise CommandError("TAB RENAME <n> <name>")
            n = int(args[1])
            if not 1 <= n <= len(self.tabs):
                raise CommandError(f"no tab {n}")
            self.tabs[n - 1][0] = " ".join(args[2:]).upper()[:18]
            return C.amber(f"tab {n} renamed {self.tabs[n - 1][0]}")

        # ---- TAB <n>: switch -------------------------------------------
        if args and is_number(args[0]):
            n = int(args[0])
            if not 1 <= n <= len(self.tabs):
                raise CommandError(f"no tab {n} (you have {len(self.tabs)})")
            self.active_tab = n
            return self._show_tab(n)

        # ---- TAB: list --------------------------------------------------
        rows = []
        for i, (name, command) in enumerate(self.tabs, start=1):
            rows.append([C.amber(f"{i}") + (C.green(" *") if i == self.active_tab else "  "),
                         C.white(name), C.dim(command)])
        return self.screen("TABS", ui.panel(f"TABS - {len(self.tabs)} open", [
            ui.table(["#", "NAME", "SHOWING"], rows), "",
            C.dim("TAB 2 switches (or just T2)   TAB NEW <cmd> adds   TAB CLOSE <n> removes"),
            C.dim("Each tab re-runs its command when you switch to it, so the numbers are fresh.")]))

    def _tab_name(self, command: str) -> str:
        """Short label for a tab, taken from the command it runs."""
        parts = command.strip().split()
        if not parts:
            return "BLANK"
        head = parts[0].upper()
        rest = " ".join(parts[1:3]).upper()
        return (f"{head} {rest}".strip())[:18]

    def _show_tab(self, n: int) -> str:
        """Re-run the command a tab is showing, without recording it again."""
        name, command = self.tabs[n - 1]
        self._in_tab_switch = True
        try:
            out = self.run_line(command)
        finally:
            self._in_tab_switch = False
        return out or self.screen(name, ui.panel(name, [C.dim(f"'{command}' produced no screen")]))

    # ---------------- boards -------------------------------------------
    def cmd_TICKER(self, args, opts):
        """TICKER [POS] [--week N]       biggest movers board (season and this week)"""
        g = self.graph
        pos = args[0].upper() if args else None
        week = int(opts.get("week", self.week))
        rows_n = self.rows_opt(opts, panel_chrome=5)
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
        rows_n = self.rows_opt(opts, panel_chrome=4)
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
        t = ui.table(["TM", "TEAM", "VS AVG", "", "RATING", "BYE"], rows, ["<", "<", ">", "<", "<", "<"],
                     max_rows=self.rows_opt(opts, 4))
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
                                    [self._explain_table(n, limit=self.rows_opt(opts, 5))]))

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
        for (nid, wk), d in sorted(totals.items(), key=lambda kv: -abs(kv[1]))[:self.rows_opt(opts, 5)]:
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

    # ---------------- post-game recaps ---------------------------------
    def _find_game(self, want: str, date: Optional[str] = None):
        """Find a game by team abbreviation or ESPN id."""
        try:
            games = gamesmod.fetch_scoreboard(date)
        except feeds.FeedError as e:
            raise CommandError(str(e))
        for g in games:
            if want.upper() in (g.home, g.away, g.id):
                return g
        raise CommandError(f"no game for '{want}'" + (f" on {date}" if date else " today"))

    def cmd_SCORES(self, args, opts):
        """SCORES [--date YYYYMMDD]      every NFL game and its score"""
        try:
            games = gamesmod.fetch_scoreboard(opts.get("date"))
        except feeds.FeedError as e:
            raise CommandError(str(e))
        rows = []
        for gm in games:
            state = (C.green("LIVE") if gm.is_live else
                     C.white("FINAL") if gm.state == "post" else C.dim("UPCOMING"))
            rows.append([state, C.white(gm.away), f"{gm.away_score}", C.dim("@"),
                         C.white(gm.home), f"{gm.home_score}", C.dim(gm.detail[:30])])
        t = ui.table(["", "AWAY", "", "", "HOME", "", "WHEN"], rows,
                     ["<", "<", ">", "<", "<", ">", "<"])
        done = sum(1 for g in games if g.state == "post")
        return self.screen("SCORES", ui.panel(
            f"NFL SCOREBOARD - {done} of {len(games)} final",
            [t, "", C.dim("RECAP <team> tells you what happened in a finished game.")]))

    def cmd_RECAP(self, args, opts):
        """RECAP <team> [--date YYYYMMDD] [--ai]   what happened in a game and what it means"""
        if not args:
            raise CommandError("RECAP <team>   e.g. RECAP KC --date 20260104")
        game = self._find_game(args[0], opts.get("date"))
        if game.state == "pre":
            raise CommandError(f"{game.label} has not been played yet ({game.detail})")
        try:
            summary = gamesmod.fetch_summary(game.id)
        except feeds.FeedError as e:
            raise CommandError(str(e))
        r = gamesmod.build_recap(summary, game)
        g = self.graph
        week = self.week

        # --- headline block
        head = [
            ui.kv("RESULT", C.white(r.headline)) + "     " +
            ui.kv("STATUS", C.white(r.game.detail or r.game.state.upper())),
            ui.kv("FLOW", C.dim(gamesmod.script_summary(r.game))),
            ui.kv("PLAYS", f"{r.total_plays}") + "     " +
            ui.kv("SCORES", f"{len(r.scoring)}") + "     " +
            ui.kv("INJURIES", (C.red(str(len(r.injuries))) if r.injuries else "0")),
        ]

        # --- how it was scored
        srows = []
        for sp in r.scoring[:GAMES["RECAP_SCORES"]]:
            srows.append([C.dim(f"Q{sp['period']} {sp['clock']:>5}"), C.white(sp["team"]),
                          C.dim(sp["type"][:20]), sp["text"][:74],
                          C.dim(f"{sp['away_score']}-{sp['home_score']}")])
        scoring_panel = ui.panel("HOW IT WAS SCORED", [
            ui.table(["WHEN", "TM", "TYPE", "PLAY", "SCORE"], srows) if srows
            else C.dim("no scoring plays recorded")])

        # --- fantasy production, ours flagged
        prows = []
        for (name, team), rec in r.top_scorers(GAMES["RECAP_PLAYERS"]):
            node, _why = ingest.resolve_player(g, name, team)
            owned = rostermod.owners_of(self.rosters, node.id) if node else []
            pre = node.base_value if node else None
            actual = rec["points"]
            vs = (actual - pre) if pre is not None else None
            prows.append([
                C.white(name[:22]), team,
                node.slot if node else C.dim("-"),
                f"{pre:.1f}" if pre is not None else C.dim("-"),
                C.white(f"{actual:.1f}"),
                ui.fmt_chg(vs) if vs is not None else C.dim("-"),
                C.amber(",".join(owned)) if owned else "",
                C.dim(rec["line"][:40]),
            ])
        prod_panel = ui.panel("FANTASY PRODUCTION  (real points from real stats)", [
            ui.table(["PLAYER", "TM", "SLOT", "PROJ", "ACTUAL", "VS PROJ", "ROSTER", "STAT LINE"],
                     prows, ["<", "<", "<", ">", ">", ">", "<", "<"]),
            "", C.dim("PROJ is what we had him down for. VS PROJ is how the day actually went.")])

        blocks = [ui.panel(f"RECAP - {r.game.label}", head), scoring_panel, prod_panel]

        # --- injuries, and what they mean going forward
        if r.injuries:
            irows = []
            for inj in r.injuries:
                node = gamesmod.match_abbrev_name(g, inj["hint"], inj["team"])
                who = C.white(node.name) if node else C.dim(inj["hint"] + " (not tracked)")
                effect = ""
                if node is not None and inj["kind"] == "INJURED":
                    # Who benefits if he misses time?  Ask the graph.
                    ripples = g.propagate(node.id, -node.base_value, None, 0, "if he misses time")
                    gains = sorted(((c.path[-1], c.delta) for c in ripples if len(c.path) > 1),
                                   key=lambda kv: -kv[1])[:2]
                    if gains and gains[0][1] > 0.05:
                        effect = "watch " + ", ".join(g.nodes[i].name for i, d in gains if d > 0.05)
                irows.append([C.red("HURT") if inj["kind"] == "INJURED" else C.green("BACK"),
                              C.dim(inj["when"]), inj["team"], who, C.dim(effect)])
            blocks.append(ui.panel("INJURIES IN THIS GAME", [
                ui.table(["", "WHEN", "TM", "PLAYER", "IF HE MISSES TIME"], irows), "",
                C.dim("Nothing has been applied. Use NEWS ADD or FEED FETCH to put it on the board.")]))

        # --- optional written recap
        if "ai" in opts:
            story, err = self._ai_recap(r)
            blocks.append(ui.panel("WRITTEN RECAP",
                                   [story] if story else [C.red(err or "no recap returned")]))

        return self.screen(f"RECAP {r.game.label}", *blocks)

    def _ai_recap(self, recap) -> Tuple[Optional[str], Optional[str]]:
        """Ask Claude to write the game up in prose, from the facts we parsed.

        Everything it is given is real - the score, the scoring plays, the
        stat lines - so it is writing, not guessing. Returns (text, error).
        """
        try:
            import anthropic
        except ImportError:
            return None, "the 'anthropic' package is not installed - run: pip install anthropic"
        try:
            client = anthropic.Anthropic()
        except Exception as e:
            return None, f"no API key: {e}"
        facts = {
            "result": recap.headline,
            "flow": gamesmod.script_summary(recap.game),
            "scoring_plays": [f"Q{s['period']} {s['clock']} {s['team']}: {s['text']}"
                              for s in recap.scoring],
            "top_fantasy": [f"{n} ({t}) {v['points']:.1f} pts - {v['line']}"
                            for (n, t), v in recap.top_scorers(10)],
            "injuries": [f"{i['team']} {i['hint']} {i['kind'].lower()} at {i['when']}"
                         for i in recap.injuries],
        }
        try:
            resp = client.messages.create(
                model=GAMES["RECAP_MODEL"],
                max_tokens=1200,
                system=("You write short fantasy football game recaps. Use ONLY the facts given. "
                        "Three short paragraphs: what happened in the game, which fantasy players "
                        "won and lost the day, and what to watch next week. Name players and "
                        "numbers. No preamble, no headings, no speculation beyond the facts."),
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": json.dumps(facts, indent=1)}],
            )
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
        if getattr(resp, "stop_reason", None) == "refusal":
            return None, "the model declined to write this recap"
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        # wrap to the screen width so the panel stays tidy
        import textwrap
        width = ui.width() - 4
        out = []
        for para in text.split("\n"):
            out.extend(textwrap.wrap(para, width) or [""])
        return "\n".join(out), None

    # ---------------- your league: rosters -----------------------------
    def _roster(self, name: str) -> rostermod.Roster:
        key = name.strip().upper()
        if key not in self.rosters:
            raise CommandError(f"no roster '{key}' - ROSTER NEW {key} creates it")
        return self.rosters[key]

    def cmd_ROSTER(self, args, opts):
        """ROSTER NEW|ADD|DROP|DEL|<name>   set up your team, your opponent, a trade partner"""
        sub = args[0].upper() if args else "LIST"

        if sub == "NEW":
            if len(args) < 2:
                raise CommandError('ROSTER NEW <name> [--owner "Dave"]')
            key = args[1].upper()
            if key in self.rosters:
                raise CommandError(f"roster '{key}' already exists")
            self.rosters[key] = rostermod.Roster(name=key, owner=opts.get("owner", ""))
            return C.amber(f"created roster {key}" +
                           (f" (owner {opts['owner']})" if opts.get("owner") else "") +
                           f"  -  ROSTER ADD {key} <player> to fill it")

        if sub == "ADD":
            if len(args) < 3:
                raise CommandError('ROSTER ADD <name> <player>[, <player>...]')
            roster = self._roster(args[1])
            # everything after the roster name, split on commas so you can
            # paste a whole team in one line
            blob = " ".join(args[2:])
            added, failed = [], []
            for chunk in blob.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    node = self.resolve(chunk)
                except CommandError as e:
                    failed.append(f"{chunk}: {e}")
                    continue
                if node.id in roster.player_ids:
                    failed.append(f"{node.name}: already on {roster.name}")
                    continue
                roster.player_ids.append(node.id)
                added.append(node)
            lines = []
            if added:
                lines.append(C.green(f"added {len(added)}: ") +
                             ", ".join(f"{n.name} ({n.team} {n.slot})" for n in added))
            for f in failed:
                lines.append(C.red("skipped " + f))
            return "\n".join(lines) or C.dim("nothing added")

        if sub == "DROP":
            if len(args) < 3:
                raise CommandError("ROSTER DROP <name> <player>")
            roster = self._roster(args[1])
            node = self.resolve(" ".join(args[2:]))
            if node.id not in roster.player_ids:
                raise CommandError(f"{node.name} is not on {roster.name}")
            roster.player_ids.remove(node.id)
            return C.amber(f"dropped {node.name} from {roster.name}")

        if sub in ("DEL", "DELETE"):
            if len(args) < 2:
                raise CommandError("ROSTER DEL <name>")
            key = args[1].upper()
            if key not in self.rosters:
                raise CommandError(f"no roster '{key}'")
            del self.rosters[key]
            return C.amber(f"deleted roster {key}")

        if sub == "LIST" and len(args) < 2:
            if not self.rosters:
                return self.screen("ROSTER", ui.panel("YOUR LEAGUE", [
                    C.dim("no rosters yet."),
                    "",
                    C.white('ROSTER NEW MYTEAM --owner "me"'),
                    C.white('ROSTER ADD MYTEAM josh allen, jahmyr gibbs, ja\'marr chase'),
                    C.white("ROSTER MYTEAM              see the lineup"),
                    C.white("MATCHUP MYTEAM OPPONENT    head to head"),
                    C.white("TRADE MYTEAM kelce FOR THEIRS gibbs"),
                ]))
            rows = []
            for r in self.rosters.values():
                starters, bench, total = rostermod.best_lineup(self.graph, r, self.week)
                chg = rostermod.roster_change(self.graph, r, self.week)
                rows.append([C.white(r.name), C.dim(r.owner or "-"), f"{len(r.player_ids)}",
                             C.white(f"{total:.1f}"), ui.fmt_chg(chg),
                             C.dim(", ".join(n.name for _s, n, _v in starters[:3] if n))])
            return self.screen("ROSTER", ui.panel(
                f"YOUR LEAGUE - week {self.week}",
                [ui.table(["ROSTER", "OWNER", "PLAYERS", "PROJ", "NEWS CHG", "TOP STARTERS"], rows,
                          ["<", "<", ">", ">", ">", "<"]), "",
                 C.dim("ROSTER <name> for the lineup   MATCHUP <a> <b> for head to head")]))

        # ---- ROSTER <name>: show one team's lineup -----------------------
        name = args[1] if sub == "LIST" else args[0]
        roster = self._roster(name)
        starters, bench, total = rostermod.best_lineup(self.graph, roster, self.week)
        srows = []
        for slot, n, v in starters:
            if n is None:
                srows.append([C.amber(slot), C.red("(empty)"), "", "", "", "", ""])
                continue
            opp = S.opponent(self.graph, n, self.week)
            team = self.graph.teams.get(n.team)
            ha = "vs" if team and team.home.get(self.week) else "@"
            srows.append([C.amber(slot), C.white(n.name), n.team, n.slot,
                          f"{ha} {opp}" if opp else C.dim("BYE"),
                          C.white(f"{v:.1f}"),
                          ui.fmt_chg(S.week_change(n, self.week)),
                          ui.fmt_chg(S.matchup_adjustment(self.graph, n, self.week)),
                          C.red(n.status) if n.status != "ACTIVE" else ""])
        brows = []
        for n, v in bench:
            brows.append([C.dim("BN"), n.name, n.team, n.slot, "",
                          f"{v:.1f}", ui.fmt_chg(S.week_change(n, self.week)),
                          ui.fmt_chg(S.matchup_adjustment(self.graph, n, self.week)),
                          C.red(n.status) if n.status != "ACTIVE" else ""])
        cols = ["SLOT", "PLAYER", "TM", "POS", "OPP", "PROJ", "NEWS", "MATCHUP", "STATUS"]
        blocks = [ui.panel(
            f"{roster.name}" + (f"  ({roster.owner})" if roster.owner else "") +
            f"  -  WEEK {self.week} STARTERS: {total:.1f}",
            [ui.table(cols, srows, ["<", "<", "<", "<", "<", ">", ">", ">", "<"])])]
        if brows:
            blocks.append(ui.panel("BENCH", [ui.table(cols, brows,
                                                      ["<", "<", "<", "<", "<", ">", ">", ">", "<"])]))
        return self.screen(f"ROSTER {roster.name}", *blocks)

    def cmd_MATCHUP(self, args, opts):
        """MATCHUP <a> <b> [--week N]    head to head between two rosters, slot by slot"""
        if len(args) < 2:
            raise CommandError("MATCHUP <a> <b>   e.g. MATCHUP MYTEAM OPPONENT")
        week = int(opts.get("week", self.week))
        a, b = self._roster(args[0]), self._roster(args[1])
        c = rostermod.compare(self.graph, a, b, week)
        rows = []
        for row in c["rows"]:
            an, bn = row["a"], row["b"]
            edge = row["edge"]
            rows.append([
                C.amber(row["slot"]),
                C.white(an.name[:20]) if an else C.dim("(empty)"),
                f"{row['a_pts']:.1f}",
                (C.green("◀") if edge > 0.5 else C.red("▶") if edge < -0.5 else C.dim("=")),
                f"{row['b_pts']:.1f}",
                C.white(bn.name[:20]) if bn else C.dim("(empty)"),
                ui.fmt_chg(edge),
            ])
        margin = c["margin"]
        verdict = (C.green(f"{a.name} favoured by {margin:.1f}") if margin > 0
                   else C.red(f"{b.name} favoured by {abs(margin):.1f}") if margin < 0
                   else C.dim("dead level"))
        head = [ui.kv(a.name, C.white(f"{c['a_total']:.1f}")) + "     " +
                ui.kv(b.name, C.white(f"{c['b_total']:.1f}")) + "     " +
                ui.kv("MARGIN", verdict)]
        t = ui.table(["SLOT", a.name[:20], "PTS", "", "PTS", b.name[:20], "EDGE"], rows,
                     ["<", "<", ">", "^", "<", "<", ">"])
        biggest = max(c["rows"], key=lambda r: abs(r["edge"]))
        note = C.dim(f"biggest swing: {biggest['slot']} "
                     f"({biggest['a'].name if biggest['a'] else '-'} vs "
                     f"{biggest['b'].name if biggest['b'] else '-'}) "
                     f"worth {abs(biggest['edge']):.1f}")
        return self.screen(f"MATCHUP {a.name} v {b.name}",
                           ui.panel(f"WEEK {week} HEAD TO HEAD", head + ["", t, "", note]))

    def cmd_TRADE(self, args, opts):
        """TRADE <a> <players> FOR <b> <players>   price a trade for both sides"""
        upper = [a.upper() for a in args]
        if "FOR" not in upper:
            raise CommandError('TRADE MYTEAM "travis kelce" FOR THEIRS "jahmyr gibbs"')
        i = upper.index("FOR")
        left, right = args[:i], args[i + 1:]
        if len(left) < 2 or len(right) < 2:
            raise CommandError("each side needs a roster name and at least one player")
        a = self._roster(left[0])
        b = self._roster(right[0])

        def names_to_nodes(chunks, roster):
            nodes = []
            for chunk in " ".join(chunks).split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                n = self.resolve(chunk)
                if n.id not in roster.player_ids:
                    raise CommandError(f"{n.name} is not on {roster.name}")
                nodes.append(n)
            return nodes

        a_sends = names_to_nodes(left[1:], a)
        b_sends = names_to_nodes(right[1:], b)
        week = int(opts.get("week", self.week))
        r = rostermod.evaluate_trade(self.graph, a, a_sends, b, b_sends, week)

        rows = []
        for side, sends, gets, before, after, delta in (
                (a, a_sends, b_sends, r["a_before"], r["a_after"], r["a_delta"]),
                (b, b_sends, a_sends, r["b_before"], r["b_after"], r["b_delta"])):
            rows.append([
                C.white(side.name),
                C.red("- " + ", ".join(n.name for n in sends)),
                C.green("+ " + ", ".join(n.name for n in gets)),
                f"{before:.1f}", f"{after:.1f}", ui.fmt_chg(delta),
                (C.green("BETTER") if delta > 0.2 else C.red("WORSE") if delta < -0.2 else C.dim("EVEN")),
            ])
        t = ui.table(["ROSTER", "GIVES", "GETS", "BEFORE", "AFTER", "CHANGE", "VERDICT"], rows,
                     ["<", "<", "<", ">", ">", ">", "<"])
        # a trade can help both sides, because lineups have slots
        both = r["a_delta"] > 0.2 and r["b_delta"] > 0.2
        note = (C.green("both sides improve their starting lineup - this is the rare fair trade")
                if both else
                C.dim("a trade can help both teams when it fills a slot each side was weak at"))
        return self.screen("TRADE", ui.panel(
            f"TRADE EVALUATION - WEEK {week} STARTING LINEUPS", [t, "", note, "",
            C.dim("Nothing has been moved. This prices the swap only.")]))

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
                "prop_overrides": self.prop_overrides,
                "actions": [a.to_dict() for a in self.actions],
                "rosters": {k: r.to_dict() for k, r in self.rosters.items()}}
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
        self.rosters = {k: rostermod.Roster.from_dict(v)
                        for k, v in (data.get("rosters") or {}).items()}
        self.rebuild()
        return C.amber(f"loaded {len(self.actions)} actions and "
                       f"{len(self.rosters)} rosters from {path}")

    def cmd_RESET(self, args, opts):
        """RESET                         wipe news, links and weight changes"""
        self.actions, self.weight_overrides, self.prop_overrides = [], {}, {}
        self.next_action_id = 1
        self.rebuild()
        return C.amber("reset to base projections (rosters kept - ROSTER DEL removes those)")
