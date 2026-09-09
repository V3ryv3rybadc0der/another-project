"""
ingest.py - TURNING REAL NEWS INTO TERMINAL COMMANDS
====================================================

feeds.py downloads news.  This file decides WHAT IT MEANS and produces
`Proposal` objects - suggested `NEWS ADD ...` commands with a confidence
score and the headline they came from.

Nothing here touches the graph.  Proposals go into a REVIEW QUEUE and are
only applied when you type `FEED APPLY`.  That is deliberate: one bad parse
would silently poison every projection on the board, and a wrong number you
cannot see is worse than no number at all.

Two interpretation paths:

  1. RULE-BASED (match_sleeper_injuries)  - no AI, no API key, free.
     Sleeper's data is ALREADY structured ("Questionable", "IR", "Out"), so
     a lookup table is all it takes.  This handles the bulk of real news:
     injury designations for every player in the league.

  2. AI EXTRACTION (extract_with_claude)  - optional, needs an API key.
     RSS headlines are free text: "Patriots RB Henderson out for opener"
     needs a reader who knows that is TreVeyon Henderson, a running back on
     New England, and that "out for opener" means OUT in week 1.  A regex
     cannot do that.  A language model can.  If no API key is configured
     this path is skipped and the rule-based path still works.

NAME MATCHING is the dangerous part and is handled by resolve_player().
"Josh Allen" is BOTH a Bills quarterback and a linebacker; Sleeper lists
both.  So a name match is only accepted when the TEAM and POSITION also
agree.  Wrong-player matches are silently wrong, so the rule is strict.
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from .config import INGEST
from .models import Node

# ---------------------------------------------------------------------------
# HOW SLEEPER'S INJURY TAGS MAP TO OUR NEWS TYPES
# ---------------------------------------------------------------------------
# Edit this table to change what each designation does.
#   news_type  - one of the types in news.py
#   scope      - "week"   applies to the current week only
#                "season" applies to the whole season
#   confidence - 0..1, shown in the review queue; below INGEST["AUTO_MIN_CONFIDENCE"]
#                the proposal is flagged for a closer look
SLEEPER_STATUS_MAP = {
    "Out":          {"news_type": "OUT",          "scope": "week",   "confidence": 0.95},
    "Doubtful":     {"news_type": "DOUBTFUL",     "scope": "week",   "confidence": 0.90},
    "Questionable": {"news_type": "QUESTIONABLE", "scope": "week",   "confidence": 0.85},
    "IR":           {"news_type": "OUT",          "scope": "season", "confidence": 0.95},
    "PUP":          {"news_type": "OUT",          "scope": "season", "confidence": 0.90},
    "Sus":          {"news_type": "SUSPENDED",    "scope": "season", "confidence": 0.85},
    "COV":          {"news_type": "OUT",          "scope": "week",   "confidence": 0.80},
    # "NA" (not active) and "DNR" (did not report) are too vague to act on.
    # Add them here if you want them proposed anyway.
}


@dataclass
class Proposal:
    """One suggested change, waiting for you to approve it.

    command     the exact terminal line that will run if you apply it
    node_id     which node it targets (already resolved and verified)
    confidence  0..1
    headline    the source text, so you can judge the parse yourself
    source      where it came from ("Sleeper" / "ESPN NFL" / ...)
    origin      "rule" or "ai" - which path produced it
    reason      short explanation of the interpretation
    """
    id: int
    command: str
    node_id: str
    node_name: str
    team: str
    news_type: str
    confidence: float
    headline: str
    source: str
    origin: str
    reason: str = ""
    applied: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# NAME MATCHING
# ---------------------------------------------------------------------------
_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def normalize_name(name: str) -> str:
    """'Ja'Marr Chase' / 'Michael Pittman Jr.' -> 'JAMARR CHASE' / 'MICHAEL PITTMAN'.

    Strips accents, punctuation and generational suffixes so that the same
    human matches across feeds that spell them differently.
    """
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[^A-Za-z ]+", "", n).upper()
    words = [w for w in n.split() if w not in _SUFFIXES]
    return " ".join(words)


def build_name_index(graph) -> Dict[str, List[Node]]:
    """Map normalized name -> the node(s) in our graph with that name."""
    idx: Dict[str, List[Node]] = {}
    for n in graph.nodes.values():
        if n.pos in ("OFF", "OLUNIT", "DST"):
            continue
        idx.setdefault(normalize_name(n.name), []).append(n)
    return idx


def resolve_player(graph, name: str, team: Optional[str] = None, pos: Optional[str] = None,
                   index: Optional[dict] = None) -> Tuple[Optional[Node], str]:
    """Find the ONE node this news is about, or explain why we could not.

    Returns (node, reason).  node is None when we refuse to guess.

    Safety rules, in order:
      1. name must match after normalization
      2. if several players share the name, TEAM must disambiguate
         (this is what stops Josh Allen the QB being confused with
          Josh Allen the linebacker)
      3. if a position is supplied and disagrees, we refuse
      4. if a team is supplied and disagrees, we refuse - the player
         probably moved, which is different news than an injury
    """
    index = index if index is not None else build_name_index(graph)
    key = normalize_name(name)
    candidates = index.get(key, [])
    if not candidates:
        return None, "not on our roster"
    if len(candidates) > 1:
        if team:
            candidates = [c for c in candidates if c.team == team.upper()]
        if len(candidates) != 1:
            return None, f"ambiguous name ({len(candidates)} matches)"
    node = candidates[0]
    if pos and node.pos != pos.upper():
        return None, f"position mismatch (ours {node.pos}, feed {pos})"
    if team and node.team != team.upper():
        return None, f"team mismatch (ours {node.team}, feed {team})"
    return node, "matched"


# ---------------------------------------------------------------------------
# PATH 1: RULE-BASED (Sleeper injury report - no AI needed)
# ---------------------------------------------------------------------------
def match_sleeper_injuries(graph, injuries: List[dict], week: int,
                           start_id: int = 1) -> Tuple[List[Proposal], List[dict]]:
    """Turn Sleeper's injury rows into Proposals.

    Returns (proposals, skipped) where `skipped` explains every row we
    refused to act on - useful for spotting roster gaps.
    """
    index = build_name_index(graph)
    proposals, skipped = [], []
    next_id = start_id
    for row in injuries:
        rule = SLEEPER_STATUS_MAP.get(row["status"])
        if rule is None:
            skipped.append({**row, "why": f"status '{row['status']}' not actionable"})
            continue
        node, why = resolve_player(graph, row["name"], row["team"], row["pos"], index)
        if node is None:
            skipped.append({**row, "why": why})
            continue
        # Build the exact terminal command this proposal represents.
        weeks = "" if rule["scope"] == "season" else f" --weeks {week}"
        body = row["body_part"] or "injury"
        note = f'{row["status"]} - {body}'
        command = f'NEWS ADD "{node.name}" {rule["news_type"]}{weeks} --note "{note}"'
        proposals.append(Proposal(
            id=next_id, command=command, node_id=node.id, node_name=node.name,
            team=node.team, news_type=rule["news_type"], confidence=rule["confidence"],
            headline=f'{row["name"]} ({row["team"]} {row["pos"]}) listed {row["status"]} - {body}',
            source="Sleeper", origin="rule",
            reason=f'Sleeper status "{row["status"]}" maps to {rule["news_type"]} ({rule["scope"]})',
        ))
        next_id += 1
    return proposals, skipped


# ---------------------------------------------------------------------------
# PATH 2: AI EXTRACTION (RSS headlines -> commands)
# ---------------------------------------------------------------------------
# The JSON shape we force the model to return.  Because we use structured
# outputs, the response is guaranteed to validate against this - no prompt
# begging, no defensive parsing of prose.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "True only if this changes a specific fantasy player's expected output.",
        },
        "player_name": {"type": "string", "description": "Full name, or empty if not relevant."},
        "team": {"type": "string", "description": "Team abbreviation like KC, or empty if unknown."},
        "news_type": {"type": "string", "description": "One of the allowed news types, or empty."},
        "magnitude": {"type": "number", "description": "Points per game for HYPE/BUST/CUSTOM, else 0."},
        "weeks": {"type": "array", "items": {"type": "integer"},
                  "description": "Weeks affected. Empty means the whole season."},
        "confidence": {"type": "number", "description": "0..1, how sure you are."},
        "reason": {"type": "string", "description": "One short sentence explaining the call."},
    },
    "required": ["relevant", "player_name", "team", "news_type", "magnitude",
                 "weeks", "confidence", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You convert NFL news headlines into fantasy football projection changes.

You will be given one headline and the list of players tracked in this league.
Decide whether the headline changes a tracked player's expected fantasy output.

Set relevant=false for: contract extensions, awards, previews, opinion pieces,
betting articles, uniform news, coaching profiles, and anything about a player
not in the tracked list. Being in the news is not the same as changing value.

Allowed news_type values and what they mean:
  OUT           player will not play (ruled out, torn ACL, placed on IR)
  QUESTIONABLE  listed questionable; removes about a quarter of his value
  DOUBTFUL      listed doubtful; removes most of his value
  RETURNS       coming back from injury, restore his value
  SUSPENDED     banned for a number of games (use weeks)
  RELEASED      cut by his team
  SIGNED        joins a new team (set team to the NEW team)
  TRADED        traded to a new team (set team to the NEW team)
  ROLE          depth chart change on the same team (named starter, benched)
  HYPE          clearly positive outlook change; set magnitude in points per game
  BUST          clearly negative outlook change; set magnitude in points per game

Rules:
- weeks: [] means the whole season. A single game means that week only.
- For a season-ending injury use OUT with weeks [].
- magnitude is only used by HYPE, BUST and CUSTOM. Otherwise 0.
- Prefer relevant=false when you are unsure. A wrong entry silently corrupts
  every projection that depends on it, which is worse than missing one.
- confidence below 0.6 means you are guessing."""


def extract_with_claude(headlines: List[dict], roster_names: List[str], week: int,
                        model: Optional[str] = None, api_key: Optional[str] = None,
                        max_items: int = 25) -> Tuple[List[dict], List[str]]:
    """Ask Claude to read each headline and return a structured verdict.

    Returns (results, errors).  Each result is the parsed JSON plus the
    original headline.  Requires the `anthropic` package and an API key; if
    either is missing, returns ([], ["reason"]) rather than raising, so the
    rule-based path keeps working.

    Cost note: this is a small classification call.  The roster list is sent
    as a cached prefix so repeated runs only pay full price for the headline.
    """
    try:
        import anthropic
    except ImportError:
        return [], ["the 'anthropic' package is not installed - run: pip install anthropic"]

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        # The SDK can also authenticate from an `ant auth login` profile, so
        # only refuse if there is no credential of any kind.
        try:
            client = anthropic.Anthropic()
        except Exception:
            return [], ["no API key - set ANTHROPIC_API_KEY or run: ant auth login"]
    else:
        client = anthropic.Anthropic(api_key=key)

    model = model or INGEST["MODEL"]
    roster_block = "Tracked players:\n" + "\n".join(sorted(roster_names))
    results, errors = [], []

    for item in headlines[:max_items]:
        text = f"Headline: {item['title']}\n"
        if item.get("description"):
            text += f"Detail: {item['description']}\n"
        text += f"Current NFL week: {week}"
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1000,
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT},
                    # The roster rarely changes, so cache it: repeat runs pay
                    # about a tenth as much for this part of the prompt.
                    {"type": "text", "text": roster_block,
                     "cache_control": {"type": "ephemeral"}},
                ],
                output_config={
                    "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
                    "effort": "low",   # a short classification; no need to think hard
                },
                messages=[{"role": "user", "content": text}],
            )
        except Exception as e:                      # network, auth, rate limit
            errors.append(f"{type(e).__name__}: {e}")
            continue
        if getattr(resp, "stop_reason", None) == "refusal":
            errors.append(f"model declined: {item['title'][:60]}")
            continue
        raw = "".join(b.text for b in resp.content if b.type == "text")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"unparseable reply for: {item['title'][:60]}")
            continue
        parsed["_headline"] = item["title"]
        parsed["_source"] = item.get("source", "RSS")
        results.append(parsed)
    return results, errors


def ai_results_to_proposals(graph, results: List[dict], week: int,
                            start_id: int = 1) -> Tuple[List[Proposal], List[dict]]:
    """Turn the model's verdicts into Proposals, applying the same safety
    checks as the rule-based path (name/team/position must agree)."""
    from .news import NEWS_TYPES
    index = build_name_index(graph)
    proposals, skipped = [], []
    next_id = start_id
    for r in results:
        if not r.get("relevant"):
            continue
        ntype = (r.get("news_type") or "").upper()
        if ntype not in NEWS_TYPES:
            skipped.append({"name": r.get("player_name", "?"), "status": ntype,
                            "why": f"unknown news type '{ntype}'"})
            continue
        # For a move, the team in the news is the DESTINATION, so do not use
        # it to verify the player's current team.
        is_move = ntype in ("SIGNED", "TRADED")
        node, why = resolve_player(graph, r.get("player_name", ""),
                                   None if is_move else (r.get("team") or None),
                                   None, index)
        if node is None:
            skipped.append({"name": r.get("player_name", "?"), "status": ntype, "why": why})
            continue
        conf = float(r.get("confidence") or 0)
        if conf < INGEST["MIN_CONFIDENCE"]:
            skipped.append({"name": node.name, "status": ntype,
                            "why": f"confidence {conf:.2f} below floor {INGEST['MIN_CONFIDENCE']}"})
            continue
        parts = [f'NEWS ADD "{node.name}" {ntype}']
        if is_move and r.get("team"):
            parts.append(r["team"].upper())
        if ntype in ("HYPE", "BUST", "CUSTOM") and r.get("magnitude"):
            parts.append(f'{float(r["magnitude"]):.1f}')
        weeks = r.get("weeks") or []
        if weeks:
            parts.append("--weeks " + ",".join(str(int(w)) for w in weeks))
        note = (r.get("reason") or r.get("_headline", ""))[:60].replace('"', "'")
        parts.append(f'--note "{note}"')
        proposals.append(Proposal(
            id=next_id, command=" ".join(parts), node_id=node.id, node_name=node.name,
            team=node.team, news_type=ntype, confidence=conf,
            headline=r.get("_headline", ""), source=r.get("_source", "RSS"),
            origin="ai", reason=r.get("reason", ""),
        ))
        next_id += 1
    return proposals, skipped
