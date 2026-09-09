"""
fantasy_terminal
================

A "Bloomberg terminal" for fantasy football.

Every player, offensive line, team offense and defense is a NODE in a graph.
Every relationship between them (QB throws to WR, O-line protects QB, offense
faces a defense in week 3, ...) is a weighted EDGE.  When news happens
(injury, release, signing, trade, hype, weather, ...) a "shock" is applied to
one node and the shock flows along the edges, shrinking by each edge's weight,
so that every connected node moves by an amount proportional to how directly
it is affected.

Package layout (each file has a header comment explaining what it does):

    config.py       - all tunable numbers: edge weights, propagation limits, colors
    models.py       - the data classes: Node, Edge, Team, Contribution, Action
    data_loader.py  - reads the CSV files in ./data into a fresh ImpactGraph
    graph.py        - ImpactGraph: nodes, edges, auto-wiring, shock propagation
    stats.py        - projections, strength of schedule, matchup math, rankings
    news.py         - the catalogue of news types and how each becomes shocks
    ui.py           - ANSI colors, tables and panels (the Bloomberg look)
    terminal.py     - the interactive command line (FFT> prompt) and commands
    __main__.py     - lets you run `python -m fantasy_terminal`
"""

__version__ = "1.0.0"
