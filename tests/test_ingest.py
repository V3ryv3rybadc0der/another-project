"""
tests/test_ingest.py - NEWS INGESTION TESTS (offline)
=====================================================

These use fixture data, never the network, so they run anywhere and stay
fast.  What they protect:

  * name normalization ("Michael Penix" == "Michael Penix Jr.")
  * the Josh Allen trap: two real players share that name, so a match is
    only allowed when team and position also agree
  * Sleeper's injury tags map to the right news types and scopes
  * the AI path produces valid commands and refuses low-confidence junk
  * proposals are NEVER applied automatically
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasy_terminal import ingest, ui                    # noqa: E402
from fantasy_terminal.terminal import Terminal             # noqa: E402

ui.set_color(False)


class NameMatchingTests(unittest.TestCase):
    def setUp(self):
        self.t = Terminal()
        self.g = self.t.graph

    def test_normalize_strips_suffix_and_punctuation(self):
        self.assertEqual(ingest.normalize_name("Michael Penix Jr."), "MICHAEL PENIX")
        self.assertEqual(ingest.normalize_name("Ja'Marr Chase"), "JAMARR CHASE")
        self.assertEqual(ingest.normalize_name("Brian Robinson Jr."), "BRIAN ROBINSON")
        # the two spellings must land on the same key
        self.assertEqual(ingest.normalize_name("Michael Penix"),
                         ingest.normalize_name("Michael Penix Jr."))

    def test_suffix_difference_still_matches(self):
        node, why = ingest.resolve_player(self.g, "Michael Penix", "ATL", "QB")
        self.assertIsNotNone(node, why)
        self.assertEqual(node.name, "Michael Penix Jr.")

    def test_team_mismatch_is_refused(self):
        """A player listed on the wrong team is NOT silently matched."""
        node, why = ingest.resolve_player(self.g, "Patrick Mahomes", "DEN", "QB")
        self.assertIsNone(node)
        self.assertIn("team mismatch", why)

    def test_position_mismatch_is_refused(self):
        """The Josh Allen trap: the QB must not absorb a linebacker's news."""
        node, why = ingest.resolve_player(self.g, "Josh Allen", "BUF", "LB")
        self.assertIsNone(node)
        self.assertIn("position mismatch", why)
        # ...but the real QB news still lands
        node, why = ingest.resolve_player(self.g, "Josh Allen", "BUF", "QB")
        self.assertIsNotNone(node, why)
        self.assertEqual(node.team, "BUF")

    def test_unknown_player_is_refused(self):
        node, why = ingest.resolve_player(self.g, "Nobody McNobody", "KC", "WR")
        self.assertIsNone(node)
        self.assertIn("not on our roster", why)


class SleeperMappingTests(unittest.TestCase):
    def setUp(self):
        self.t = Terminal()
        self.rows = [
            {"name": "Patrick Mahomes", "team": "KC", "pos": "QB", "depth": 1,
             "status": "Questionable", "body_part": "Knee", "updated": 1},
            {"name": "James Conner", "team": "ARI", "pos": "RB", "depth": 1,
             "status": "IR", "body_part": "Foot", "updated": 2},
            {"name": "Nobody McNobody", "team": "KC", "pos": "WR", "depth": 9,
             "status": "Out", "body_part": "Hamstring", "updated": 3},
            {"name": "Travis Kelce", "team": "KC", "pos": "TE", "depth": 1,
             "status": "NA", "body_part": "", "updated": 4},
        ]

    def test_maps_status_to_type_and_scope(self):
        props, skipped = ingest.match_sleeper_injuries(self.t.graph, self.rows, week=3)
        by_name = {p.node_name: p for p in props}
        # Questionable is a single-week designation
        self.assertIn("--weeks 3", by_name["Patrick Mahomes"].command)
        self.assertEqual(by_name["Patrick Mahomes"].news_type, "QUESTIONABLE")
        # IR is season-long, so it must NOT carry a week flag
        self.assertEqual(by_name["James Conner"].news_type, "OUT")
        self.assertNotIn("--weeks", by_name["James Conner"].command)
        # unknown player and vague status are skipped with a reason
        reasons = {s["name"]: s["why"] for s in skipped}
        self.assertIn("not on our roster", reasons["Nobody McNobody"])
        self.assertIn("not actionable", reasons["Travis Kelce"])

    def test_proposals_are_not_applied_automatically(self):
        """Building proposals must not touch the graph."""
        before = {n.id: n.season_delta for n in self.t.graph.nodes.values()}
        ingest.match_sleeper_injuries(self.t.graph, self.rows, week=1)
        after = {n.id: n.season_delta for n in self.t.graph.nodes.values()}
        self.assertEqual(before, after)
        self.assertEqual(self.t.actions, [])

    def test_generated_commands_actually_run(self):
        """Every proposed command must be valid terminal syntax."""
        props, _ = ingest.match_sleeper_injuries(self.t.graph, self.rows, week=2)
        self.assertTrue(props)
        for p in props:
            out = self.t.run_line(p.command)
            self.assertNotIn("error:", ui.strip(out).lower(), p.command)
        self.assertEqual(len(self.t.actions), len(props))


class AIResultTests(unittest.TestCase):
    def setUp(self):
        self.t = Terminal()

    def test_confident_result_becomes_a_command(self):
        results = [{"relevant": True, "player_name": "Rashee Rice", "team": "KC",
                    "news_type": "OUT", "magnitude": 0, "weeks": [4], "confidence": 0.9,
                    "reason": "ruled out with a hamstring", "_headline": "Rice out week 4",
                    "_source": "ESPN"}]
        props, skipped = ingest.ai_results_to_proposals(self.t.graph, results, week=4)
        self.assertEqual(len(props), 1)
        self.assertIn("--weeks 4", props[0].command)
        self.assertEqual(props[0].origin, "ai")
        out = self.t.run_line(props[0].command)
        self.assertNotIn("error:", ui.strip(out).lower())

    def test_low_confidence_is_dropped(self):
        results = [{"relevant": True, "player_name": "Rashee Rice", "team": "KC",
                    "news_type": "OUT", "magnitude": 0, "weeks": [], "confidence": 0.2,
                    "reason": "guessing", "_headline": "h", "_source": "ESPN"}]
        props, skipped = ingest.ai_results_to_proposals(self.t.graph, results, week=1)
        self.assertEqual(props, [])
        self.assertIn("confidence", skipped[0]["why"])

    def test_irrelevant_is_ignored(self):
        results = [{"relevant": False, "player_name": "", "team": "", "news_type": "",
                    "magnitude": 0, "weeks": [], "confidence": 0.9, "reason": "contract news",
                    "_headline": "Mayfield signs extension", "_source": "ESPN"}]
        props, skipped = ingest.ai_results_to_proposals(self.t.graph, results, week=1)
        self.assertEqual(props, [])

    def test_signing_uses_destination_team(self):
        """For a move, the team in the news is where he is GOING, so it must
        not be used to verify his current team."""
        results = [{"relevant": True, "player_name": "Tee Higgins", "team": "NYJ",
                    "news_type": "SIGNED", "magnitude": 0, "weeks": [], "confidence": 0.9,
                    "reason": "signs with the Jets", "_headline": "Higgins to NYJ",
                    "_source": "ESPN"}]
        props, skipped = ingest.ai_results_to_proposals(self.t.graph, results, week=1)
        self.assertEqual(len(props), 1, skipped)
        self.assertIn("NYJ", props[0].command)
        out = self.t.run_line(props[0].command)
        self.assertNotIn("error:", ui.strip(out).lower())
        self.assertEqual(self.t.resolve("Tee Higgins").team, "NYJ")

    def test_unknown_news_type_is_refused(self):
        results = [{"relevant": True, "player_name": "Rashee Rice", "team": "KC",
                    "news_type": "EXPLODED", "magnitude": 0, "weeks": [], "confidence": 0.9,
                    "reason": "x", "_headline": "h", "_source": "ESPN"}]
        props, skipped = ingest.ai_results_to_proposals(self.t.graph, results, week=1)
        self.assertEqual(props, [])
        self.assertIn("unknown news type", skipped[0]["why"])


class FeedCommandTests(unittest.TestCase):
    """The FEED command itself, driven with a pre-seeded queue (no network)."""

    def setUp(self):
        self.t = Terminal()
        rows = [{"name": "Patrick Mahomes", "team": "KC", "pos": "QB", "depth": 1,
                 "status": "Out", "body_part": "Ankle", "updated": 1}]
        props, skipped = ingest.match_sleeper_injuries(self.t.graph, rows, week=1)
        self.t.proposals, self.t.skipped = props, skipped
        self.t.next_proposal_id = len(props) + 1

    def test_list_then_apply_moves_the_board(self):
        out = self.t.run_line("FEED LIST")
        self.assertIn("Patrick Mahomes", ui.strip(out))
        self.assertEqual(self.t.graph.nodes["QB_PATRICK_MAHOMES"].week_deltas, {})
        self.t.run_line("FEED APPLY ALL")
        # the injury is now on the board and his receivers moved with him
        self.assertLess(self.t.resolve("Patrick Mahomes").week_deltas[1], 0)
        self.assertLess(self.t.resolve("Rashee Rice").week_deltas[1], 0)
        self.assertTrue(self.t.proposals[0].applied)

    def test_drop_removes_without_applying(self):
        self.t.run_line("FEED DROP 1")
        self.assertEqual(self.t.proposals, [])
        self.assertEqual(self.t.actions, [])

    def test_apply_high_respects_the_confidence_floor(self):
        self.t.proposals[0].confidence = 0.1
        out = self.t.run_line("FEED APPLY HIGH")
        self.assertIn("error", ui.strip(out).lower())
        self.assertEqual(self.t.actions, [])


if __name__ == "__main__":
    unittest.main()
