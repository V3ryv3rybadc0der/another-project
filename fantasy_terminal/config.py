"""
config.py - EVERY TUNABLE NUMBER LIVES HERE
===========================================

If you want to change how strongly one thing affects another, this is the
file to edit.  Nothing in here needs code knowledge: it is just dictionaries
of numbers with comments.

The two big ideas:

1.  WEIGHTS  - how much of a shock flows across each kind of connection.
               A weight of 0.50 on "WR1_TO_QB" means: if the WR1 moves by
               +10 points per game, the QB moves by +5.  Negative weights
               mean the two compete (RB1 gains -> RB2 loses).

2.  PROPAGATION - how far a shock is allowed to travel before we stop
               chasing tiny numbers (max depth, minimum size, damping).

You can also change weights at runtime without editing this file using the
terminal command:   SET WEIGHT WR1_TO_QB 0.6
"""

# ---------------------------------------------------------------------------
# EDGE WEIGHTS
# ---------------------------------------------------------------------------
# Key format:  "<SOURCE>_TO_<TARGET>"
#   The SOURCE is the node that moved.  The TARGET is the node that reacts.
#   Position codes:  QB, RB, WR, TE, K, DST (team defense), OL (one lineman),
#                    OLUNIT (the whole offensive line), OFF (team offense hub)
#   A number after a code is the depth-chart slot (WR1 = top receiver).
#
# How to read a line:  "WR1_TO_QB": 0.50
#   -> "when the WR1 changes by X points, the QB changes by 0.50 * X"
#
# If a key is missing for some combination, that connection is simply not
# created (weight 0).  Add a key to create the connection.
WEIGHTS = {
    # --- QB -> pass catchers  (better QB = more points for his targets) ------
    "QB1_TO_WR1": 0.50,
    "QB1_TO_WR2": 0.35,
    "QB1_TO_WR3": 0.20,
    "QB1_TO_TE1": 0.35,
    "QB1_TO_TE2": 0.15,
    "QB1_TO_RB1": 0.10,   # a good passing game opens up running lanes a bit
    "QB1_TO_RB2": 0.05,
    "QB1_TO_K1":  0.15,   # more drives -> more field goals / extra points

    # --- pass catchers -> QB  (star WR arrives = QB throws for more) --------
    "WR1_TO_QB1": 0.50,
    "WR2_TO_QB1": 0.30,
    "WR3_TO_QB1": 0.15,
    "TE1_TO_QB1": 0.30,
    "TE2_TO_QB1": 0.10,
    "RB1_TO_QB1": 0.10,   # a strong run game helps the QB (play action)

    # --- competition for the same touches (NEGATIVE = they compete) ---------
    "RB1_TO_RB2": -0.50,  # RB1 goes down 10 -> RB2 (handcuff) goes UP 5
    "RB2_TO_RB1": -0.25,
    "RB1_TO_RB3": -0.20,
    "WR1_TO_WR2": -0.30,  # a star WR1 arriving takes a big bite of the old WR1 (now WR2)
    "WR2_TO_WR1": -0.15,
    "WR1_TO_WR3": -0.15,
    "WR2_TO_WR3": -0.10,
    "WR1_TO_TE1": -0.15,
    "TE1_TO_WR1": -0.05,
    "TE1_TO_WR2": -0.05,
    "QB1_TO_QB2": -0.70,  # starting QB out -> backup QB inherits most of it

    # --- offensive line ------------------------------------------------------
    "OL1_TO_OLUNIT": 0.30,    # a star lineman is ~30% of the line's quality
    "OL2_TO_OLUNIT": 0.20,    # a regular starter is ~20% (one of five)
    "OLUNIT_TO_QB1": 0.50,    # line quality -> QB (sacks, time to throw)
    "OLUNIT_TO_RB1": 0.40,    # line quality -> lead back (run blocking)
    "OLUNIT_TO_RB2": 0.15,
    "OLUNIT_TO_WR1": 0.05,    # tiny direct effect; most flows through the QB

    # --- team offense hub (OFF) ----------------------------------------------
    # Every skill player feeds the team's overall offense node.  The offense
    # node is what the OPPOSING DEFENSES on the schedule are connected to.
    "QB1_TO_OFF":    0.60,
    "QB2_TO_OFF":    0.05,
    "RB1_TO_OFF":    0.35,
    "RB2_TO_OFF":    0.15,
    "RB3_TO_OFF":    0.05,
    "WR1_TO_OFF":    0.35,
    "WR2_TO_OFF":    0.25,
    "WR3_TO_OFF":    0.15,
    "TE1_TO_OFF":    0.20,
    "TE2_TO_OFF":    0.05,
    "OLUNIT_TO_OFF": 0.30,

    # The offense hub feeds back into its own players ONLY when the shock came
    # from OUTSIDE the team (e.g. an opposing defense got weaker this week).
    # Shocks that started inside the team already reached the players through
    # the direct player-to-player edges above, so we do not double count.
    "OFF_TO_QB1": 0.60,
    "OFF_TO_QB2": 0.05,
    "OFF_TO_RB1": 0.40,
    "OFF_TO_RB2": 0.20,
    "OFF_TO_RB3": 0.05,
    "OFF_TO_WR1": 0.45,
    "OFF_TO_WR2": 0.30,
    "OFF_TO_WR3": 0.20,
    "OFF_TO_TE1": 0.25,
    "OFF_TO_TE2": 0.05,
    "OFF_TO_K1":  0.20,

    # --- schedule connections (these edges carry a WEEK number) --------------
    "OFF_TO_OPP_DST": -0.04,  # our offense +10 -> the defense we face that week -0.4
    "DST_TO_OPP_OFF": -0.20,  # a defense +5 (star pass rusher back) -> opp offense -1.0
    "OFF_TO_OWN_DST": 0.03,   # our offense scoring more -> our own DST gets leads / pass rush
}

# ---------------------------------------------------------------------------
# PROPAGATION LIMITS
# ---------------------------------------------------------------------------
PROPAGATION = {
    # How many hops away from the news a shock may travel.
    # Example chain of 4 hops:  OL -> OLUNIT -> QB -> OFF -> week-3 opponent DST
    "MAX_DEPTH": 5,

    # Stop following a shock once it is smaller than this many points.
    # Lower = more (tiny) ripples, slower.  Higher = cleaner, fewer ripples.
    "MIN_DELTA": 0.02,

    # Extra multiplier applied on EVERY hop (1.0 = none).  Set to e.g. 0.9 to
    # make far-away effects fade faster without touching individual weights.
    "DAMPING": 1.0,
}

# ---------------------------------------------------------------------------
# STATS / PROJECTION SETTINGS
# ---------------------------------------------------------------------------
STATS = {
    # Number of regular season games (used to average weekly effects into a
    # season number).
    "GAMES_IN_SEASON": 17,
    "WEEKS_IN_SEASON": 18,

    # How much a matchup changes a player's weekly projection.
    # 0.5 means: facing a defense that gives up 20% more than average to your
    # position lifts your projection by 10% (20% * 0.5).
    "MATCHUP_SENSITIVITY": 0.5,

    # Fraction of a player's value that a QUESTIONABLE tag removes.
    "QUESTIONABLE_HAIRCUT": 0.25,
    # Fraction removed for DOUBTFUL.
    "DOUBTFUL_HAIRCUT": 0.75,

    # Default magnitude (points per game) for news types where the user did
    # not give a number:  NEWS ADD <player> HYPE     -> +DEFAULT_MAGNITUDE
    "DEFAULT_MAGNITUDE": 2.0,
}

# ---------------------------------------------------------------------------
# DISPLAY SETTINGS (the "Bloomberg" look)
# ---------------------------------------------------------------------------
DISPLAY = {
    # Set to False (or run with --no-color / set env NO_COLOR=1) for plain text.
    "COLOR": True,
    # Width of the screen.  "auto" follows the terminal window (minimum 100),
    # or put a number here for a fixed width.
    "WIDTH": "auto",
    # Clear the screen before every command in interactive mode, so each
    # function replaces the previous one like a real terminal screen.
    "CLEAR_SCREEN": True,
    # How many cells the top ticker strip shows.
    "TICKER_CELLS": 12,
    # How many rows the TICKER / RANK boards show by default.
    "DEFAULT_ROWS": 20,
    # Prompt text.
    "PROMPT": "FFT> ",
}

# ---------------------------------------------------------------------------
# NEWS INGESTION (pulling real NFL news in - see feeds.py and ingest.py)
# ---------------------------------------------------------------------------
INGEST = {
    # Which Claude model reads the free-text headlines.  Only used by the
    # optional AI path; the Sleeper injury path needs no model at all.
    # Cheaper options: "claude-sonnet-5", "claude-haiku-4-5".
    "MODEL": "claude-opus-5",

    # Proposals below this confidence are thrown away rather than queued.
    "MIN_CONFIDENCE": 0.5,

    # Proposals below this are flagged in the review queue as "CHECK".
    "AUTO_MIN_CONFIDENCE": 0.8,

    # How many headlines to send to the model in one FEED FETCH.
    # Each headline is one small API call, so this bounds the cost.
    "MAX_HEADLINES": 25,

    # Never apply proposals automatically.  Set True only if you really want
    # the board to move without you reading the headline first.
    "AUTO_APPLY": False,
}

# ---------------------------------------------------------------------------
# FILE LOCATIONS
# ---------------------------------------------------------------------------
import os as _os
DATA_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data")
FILES = {
    "teams":     _os.path.join(DATA_DIR, "teams.csv"),
    "players":   _os.path.join(DATA_DIR, "players.csv"),
    "defenses":  _os.path.join(DATA_DIR, "defenses.csv"),
    "schedule":  _os.path.join(DATA_DIR, "schedule.csv"),
    # Where SAVE / LOAD put the news log when no filename is given.
    "default_save": _os.path.join(DATA_DIR, "session.json"),
}
