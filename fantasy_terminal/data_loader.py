"""
data_loader.py - READ THE CSV FILES INTO A GRAPH
================================================

    g = load_graph()            # uses the paths in config.FILES
    g = load_graph(files={...}) # or your own paths

CSV formats (edit these files in Excel / a text editor to change the data):

data/teams.csv
    abbr,name,division,off_rating,ol_grade,def_strength
    off_rating  ~ team points per game / 3   (9.5 = elite offense)
    ol_grade    0-10 grade for the whole line
    def_strength 1-10, only used by tools/make_sample_data.py

data/players.csv
    name,team,pos,depth,last_ppg,games,proj_ppg,note
    pos    QB RB WR TE K OL
    depth  1 = starter, 2 = next on the depth chart ...
    For OL rows the "points" columns are a 0-10 grade instead.

data/defenses.csv
    team,vs_qb,vs_rb,vs_wr,vs_te,vs_k,dst_ppg
    vs_XX   fantasy points per game the defense allowed to that position
    dst_ppg the DST's own fantasy points per game

data/schedule.csv
    week,home,away
    The bye week is inferred: any week 1-18 with no game.
"""

import csv
import re
from typing import Dict, Optional

from .config import FILES, STATS
from .graph import ImpactGraph
from .models import Node, Team
from .stats import DefenseProfile


def slug(text: str) -> str:
    """'Ja'Marr Chase' -> 'JAMARR_CHASE' (used for node ids)."""
    return re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")


def _read(path: str):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_graph(files: Optional[Dict[str, str]] = None, weights=None, propagation=None) -> ImpactGraph:
    files = {**FILES, **(files or {})}
    g = ImpactGraph(weights=weights, propagation=propagation)
    g.defense_profiles: Dict[str, DefenseProfile] = {}

    # ---- teams --------------------------------------------------------------
    for r in _read(files["teams"]):
        g.add_team(Team(abbr=r["abbr"].strip().upper(), name=r["name"], division=r.get("division", ""),
                        bye_week=0, off_rating=_num(r.get("off_rating"), 7.5),
                        ol_grade=_num(r.get("ol_grade"), 6.5)))

    # ---- schedule -----------------------------------------------------------
    weeks_in_season = int(STATS["WEEKS_IN_SEASON"])
    for r in _read(files["schedule"]):
        wk = int(r["week"])
        home, away = r["home"].strip().upper(), r["away"].strip().upper()
        if home in g.teams and away in g.teams:
            g.teams[home].schedule[wk] = away
            g.teams[home].home[wk] = True
            g.teams[away].schedule[wk] = home
            g.teams[away].home[wk] = False
    for t in g.teams.values():
        off_weeks = [w for w in range(1, weeks_in_season + 1) if w not in t.schedule]
        t.bye_week = off_weeks[0] if off_weeks else 0

    # ---- defenses -> DST nodes + matchup profiles ----------------------------
    for r in _read(files["defenses"]):
        abbr = r["team"].strip().upper()
        if abbr not in g.teams:
            continue
        g.defense_profiles[abbr] = DefenseProfile(abbr, {
            "QB": _num(r.get("vs_qb")), "RB": _num(r.get("vs_rb")), "WR": _num(r.get("vs_wr")),
            "TE": _num(r.get("vs_te")), "K": _num(r.get("vs_k")),
        })
        ppg = _num(r.get("dst_ppg"), 7.0)
        g.add_node(Node(id=g.teams[abbr].dst_node_id, name=f"{abbr} DST", team=abbr, pos="DST",
                        base_value=ppg, last_year_ppg=ppg, last_year_total=round(ppg * 17, 1),
                        games_played=17, note="Team defense / special teams"))

    # ---- players -------------------------------------------------------------
    for r in _read(files["players"]):
        team = r["team"].strip().upper()
        pos = r["pos"].strip().upper()
        nid = f"{pos}_{slug(r['name'])}"
        while nid in g.nodes:               # two players with the same name
            nid += "_X"
        games = int(_num(r.get("games"), 0))
        ppg = _num(r.get("last_ppg"))
        g.add_node(Node(id=nid, name=r["name"].strip(), team=team, pos=pos,
                        depth=int(_num(r.get("depth"), 1)), base_value=_num(r.get("proj_ppg"), ppg),
                        last_year_ppg=ppg, last_year_total=round(ppg * games, 1),
                        games_played=games, note=r.get("note", "") or ""))

    # ---- build every standard connection ------------------------------------
    g.wire_all()
    return g
