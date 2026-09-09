"""
tests/test_graph.py - run with:  python -m unittest -v

These check the engine, not the screens:
  * an O-line release ripples OL -> O-LINE -> QB -> WR and to an opposing DST
  * week-tagged edges only carry shocks into that week
  * a signing lifts the QB and shifts the depth chart
  * UNDO restores the exact base state
  * IMPACT (propagate) does not change anything
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasy_terminal import stats as S          # noqa: E402
from fantasy_terminal.terminal import Terminal  # noqa: E402
from fantasy_terminal import ui                 # noqa: E402

ui.set_color(False)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.t = Terminal()
        self.g = self.t.graph

    def node(self, text):
        return self.t.resolve(text)

    def test_release_lineman_ripples_to_qb_wr_and_opponent(self):
        williams = self.node("Trent Williams")
        purdy = self.node("Brock Purdy")
        pearsall = self.node("Ricky Pearsall")
        sf_off = self.node("SF OFF")
        wk1_opp = self.g.teams["SF"].schedule[1]
        opp_dst = self.g.nodes[self.g.teams[wk1_opp].dst_node_id]

        self.t.run_line("NEWS ADD trent williams RELEASED")

        self.assertEqual(williams.team, "FA")
        self.assertEqual(williams.status, "RELEASED")
        self.assertLess(purdy.season_delta, 0, "QB should lose points when his LT is cut")
        self.assertLess(pearsall.season_delta, 0, "WR1 should lose points via the QB")
        self.assertLess(sf_off.season_delta, 0)
        # the defense SF faces in week 1 gets a small weekly BOOST
        self.assertGreater(opp_dst.week_deltas.get(1, 0.0), 0)
        # the effect on the DST is much smaller than on the QB (more hops away)
        self.assertLess(abs(opp_dst.week_deltas[1]), abs(purdy.season_delta))
        # chain is recorded for EXPLAIN
        chain = max(purdy.contributions, key=lambda c: abs(c.delta))
        self.assertIn(williams.id, chain.path)
        self.assertEqual(chain.path[-1], purdy.id)

    def test_weekly_news_only_touches_that_week(self):
        mahomes = self.node("Patrick Mahomes")
        rice = self.node("Rashee Rice")
        self.t.run_line("NEWS ADD mahomes OUT --weeks 3")
        self.assertAlmostEqual(mahomes.season_delta, 0.0)
        self.assertLess(mahomes.week_deltas[3], 0)
        self.assertLess(rice.week_deltas.get(3, 0.0), 0)
        self.assertNotIn(4, rice.week_deltas)
        self.assertIsNone(S.projected_week(self.g, mahomes, self.g.teams["KC"].bye_week))

    def test_signing_star_wr_lifts_qb_and_shifts_depth(self):
        higgins = self.node("Tee Higgins")
        burrow = self.node("Joe Burrow")
        fields = self.node("Justin Fields")
        old_nyj_wr1 = self.g.players_at("NYJ", "WR")[0]
        self.t.run_line("NEWS ADD tee higgins SIGNED NYJ --depth 1")
        self.assertEqual(higgins.team, "NYJ")
        self.assertEqual(higgins.depth, 1)
        self.assertEqual(old_nyj_wr1.depth, 2)
        self.assertGreater(fields.season_delta, 0, "new QB gains from a star WR")
        self.assertLess(burrow.season_delta, 0, "old QB loses his WR2")
        # the star arriving takes targets from the old WR1
        self.assertLess(old_nyj_wr1.season_delta, 0)

    def test_undo_restores_base_state(self):
        base = {nid: S.projected_season(self.g, n) for nid, n in self.g.nodes.items()}
        self.t.run_line("NEWS ADD lamar jackson OUT")
        self.t.run_line("NEWS ADD derrick henry HYPE 3")
        self.assertEqual(len(self.t.actions), 2)
        self.t.run_line("NEWS UNDO")
        self.t.run_line("NEWS UNDO")
        after = {nid: S.projected_season(self.t.graph, n) for nid, n in self.t.graph.nodes.items()}
        self.assertEqual(base, after)

    def test_impact_is_a_dry_run(self):
        purdy = self.node("Brock Purdy")
        before = purdy.season_delta
        out = self.t.run_line("IMPACT trent williams -8")
        self.assertIn("nothing applied", out)
        self.assertEqual(purdy.season_delta, before)
        self.assertEqual(self.t.actions, [])

    def test_custom_link_and_weight_override(self):
        self.t.run_line('LINK "Derrick Henry" "Lamar Jackson" 0.5 --label test')
        self.t.run_line("SET WEIGHT RB1_TO_RB2 -0.9")
        self.assertEqual(self.t.graph.weights["RB1_TO_RB2"], -0.9)
        henry, lamar, hill = self.node("Derrick Henry"), self.node("Lamar Jackson"), self.node("Justice Hill")
        self.t.run_line("NEWS ADD derrick henry BUST 4")
        self.assertLess(lamar.season_delta, 0)
        self.assertGreater(hill.season_delta, 0)
        self.assertAlmostEqual(hill.contributions[0].delta, 4 * 0.9)

    def test_screens_render_and_fit_width(self):
        """Every full screen builds without error and no line is wider than the screen."""
        ui.set_color(True)
        try:
            self.t.run_line("NEWS ADD trent williams RELEASED")
            for cmd in ("HOME", "TICKER", "RANK WR", "SOS QB", "PLAYER brock purdy", "TEAM SF",
                        "SCHED KC", "EDGES purdy", "EXPLAIN purdy", "IMPACT mahomes -5", "WEIGHTS", "HELP"):
                out = self.t.run_line(cmd)
                self.assertNotIn("error:", ui.strip(out), cmd)
                for line in ui.strip(out).split("\n"):
                    self.assertLessEqual(len(line), ui.width(), f"{cmd}: line too wide")
        finally:
            ui.set_color(False)

    def test_ui_helpers(self):
        self.assertEqual(ui.strip(ui.dbar(-1.0, 1.0, 11)), "█████│     ")
        self.assertEqual(ui.strip(ui.dbar(0.5, 1.0, 11)), "     │███  ")
        two = ui.columns(["a\nb\nc", "x"], [3, 3])
        self.assertEqual(two.split("\n"), ["a    x  ", "b       ", "c       "])

    def test_save_and_load_roundtrip(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        self.t.run_line("NEWS ADD josh allen QUESTIONABLE --weeks 2")
        self.t.run_line(f"SAVE {path}")
        allen_wk2 = self.node("Josh Allen").week_deltas[2]
        t2 = Terminal()
        t2.run_line(f"LOAD {path}")
        self.assertAlmostEqual(t2.resolve("Josh Allen").week_deltas[2], allen_wk2)


if __name__ == "__main__":
    unittest.main()
