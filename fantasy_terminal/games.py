"""
games.py - REAL GAME RESULTS AND POST-GAME RECAPS
=================================================

Fantasy lineups lock before kickoff, so nothing here tries to react during a
game.  What this file does is read a game AFTERWARDS and tell you what
happened and what it means.

Data source: ESPN's public JSON (no API key, no signup).
    scoreboard   .../football/nfl/scoreboard          scores and game state
    summary      .../football/nfl/summary?event=<id>  box score + play by play

    fetch_scoreboard()     -> list of Game (score, clock, final/upcoming)
    fetch_summary(id)      -> the raw game JSON
    parse_game_state()     -> score, quarter, whether it is over
    parse_box_score()      -> real fantasy points per player, from real stats
    parse_plays()          -> every play, oldest first
    parse_scoring_plays()  -> just the scores, in order
    find_injuries()        -> players hurt during the game, from the play text
    build_recap()          -> everything above, assembled into one Recap

A Recap holds the facts.  terminal.py renders them, and can optionally ask
Claude to write the story in prose (RECAP ... --ai).

To change the fantasy scoring used for the "real points" numbers, edit
config.SCORING (full PPR by default).
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .config import GAMES, SCORING
from .feeds import USER_AGENT, FeedError, _get

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

# ---------------------------------------------------------------------------
# GAME + EVENT CONTAINERS
# ---------------------------------------------------------------------------
@dataclass
class Game:
    """One NFL game and where it currently stands."""
    id: str
    home: str
    away: str
    home_score: int = 0
    away_score: int = 0
    period: int = 0
    clock: str = ""
    state: str = "pre"          # pre | in | post
    detail: str = ""            # "Final", "8:20 PM EDT", "3rd 4:21"

    @property
    def label(self) -> str:
        return f"{self.away}@{self.home}"

    @property
    def is_live(self) -> bool:
        return self.state == "in"

    def remaining_fraction(self) -> float:
        """How much of the game is still to be played, 0.0 to 1.0.

        Used to split a player's weekly projection into the part he has
        already had a chance to earn and the part still ahead of him.
        """
        if self.state == "pre":
            return 1.0
        if self.state == "post":
            return 0.0
        # A regulation game is 4 periods of 15 minutes = 3600 seconds.
        secs = clock_to_seconds(self.clock)
        if self.period >= 5:                       # overtime: treat as nearly over
            return max(0.0, min(1.0, secs / 3600.0))
        remaining = (4 - self.period) * 900 + secs
        return max(0.0, min(1.0, remaining / 3600.0))

    def margin(self, team: str) -> int:
        """Score differential from `team`'s point of view (negative = losing)."""
        if team == self.home:
            return self.home_score - self.away_score
        if team == self.away:
            return self.away_score - self.home_score
        return 0


def clock_to_seconds(clock: str) -> float:
    """'4:21' -> 261.0 seconds.  Bad input returns 0."""
    try:
        parts = clock.strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return float(parts[0])
    except (ValueError, AttributeError, IndexError):
        return 0.0


# ---------------------------------------------------------------------------
# FETCHING
# ---------------------------------------------------------------------------
def fetch_scoreboard(date: Optional[str] = None) -> List[Game]:
    """Every game for today (or for `date`, formatted YYYYMMDD)."""
    url = f"{ESPN_BASE}/scoreboard"
    if date:
        url += "?" + urllib.parse.urlencode({"dates": date})
    data = json.loads(_get(url, timeout=GAMES["TIMEOUT"]))
    games = []
    for e in data.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        status = e.get("status", {})
        stype = status.get("type", {})
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in sides or "away" not in sides:
            continue
        games.append(Game(
            id=str(e.get("id")),
            home=sides["home"]["team"]["abbreviation"],
            away=sides["away"]["team"]["abbreviation"],
            home_score=int(sides["home"].get("score") or 0),
            away_score=int(sides["away"].get("score") or 0),
            period=int(status.get("period") or 0),
            clock=str(status.get("displayClock") or ""),
            state=stype.get("state", "pre"),
            detail=stype.get("shortDetail", ""),
        ))
    return games


def fetch_summary(event_id: str) -> dict:
    """The full game JSON: box score, drives, plays, scoring plays."""
    url = f"{ESPN_BASE}/summary?" + urllib.parse.urlencode({"event": event_id})
    return json.loads(_get(url, timeout=GAMES["TIMEOUT"]))


# ---------------------------------------------------------------------------
# PARSING: game state
# ---------------------------------------------------------------------------
def parse_game_state(summary: dict, fallback: Optional[Game] = None) -> Optional[Game]:
    """Pull the Game out of a summary payload."""
    header = summary.get("header", {})
    comps = header.get("competitions") or []
    if not comps:
        return fallback
    comp = comps[0]
    status = comp.get("status", {})
    stype = status.get("type", {})
    sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    if "home" not in sides or "away" not in sides:
        return fallback
    return Game(
        id=str(header.get("id") or (fallback.id if fallback else "")),
        home=sides["home"]["team"]["abbreviation"],
        away=sides["away"]["team"]["abbreviation"],
        home_score=int(sides["home"].get("score") or 0),
        away_score=int(sides["away"].get("score") or 0),
        period=int(status.get("period") or 0),
        clock=str(status.get("displayClock") or ""),
        state=stype.get("state", "pre"),
        detail=stype.get("shortDetail", ""),
    )


# ---------------------------------------------------------------------------
# PARSING: box score -> real fantasy points
# ---------------------------------------------------------------------------
def _stat(values: List[str], labels: List[str], key: str, default: float = 0.0) -> float:
    """Read one labelled stat, e.g. _stat(row, labels, 'YDS')."""
    try:
        raw = values[labels.index(key)]
    except (ValueError, IndexError):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _split_pair(values: List[str], labels: List[str], key: str) -> Tuple[float, float]:
    """Read a made/attempted stat like '2/3' or a sack stat like '4-27'."""
    try:
        raw = values[labels.index(key)]
    except (ValueError, IndexError):
        return 0.0, 0.0
    for sep in ("/", "-"):
        if sep in str(raw):
            a, _, b = str(raw).partition(sep)
            try:
                return float(a), float(b)
            except ValueError:
                return 0.0, 0.0
    return 0.0, 0.0


def parse_box_score(summary: dict) -> Dict[Tuple[str, str], dict]:
    """Real fantasy points for every player who has done anything.

    Returns {(PLAYER NAME UPPER, TEAM): {"points": float, "line": "human summary"}}
    Scoring rules come from config.SCORING, so change them there for your
    league (standard vs PPR vs half-PPR).
    """
    out: Dict[Tuple[str, str], dict] = {}

    def bump(name: str, team: str, pts: float, piece: str):
        key = (name.upper(), team)
        rec = out.setdefault(key, {"points": 0.0, "pieces": []})
        rec["points"] += pts
        if piece:
            rec["pieces"].append(piece)

    for team_block in summary.get("boxscore", {}).get("players", []):
        team = team_block.get("team", {}).get("abbreviation", "")
        for group in team_block.get("statistics", []):
            gname = group.get("name")
            labels = group.get("labels") or []
            for a in group.get("athletes", []):
                name = a.get("athlete", {}).get("displayName") or ""
                vals = a.get("stats") or []
                if not name or not vals:
                    continue
                if gname == "passing":
                    yds = _stat(vals, labels, "YDS")
                    td = _stat(vals, labels, "TD")
                    ints = _stat(vals, labels, "INT")
                    pts = yds * SCORING["PASS_YD"] + td * SCORING["PASS_TD"] + ints * SCORING["INT"]
                    bump(name, team, pts, f"{yds:.0f} pass yd, {td:.0f} pass TD")
                elif gname == "rushing":
                    yds = _stat(vals, labels, "YDS")
                    td = _stat(vals, labels, "TD")
                    car = _stat(vals, labels, "CAR")
                    pts = yds * SCORING["RUSH_YD"] + td * SCORING["RUSH_TD"]
                    bump(name, team, pts, f"{car:.0f} car {yds:.0f} yd, {td:.0f} TD")
                elif gname == "receiving":
                    rec = _stat(vals, labels, "REC")
                    yds = _stat(vals, labels, "YDS")
                    td = _stat(vals, labels, "TD")
                    pts = rec * SCORING["REC"] + yds * SCORING["REC_YD"] + td * SCORING["REC_TD"]
                    bump(name, team, pts, f"{rec:.0f} rec {yds:.0f} yd, {td:.0f} TD")
                elif gname == "fumbles":
                    lost = _stat(vals, labels, "LOST")
                    if lost:
                        bump(name, team, lost * SCORING["FUMBLE_LOST"], f"{lost:.0f} fum lost")
                elif gname == "kicking":
                    fg_made, _ = _split_pair(vals, labels, "FG")
                    xp_made, _ = _split_pair(vals, labels, "XP")
                    pts = fg_made * SCORING["FG"] + xp_made * SCORING["XP"]
                    bump(name, team, pts, f"{fg_made:.0f} FG, {xp_made:.0f} XP")
    for rec in out.values():
        rec["line"] = "; ".join(rec.pop("pieces")[:3])
    return out


# ---------------------------------------------------------------------------
# PARSING: plays
# ---------------------------------------------------------------------------
def parse_plays(summary: dict) -> List[dict]:
    """Flatten every play in the game, oldest first."""
    plays = []
    drives = summary.get("drives", {})
    for bucket in ("previous", "current"):
        item = drives.get(bucket)
        if item is None:
            continue
        drive_list = item if isinstance(item, list) else [item]
        for drive in drive_list:
            team = (drive.get("team") or {}).get("abbreviation", "")
            for p in drive.get("plays", []) or []:
                plays.append({
                    "id": str(p.get("id")),
                    "text": p.get("text") or "",
                    "type": (p.get("type") or {}).get("text", ""),
                    "period": int((p.get("period") or {}).get("number") or 0),
                    "clock": (p.get("clock") or {}).get("displayValue", ""),
                    "scoring": bool(p.get("scoringPlay")),
                    "turnover": bool(p.get("isTurnover")),
                    "team": team,
                    "away_score": p.get("awayScore"),
                    "home_score": p.get("homeScore"),
                })
    return plays


# ---------------------------------------------------------------------------
# WHAT HAPPENED: scoring plays and injuries
# ---------------------------------------------------------------------------
# ESPN writes injuries into the play text in a consistent shape:
#   "... ATL-C.Bryant was injured during the play."
#   "** Injury Update: ATL-K.Kareem has returned to the game."
INJURY_RE = re.compile(r"\b([A-Z]{2,3})-([A-Z]\.[A-Za-z'\-]+)\s+was injured", re.I)
RETURN_RE = re.compile(r"\b([A-Z]{2,3})-([A-Z]\.[A-Za-z'\-]+)\s+has returned to the game", re.I)


def parse_scoring_plays(summary: dict) -> List[dict]:
    """Every touchdown and field goal, in the order they happened."""
    out = []
    for s in summary.get("scoringPlays", []) or []:
        out.append({
            "text": s.get("text", ""),
            "type": (s.get("type") or {}).get("text", ""),
            "team": ((s.get("team") or {}).get("abbreviation")
                     or (s.get("team") or {}).get("displayName", "")),
            "period": int((s.get("period") or {}).get("number") or 0),
            "clock": (s.get("clock") or {}).get("displayValue", ""),
            "away_score": s.get("awayScore"),
            "home_score": s.get("homeScore"),
        })
    return out


def find_injuries(plays: List[dict]) -> List[dict]:
    """Players hurt (or returning) during the game, read out of the play text.

    Returns {kind, team, hint, when, text} where `hint` is ESPN's abbreviated
    name, e.g. "K.Banks" - match_abbrev_name() turns that into one of our nodes.
    """
    out = []
    for p in plays:
        for kind, rx in (("INJURED", INJURY_RE), ("RETURNED", RETURN_RE)):
            for m in rx.finditer(p["text"]):
                out.append({
                    "kind": kind, "team": m.group(1).upper(), "hint": m.group(2),
                    "when": f"Q{p['period']} {p['clock']}", "text": p["text"],
                })
    return out


def script_summary(game: Game) -> str:
    """One line describing how the game flowed, for the recap."""
    gap = abs(game.home_score - game.away_score)
    winner = game.home if game.home_score > game.away_score else game.away
    if game.home_score == game.away_score:
        return "ended level"
    if gap >= GAMES["SCRIPT_MIN_MARGIN"] * 2:
        return f"{winner} in control - a blowout script, so expect pass-heavy garbage time from the loser"
    if gap >= GAMES["SCRIPT_MIN_MARGIN"]:
        return f"{winner} comfortable - the trailing offense had to throw late"
    return "close throughout - a neutral script that favoured neither run nor pass"


@dataclass
class Recap:
    """Everything worth knowing about a finished game."""
    game: Game
    scoring: List[dict] = field(default_factory=list)
    injuries: List[dict] = field(default_factory=list)
    box: Dict[Tuple[str, str], dict] = field(default_factory=dict)
    plays: List[dict] = field(default_factory=list)
    total_plays: int = 0

    @property
    def headline(self) -> str:
        g = self.game
        if g.home_score == g.away_score:
            return f"{g.away} and {g.home} tied {g.away_score}-{g.home_score}"
        win, lose = ((g.home, g.away) if g.home_score > g.away_score else (g.away, g.home))
        hi, lo = max(g.home_score, g.away_score), min(g.home_score, g.away_score)
        return f"{win} beat {lose} {hi}-{lo}"

    def top_scorers(self, limit: int = 10) -> List[Tuple[Tuple[str, str], dict]]:
        return sorted(self.box.items(), key=lambda kv: -kv[1]["points"])[:limit]


def build_recap(summary: dict, fallback: Optional[Game] = None) -> Recap:
    """Assemble a Recap from one game's summary payload."""
    game = parse_game_state(summary, fallback)
    plays = parse_plays(summary)
    return Recap(
        game=game,
        scoring=parse_scoring_plays(summary),
        injuries=find_injuries(plays),
        box=parse_box_score(summary),
        plays=plays,
        total_plays=len(plays),
    )


# ---------------------------------------------------------------------------
# MATCHING ESPN's ABBREVIATED NAMES TO OUR ROSTER
# ---------------------------------------------------------------------------
def match_abbrev_name(graph, hint: str, team: str):
    """Turn 'K.Banks' + 'NO' into one of our nodes, or None.

    ESPN abbreviates first names in play text.  We require the TEAM to match
    and the last name to match, and we refuse when two players on the same
    team would both fit - guessing the wrong player is worse than missing one.
    """
    if not hint or "." not in hint:
        return None
    initial, _, last = hint.partition(".")
    initial, last = initial.strip().upper(), last.strip().upper()
    hits = []
    for n in graph.nodes.values():
        if n.team != team.upper() or n.pos in ("OFF", "OLUNIT", "DST"):
            continue
        parts = n.name.upper().replace("'", "").split()
        if not parts:
            continue
        surname = parts[-1]
        if surname in ("JR.", "SR.", "II", "III", "IV") and len(parts) > 1:
            surname = parts[-2]
        if surname.rstrip(".") == last.replace("'", "") and parts[0].startswith(initial):
            hits.append(n)
    return hits[0] if len(hits) == 1 else None
