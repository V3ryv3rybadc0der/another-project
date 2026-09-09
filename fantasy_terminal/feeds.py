"""
feeds.py - FETCHING REAL NFL NEWS
=================================

This file ONLY downloads data.  It does not decide what the news means -
that is ingest.py's job.  Keeping them apart means you can swap in a
different source without touching the interpretation logic.

Two sources, both FREE and needing NO API KEY or signup:

    fetch_sleeper_injuries()  -> structured injury report for every NFL player
        https://api.sleeper.app/v1/players/nfl
        Gives: full_name, team, position, depth_chart_order, injury_status
        ("Questionable" / "Doubtful" / "Out" / "IR" / "PUP" / "Sus"),
        injury_body_part, and news_updated.
        This is the BACKBONE: it is already structured, so no AI is needed
        to read it.  ~15 MB, so it is cached on disk (see CACHE_HOURS).

    fetch_rss(url)            -> headlines from any RSS feed
        ESPN / NFL.com / CBS / Yahoo all publish free RSS.
        Gives: title, description, link, published date - free text, which
        is where the AI extractor in ingest.py earns its keep.

Everything uses the Python standard library (urllib, xml, json) so the
project still has zero dependencies.

To ADD A NEWS SOURCE: add a URL to RSS_FEEDS, or write a new fetch_*
function that returns a list of dicts and wire it into ingest.py.
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from .config import DATA_DIR

# ---------------------------------------------------------------------------
# SOURCES - edit these to change where news comes from
# ---------------------------------------------------------------------------
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_STATE_URL = "https://api.sleeper.app/v1/state/nfl"

# RSS feeds.  Add or remove lines freely; each is (name, url).
RSS_FEEDS = [
    ("ESPN NFL", "https://www.espn.com/espn/rss/nfl/news"),
    ("Rotowire", "https://www.rotowire.com/rss/news.php?sport=NFL"),
    ("CBS NFL", "https://www.cbssports.com/rss/headlines/nfl/"),
    ("Yahoo NFL", "https://sports.yahoo.com/nfl/rss.xml"),
    ("ProFootballTalk", "https://profootballtalk.nbcsports.com/feed/"),
]

# The Sleeper player file is ~15 MB.  Re-download it only if the cached copy
# is older than this many hours.  Set to 0 to always re-download.
CACHE_HOURS = 6

# Where the cached copy lives.
SLEEPER_CACHE = os.path.join(DATA_DIR, "sleeper_players.json")

# Pretend to be a browser; some feeds reject the default urllib agent.
USER_AGENT = "Mozilla/5.0 (compatible; FantasyFootballTerminal/1.0)"
TIMEOUT = 45


class FeedError(Exception):
    """Raised when a source cannot be reached or parsed."""


def _get(url: str, timeout: int = TIMEOUT) -> bytes:
    """Download a URL and return the raw bytes.

    Honours the HTTPS_PROXY / http_proxy environment variables automatically
    (urllib does this for us), which matters in sandboxed environments.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, ssl.SSLError, TimeoutError, OSError) as e:
        raise FeedError(f"could not fetch {url}: {e}") from e


# ---------------------------------------------------------------------------
# SLEEPER - structured injury data (the good stuff)
# ---------------------------------------------------------------------------
def current_nfl_week() -> Optional[int]:
    """Ask Sleeper what NFL week it is right now.  None if unreachable."""
    try:
        return int(json.loads(_get(SLEEPER_STATE_URL, timeout=15))["week"])
    except (FeedError, KeyError, ValueError, TypeError):
        return None


def fetch_sleeper_players(force: bool = False) -> Dict[str, dict]:
    """Download (or read from cache) every NFL player Sleeper knows about.

    Returns the raw dict keyed by Sleeper's player id.  Uses the on-disk
    cache when it is younger than CACHE_HOURS unless force=True.
    """
    fresh_enough = (
        not force
        and os.path.exists(SLEEPER_CACHE)
        and (time.time() - os.path.getmtime(SLEEPER_CACHE)) < CACHE_HOURS * 3600
    )
    if fresh_enough:
        try:
            with open(SLEEPER_CACHE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass                       # cache is corrupt - fall through and re-download
    raw = _get(SLEEPER_PLAYERS_URL)
    data = json.loads(raw)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SLEEPER_CACHE, "wb") as f:
            f.write(raw)
    except OSError:
        pass                           # caching is a nicety, not a requirement
    return data


def fetch_sleeper_injuries(force: bool = False, positions=("QB", "RB", "WR", "TE", "K")) -> List[dict]:
    """Just the players who currently carry an injury designation.

    Returns a list of plain dicts:
        {name, team, pos, depth, status, body_part, updated}
    `status` is Sleeper's raw tag: Questionable / Doubtful / Out / IR / PUP /
    Sus / NA / COV / DNR.  ingest.py decides what each one means.
    """
    out = []
    for p in fetch_sleeper_players(force).values():
        status = p.get("injury_status")
        if not status or not p.get("team") or not p.get("full_name"):
            continue
        if positions and p.get("position") not in positions:
            continue
        out.append({
            "name": p["full_name"],
            "team": p["team"],
            "pos": p.get("position"),
            "depth": p.get("depth_chart_order"),
            "status": status,
            "body_part": p.get("injury_body_part") or "",
            "updated": p.get("news_updated"),
        })
    # newest news first so the most relevant items are reviewed first
    out.sort(key=lambda r: r["updated"] or 0, reverse=True)
    return out


# ---------------------------------------------------------------------------
# RSS - free-text headlines
# ---------------------------------------------------------------------------
def _text(el) -> str:
    """Element text, with CDATA and whitespace cleaned up."""
    return (el.text or "").strip() if el is not None else ""


def fetch_rss(url: str, source: str = "") -> List[dict]:
    """Parse one RSS feed into a list of dicts.

    Returns: {source, title, description, link, published}
    Raises FeedError if the feed is unreachable or not valid XML.
    """
    raw = _get(url)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise FeedError(f"{url} is not valid RSS: {e}") from e
    items = []
    # RSS 2.0 uses <item>; Atom uses <entry>.  Handle both.
    for it in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
        title = _text(it.find("title")) or _text(it.find("{http://www.w3.org/2005/Atom}title"))
        if not title:
            continue
        link_el = it.find("link")
        link = _text(link_el) or (link_el.get("href") if link_el is not None else "")
        items.append({
            "source": source or url,
            "title": title,
            "description": _text(it.find("description")) or _text(it.find("{http://www.w3.org/2005/Atom}summary")),
            "link": link,
            "published": _text(it.find("pubDate")) or _text(it.find("{http://www.w3.org/2005/Atom}updated")),
        })
    return items


def fetch_all_rss(feeds=None) -> List[dict]:
    """Fetch every feed in RSS_FEEDS (or the list you pass).

    A feed that fails is skipped rather than killing the whole run - one
    dead source should not stop the others.  Failures are reported in the
    returned `errors` list.
    """
    feeds = feeds if feeds is not None else RSS_FEEDS
    items, errors = [], []
    for name, url in feeds:
        try:
            items.extend(fetch_rss(url, name))
        except FeedError as e:
            errors.append(str(e))
    # de-duplicate: the same story often appears in several feeds
    seen, unique = set(), []
    for it in items:
        key = it["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique, errors
