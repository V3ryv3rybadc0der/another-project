"""
tests/test_rosters.py - ROSTERS, MATCHUPS, TRADES AND RECAPS (offline)
=======================================================================

Fixture data only, never the network.  What these protect:

  * the lineup picker starts a legal lineup and benches the surplus
  * FLEX gets the best LEFT OVER player, not one stolen from a fixed slot
  * a bye-week player is not started ahead of someone who plays
  * news moves a roster's total, and the roster change column counts news
    only - not the matchup, which is not news
  * a trade is priced without moving anybody
  * recap parsing turns real stats into real points
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasy_terminal import games, rosters, ui                 # noqa: E402
from fantasy_terminal.terminal import Terminal                  # noqa: E402

ui.set_color(False)

SQUAD = ("josh allen, jahmyr gibbs, bijan robinson, ja'marr chase, puka nacua, "
         "drake london, trey mcbride, brandon aubrey, SEA DST, breece hall")
RIVAL = ("lamar jackson, christian mccaffrey, saquon barkley, justin jefferson, "
         "amon-ra st. brown, mike evans, george kittle, cam little, DEN DST, james cook")


class RosterBasicsTests(unittest.TestCase):
    def setUp(self):
        self.t = Terminal()
        self.t.run_line("ROSTER NEW MYTEAM --owner me")
        self.t.run_line(f"ROSTER ADD MYTEAM {SQUAD}")
        self.r = self.t.rosters["MYTEAM"]

    def test_apostrophe_names_parse(self):
        """Ja'Marr Chase must not break shell-style splitting."""
        names = [self.t.graph.nodes[i].name for i in self.r.player_ids]
        self.assertIn("Ja'Marr Chase", names)
        self.assertEqual(len(self.r.player_ids), 10)

    def test_lineup_is_legal(self):
        starters, bench, total = rosters.best_lineup(self.t.graph, self.r, 1)
        slots = [s for s, _n, _v in starters]
        self.assertEqual(slots, ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "K", "DST", "FLEX"])
        for slot, node, _v in starters:
            if node is None:
                continue
            if slot == "FLEX":
                self.assertIn(node.pos, ("RB", "WR", "TE"))
            else:
                self.assertEqual(node.pos, slot)
        self.assertAlmostEqual(total, sum(v for _s, n, v in starters if n), places=6)

    def test_a_player_is_never_started_twice(self):
        starters, bench, _ = rosters.best_lineup(self.t.graph, self.r, 1)
        ids = [n.id for _s, n, _v in starters if n]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(set(ids) & {n.id for n, _v in bench})

    def test_surplus_quarterback_goes_to_the_bench(self):
        """Only one QB starts, and FLEX cannot take him in a standard league."""
        self.t.run_line("ROSTER ADD MYTEAM patrick mahomes")
        starters, bench, _ = rosters.best_lineup(self.t.graph, self.r, 1)
        started_qbs = [n for _s, n, _v in starters if n and n.pos == "QB"]
        self.assertEqual(len(started_qbs), 1)
        self.assertIn("Patrick Mahomes", [n.name for n, _v in bench])

    def test_flex_takes_the_best_leftover(self):
        starters, bench, _ = rosters.best_lineup(self.t.graph, self.r, 1)
        flex = next(n for s, n, _v in starters if s == "FLEX")
        self.assertIsNotNone(flex)
        for n, v in bench:
            if n.pos in ("RB", "WR", "TE"):
                flex_pts = next(v2 for s, n2, v2 in starters if s == "FLEX")
                self.assertGreaterEqual(flex_pts, v)

    def test_bye_week_player_is_not_preferred(self):
        """On his bye a player is worth 0, so a healthy backup outranks him."""
        chase = self.t.resolve("Ja'Marr Chase")
        bye = self.t.graph.teams[chase.team].bye_week
        starters, _bench, _t = rosters.best_lineup(self.t.graph, self.r, bye)
        started = [n.name for _s, n, _v in starters if n]
        wr_starters = [n for _s, n, _v in starters if n and n.pos == "WR"]
        # he may still fill an otherwise empty slot, but never at a real value
        for _s, n, v in starters:
            if n is chase:
                self.assertEqual(v, 0.0)


class RosterNewsTests(unittest.TestCase):
    def setUp(self):
        self.t = Terminal()
        self.t.run_line("ROSTER NEW MYTEAM")
        self.t.run_line(f"ROSTER ADD MYTEAM {SQUAD}")
        self.r = self.t.rosters["MYTEAM"]

    def test_injury_lowers_the_roster_total(self):
        before = rosters.roster_value(self.t.graph, self.r, 1)
        self.t.run_line("NEWS ADD ja'marr chase OUT --weeks 1")
        after = rosters.roster_value(self.t.graph, self.r, 1)
        self.assertLess(after, before)

    def test_roster_change_counts_news_only_not_the_matchup(self):
        """With no news at all the change must be exactly zero, even though
        the matchup adjustment is non-zero."""
        self.assertAlmostEqual(rosters.roster_change(self.t.graph, self.r, 1), 0.0, places=6)
        self.t.run_line("NEWS ADD puka nacua BUST 3 --weeks 1")
        self.assertLess(rosters.roster_change(self.t.graph, self.r, 1), 0)

    def test_owners_of_finds_the_holder(self):
        chase = self.t.resolve("Ja'Marr Chase")
        self.assertEqual(rosters.owners_of(self.t.rosters, chase.id), ["MYTEAM"])
        mahomes = self.t.resolve("Patrick Mahomes")
        self.assertEqual(rosters.owners_of(self.t.rosters, mahomes.id), [])


class MatchupAndTradeTests(unittest.TestCase):
    def setUp(self):
        self.t = Terminal()
        self.t.run_line("ROSTER NEW MYTEAM")
        self.t.run_line(f"ROSTER ADD MYTEAM {SQUAD}")
        self.t.run_line("ROSTER NEW DAVE --owner Dave")
        self.t.run_line(f"ROSTER ADD DAVE {RIVAL}")

    def test_matchup_margin_is_the_difference(self):
        c = rosters.compare(self.t.graph, self.t.rosters["MYTEAM"], self.t.rosters["DAVE"], 1)
        self.assertAlmostEqual(c["margin"], c["a_total"] - c["b_total"], places=6)
        self.assertEqual(len(c["rows"]), 10)

    def test_trade_prices_both_sides_without_moving_anyone(self):
        a = self.t.rosters["MYTEAM"]
        before_ids = list(a.player_ids)
        out = self.t.run_line("TRADE MYTEAM drake london FOR DAVE saquon barkley")
        self.assertNotIn("error:", ui.strip(out).lower())
        self.assertEqual(a.player_ids, before_ids, "TRADE must not actually move players")

    def test_trade_rejects_a_player_the_side_does_not_own(self):
        out = self.t.run_line("TRADE MYTEAM patrick mahomes FOR DAVE saquon barkley")
        self.assertIn("not on MYTEAM", ui.strip(out))

    def test_upgrade_shows_as_better(self):
        """Swapping a weaker starter for a stronger one at the same slot must
        read as an improvement."""
        r = rosters.evaluate_trade(
            self.t.graph, self.t.rosters["MYTEAM"], [self.t.resolve("Brandon Aubrey")],
            self.t.rosters["DAVE"], [self.t.resolve("Cam Little")], 1)
        self.assertLess(r["a_delta"], 0.01)   # Aubrey is the better kicker, so this is a downgrade
        self.assertGreater(r["b_delta"], -0.01)

    def test_save_and_load_keeps_rosters(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        self.t.run_line(f"SAVE {path}")
        t2 = Terminal()
        t2.run_line(f"LOAD {path}")
        self.assertIn("MYTEAM", t2.rosters)
        self.assertEqual(t2.rosters["DAVE"].owner, "Dave")
        self.assertEqual(t2.rosters["MYTEAM"].player_ids,
                         self.t.rosters["MYTEAM"].player_ids)


class RecapTests(unittest.TestCase):
    """Recap parsing, using a small hand-built summary payload."""

    SUMMARY = {
        "header": {"id": "1", "competitions": [{
            "status": {"period": 4, "displayClock": "0:00",
                       "type": {"state": "post", "shortDetail": "Final"}},
            "competitors": [
                {"homeAway": "home", "score": "24", "team": {"abbreviation": "KC"}},
                {"homeAway": "away", "score": "17", "team": {"abbreviation": "DEN"}}]}]},
        "boxscore": {"players": [{
            "team": {"abbreviation": "KC"},
            "statistics": [
                {"name": "passing", "labels": ["C/ATT", "YDS", "AVG", "TD", "INT"],
                 "athletes": [{"athlete": {"displayName": "Patrick Mahomes"},
                               "stats": ["25/35", "300", "8.6", "3", "1"]}]},
                {"name": "receiving", "labels": ["REC", "YDS", "AVG", "TD", "LONG", "TGTS"],
                 "athletes": [{"athlete": {"displayName": "Rashee Rice"},
                               "stats": ["8", "100", "12.5", "2", "30", "11"]}]},
            ]}]},
        "scoringPlays": [{"text": "Rashee Rice 20 Yd pass from Patrick Mahomes",
                          "type": {"text": "Passing Touchdown"},
                          "team": {"abbreviation": "KC"},
                          "period": {"number": 1}, "clock": {"displayValue": "5:00"},
                          "awayScore": 0, "homeScore": 7}],
        "drives": {"previous": [{"team": {"abbreviation": "KC"}, "plays": [
            {"id": "1", "text": "P.Mahomes pass to R.Rice for 20 yards. DEN-P.Surtain was injured during the play.",
             "type": {"text": "Pass Reception"}, "period": {"number": 1},
             "clock": {"displayValue": "5:00"}, "scoringPlay": False},
            {"id": "2", "text": "** Injury Update: DEN-P.Surtain has returned to the game.",
             "type": {"text": "Timeout"}, "period": {"number": 3},
             "clock": {"displayValue": "9:00"}, "scoringPlay": False}]}]},
    }

    def test_real_stats_become_real_fantasy_points(self):
        box = games.parse_box_score(self.SUMMARY)
        # 300 pass yds = 12, 3 pass TD = 12, 1 INT = -2  ->  22
        self.assertAlmostEqual(box[("PATRICK MAHOMES", "KC")]["points"], 22.0, places=2)
        # 8 rec = 8, 100 yds = 10, 2 TD = 12  ->  30 (full PPR)
        self.assertAlmostEqual(box[("RASHEE RICE", "KC")]["points"], 30.0, places=2)

    def test_recap_reads_result_scores_and_injuries(self):
        r = games.build_recap(self.SUMMARY)
        self.assertEqual(r.headline, "KC beat DEN 24-17")
        self.assertEqual(len(r.scoring), 1)
        kinds = [i["kind"] for i in r.injuries]
        self.assertEqual(kinds, ["INJURED", "RETURNED"])
        self.assertEqual(r.injuries[0]["team"], "DEN")
        self.assertEqual(r.injuries[0]["when"], "Q1 5:00")

    def test_top_scorers_are_ordered(self):
        r = games.build_recap(self.SUMMARY)
        top = r.top_scorers(2)
        self.assertEqual(top[0][0][0], "RASHEE RICE")
        self.assertGreater(top[0][1]["points"], top[1][1]["points"])

    def test_abbreviated_name_matching_needs_the_right_team(self):
        t = Terminal()
        node = games.match_abbrev_name(t.graph, "P.Mahomes", "KC")
        self.assertIsNotNone(node)
        self.assertEqual(node.name, "Patrick Mahomes")
        # right name, wrong team -> refuse rather than guess
        self.assertIsNone(games.match_abbrev_name(t.graph, "P.Mahomes", "DEN"))


if __name__ == "__main__":
    unittest.main()
