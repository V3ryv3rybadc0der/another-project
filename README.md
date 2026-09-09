# Fantasy Football Impact Terminal

A finance-terminal style tool for fantasy football, written in plain Python
(no third-party packages).  Every player, offensive line, team offense and
defense is a node in a **weighted impact graph**.  When you add news
(injury, release, signing, trade, hype, weather ...) a shock hits one node and
ripples along the connections, so **everything connected moves, by an amount
that shrinks the further away it is**:

```
Trent Williams RELEASED (-9.0)
  → SF O-LINE   -2.70        (OL1_TO_OLUNIT 0.30)
    → Brock Purdy   -1.35    (OLUNIT_TO_QB1 0.50)
      → Ricky Pearsall -0.68 (QB1_TO_WR1 0.50)
      → SF OFFENSE   -0.81   (QB1_TO_OFF 0.60)
        → PIT DST +0.06 in week 1   (OFF_TO_OPP_DST -0.04, week-tagged edge)
```

Everything is commented so you can change it.  Start with
`fantasy_terminal/config.py` - that is where every weight lives.

---

## 1. Run it

```bash
python3 -m fantasy_terminal                       # interactive terminal  (or: python3 run.py)
python3 -m fantasy_terminal TICKER                # run one command, print, exit
python3 -m fantasy_terminal --script examples/demo.txt   # run a file of commands
python3 -m fantasy_terminal --no-color TEAM KC    # plain text (also: NO_COLOR=1)
python3 -m unittest discover -s tests -v          # run the tests
```

Python 3.8+ and nothing else.

---

## 2. Commands  (type `HELP` inside the terminal)

| Command | What it does |
|---|---|
| `TICKER [POS] [--week N]` | Biggest movers board: season projection, change, %, this week's projection. |
| `RANK <POS\|ALL> [--week N]` | Rankings by projected points (QB RB WR TE K DST), with last year PPG and SOS. |
| `PLAYER <name>` (`P`) | Quote screen: projection, base, change, this week's matchup, last season, full schedule with weekly projections, connections, and every news chain that touched him. Works on team nodes too: `PLAYER KC OFF`, `PLAYER KC DST`, `PLAYER KC OL`. |
| `TEAM <ABBR>` (`T`) | Roster board, offense rating, O-line grade, DST, schedule and SOS per position. |
| `SCHED <ABBR>` | Week by week opponents with points allowed to each position (green soft, red tough). |
| `SOS [POS]` | Strength of schedule for every team for one position. |
| `WEEK [N]` | Show / set the current week used by TICKER, TEAM and PLAYER. |
| `EDGES <name>` | Every connection into and out of a node, with weights. |
| `EXPLAIN <name>` (`X`) | Why a node moved: every chain from every news item, with the delta at each hop. |
| `IMPACT <name> <delta> [--week N]` (`SIM`) | **What-if.** Preview the ripples of a shock without applying it. |
| `NEWS ADD <name> <TYPE> [magnitude \| TEAM] [--weeks N] [--depth N] [--value X] [--note "..."]` | Add news (see types below). Prints everything that moved. |
| `NEWS LIST` / `NEWS UNDO` / `NEWS DEL <id>` / `NEWS CLEAR` | Manage the news log. Undo rebuilds the graph and replays the rest. |
| `ADD "<name>" <TEAM> <POS> <proj> [--depth N]` | Create a brand new player node (rookie, free agent). |
| `LINK "<src>" "<dst>" <weight> [--label ..] [--week N]` | Add your own connection between any two nodes. |
| `UNLINK "<src>" "<dst>"` | Remove a connection. |
| `WEIGHTS [filter]` | Show every weight key and value. |
| `SET WEIGHT <KEY> <value>` | Change a weight live (graph rebuilt, news replayed). |
| `SET MAX_DEPTH 6` / `SET MIN_DELTA 0.01` / `SET DAMPING 0.9` | Tune how far shocks travel. |
| `SAVE [file]` / `LOAD [file]` | Persist the news log + weight changes as JSON (default `data/session.json`). |
| `RESET` | Back to the base projections. |
| `QUIT` | Leave. |

Names are fuzzy and case-insensitive: `PLAYER mahomes`, `NEWS ADD purdy OUT`.
Quote a name only when a command takes several names (`LINK "A" "B" 0.3`).

### News types  (`HELP NEWS`)

| Type | Effect |
|---|---|
| `OUT` / `INJURY` | Player's value goes to 0 for the season, or for `--weeks 3` / `--weeks 1-4`. Backups rise through the negative "shares touches" edges. |
| `QUESTIONABLE` / `DOUBTFUL` | Removes 25% / 75% of his value (season or weeks). |
| `RETURNS` | Value restored to base. |
| `SUSPENDED` | Same as OUT, meant for `--weeks 1-4`. |
| `RELEASED` / `CUT` | Leaves the team. His value drains out of the team's graph, the depth chart shifts up. |
| `SIGNED <TEAM>` / `TRADED <TEAM>` | Joins a team at `--depth N` (default 1). Old team loses his value, new team gains it, everyone below him slides down. `--value 14` sets his new projection. |
| `ROLE --depth N [magnitude]` | Depth chart change on the same team (promoted to starter). |
| `HYPE [n]` / `BUST [n]` | +n / -n points (default 2). |
| `CUSTOM n` | Raw signed shock on ANY node, including `KC OFF`, `KC OL`, `KC DST`. |
| `WEATHER n --weeks W` | Team offense hub loses n points in that week. |
| `COACH n` | Team offense hub +/- n for the season (scheme / coordinator change). |

Examples:

```
NEWS ADD trent williams RELEASED --note "cap casualty"
NEWS ADD tee higgins SIGNED NYJ --depth 1 --value 15
NEWS ADD mahomes OUT --weeks 3
NEWS ADD "BUF OFF" WEATHER 4 --weeks 12
NEWS ADD "DEN DST" CUSTOM -3 --note "star pass rusher torn ACL"
NEWS ADD bijan robinson HYPE 2.5
```

---

## 3. How the model works

### Nodes
* **Players** `QB RB WR TE K` - value = projected fantasy points per game (PPR).
* **OL** - individual star linemen, value = 0-10 grade.
* **OLUNIT** (`KC O-LINE`) - the whole line, value = 0-10 grade.
* **OFF** (`KC OFFENSE`) - the team's offense hub, value ~ team points per game / 3.
* **DST** (`KC DST`) - the team defense, value = fantasy points per game.

### Edges (all weights in `config.py`)
| Family | Example key | Meaning |
|---|---|---|
| QB → pass catchers | `QB1_TO_WR1 = 0.50` | QB moves +10, WR1 moves +5 |
| pass catchers → QB | `WR1_TO_QB1 = 0.50` | star WR arrives (+14.5), QB moves +7.25 |
| competition | `RB1_TO_RB2 = -0.50` | RB1 out (-18), handcuff RB2 moves +9 |
| line | `OL1_TO_OLUNIT = 0.30`, `OLUNIT_TO_QB1 = 0.50` | lineman cut → line → QB → receivers |
| team hub | `QB1_TO_OFF = 0.60`, `OFF_TO_WR1 = 0.45` | player ↔ team offense |
| schedule | `OFF_TO_OPP_DST = -0.04` (week-tagged) | our offense +10 → the DST we face **that week** -0.4 |
| schedule | `DST_TO_OPP_OFF = -0.20` (week-tagged) | a DST +5 → the offense it faces that week -1.0 |

`OFF_TO_*` edges only carry shocks that started **outside** the team (an
opposing defense getting weaker), so an in-team shock is not counted twice.

### Propagation (`graph.py: propagate`)
Breadth-first walk from the news node.  Each hop multiplies the shock by the
edge weight (and `DAMPING`).  It stops at `MAX_DEPTH` hops or when the shock is
under `MIN_DELTA` points, never revisits a node on the same path, and turns a
season shock into a weekly one when it crosses a `FACES WK n` edge.  Every
arrival is stored as a `Contribution` with its full path, which is what
`EXPLAIN` prints.

### Projections (`stats.py`)
* season = base + season news + (weekly news averaged over 17 games)
* week n = base + season news + week-n news + **matchup adjustment**
* matchup = base × (opponent's points allowed to my position ÷ league average − 1) × `MATCHUP_SENSITIVITY`
* SOS = average of that percentage over the whole schedule (`+8%` = easy)

---

## 4. The data  (`data/*.csv` - edit in Excel or a text editor)

| File | Columns | Notes |
|---|---|---|
| `teams.csv` | `abbr,name,division,off_rating,ol_grade,def_strength` | `off_rating` ≈ team PPG / 3 |
| `players.csv` | `name,team,pos,depth,last_ppg,games,proj_ppg,note` | `pos` in QB RB WR TE K OL; OL rows use a 0-10 grade |
| `defenses.csv` | `team,vs_qb,vs_rb,vs_wr,vs_te,vs_k,dst_ppg` | points allowed per game to each position |
| `schedule.csv` | `week,home,away` | bye = a week with no game |

**The shipped data is SAMPLE data.**  Rosters are approximate for the 2025
season and the stats, defensive numbers and schedule are illustrative, not
official.  Replace the CSVs with real numbers (the loader does not care where
they came from).  `python3 tools/make_sample_data.py` regenerates
`defenses.csv` and `schedule.csv` from `teams.csv`.

---

## 5. Changing the code

| I want to... | Go to |
|---|---|
| change how strongly X affects Y | `config.py` → `WEIGHTS` (or `SET WEIGHT` at runtime) |
| let shocks travel further / stop sooner | `config.py` → `PROPAGATION` |
| change what a matchup is worth | `config.py` → `STATS["MATCHUP_SENSITIVITY"]` |
| add a new kind of connection | `graph.py` → `rewire_team()` - add a `self._link(...)` line and a weight key |
| add a new news type | `news.py` - write a function, register it in `NEWS_TYPES` |
| add a new command | `terminal.py` - add `def cmd_NAME(self, args, opts)`; HELP picks it up from the docstring |
| add a new stat or ranking | `stats.py` |
| change colours / width / prompt | `config.py` → `DISPLAY`, `ui.py` |
| load data from somewhere else | `data_loader.py` |

Project layout:

```
fantasy_terminal/      the package (see __init__.py for a file-by-file map)
data/                  CSV inputs + saved sessions
tools/make_sample_data.py   regenerates the sample defenses + schedule
examples/demo.txt      a script you can run with --script
tests/test_graph.py    engine tests
run.py                 launcher
```
