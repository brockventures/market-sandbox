"""
tests/test_burst_delta_standings.py - Combine standings rank bursts by
change in net worth over the burst, not absolute net worth (burst-
1790484966-ec8543: APM won 26,775 CR with zero trades on starting cargo
valuation alone). Covers:

  - AgoraReferee.record_burst_baseline() / get_burst_baseline(): snapshot,
    persistence across a fresh referee reading the same sqlite file, and
    wholesale replacement by the next burst.
  - get_leaderboard(): baseline_net_worth/delta_net_worth attached when a
    baseline exists, absent/None otherwise; existing net_worth sort order
    is unchanged.
  - TickerEngine.start_burst(): actually records the baseline.
  - tools/agora_announcer.py formatting: delta-ranked ordering and Baseline/
    Final/Delta display, only when delta fields are present.
"""

import os
import tempfile
import unittest

from agora.referee import AgoraReferee
from agora.ticker import TickerEngine
from tools.agora_announcer import format_final_standings


class TestBurstBaselinePersistence(unittest.TestCase):
    def test_no_baseline_returns_none(self):
        ref = AgoraReferee()
        self.assertIsNone(ref.get_burst_baseline())
        board = ref.get_leaderboard()
        self.assertTrue(board)
        for row in board:
            self.assertIsNone(row['baseline_net_worth'])
            self.assertIsNone(row['delta_net_worth'])

    def test_record_and_read_baseline(self):
        ref = AgoraReferee()
        before = {row['agent_id']: row['net_worth'] for row in ref.get_leaderboard()}
        snapshot = ref.record_burst_baseline('burst-test-1')
        self.assertEqual(snapshot, before)

        baseline = ref.get_burst_baseline()
        self.assertEqual(baseline['burst_id'], 'burst-test-1')
        self.assertEqual(baseline['net_worth'], before)

    def test_leaderboard_gains_delta_fields_after_baseline(self):
        ref = AgoraReferee()
        ref.record_burst_baseline('burst-test-2')
        # Move some CR around so net worth changes for at least one fleet.
        with ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = balance + 5000 WHERE agent_id='amos' AND instrument='CR'")
        board = ref.get_leaderboard()
        amos = next(r for r in board if r['agent_id'] == 'amos')
        self.assertIsNotNone(amos['baseline_net_worth'])
        self.assertEqual(amos['delta_net_worth'], amos['net_worth'] - amos['baseline_net_worth'])
        self.assertEqual(amos['delta_net_worth'], 5000)

    def test_default_sort_still_by_net_worth(self):
        """Attaching delta fields must not change get_leaderboard()'s own
        sort order -- callers that want delta-ranking re-sort themselves."""
        ref = AgoraReferee()
        ref.record_burst_baseline('burst-test-3')
        with ref.conn:
            # Give the currently-lowest fleet a huge net worth swing, but
            # still leave it below the leader in absolute terms.
            ref.conn.execute("UPDATE accounts SET balance = balance + 1 WHERE agent_id='zero' AND instrument='CR'")
        board = ref.get_leaderboard()
        net_worths = [row['net_worth'] for row in board]
        self.assertEqual(net_worths, sorted(net_worths, reverse=True))

    def test_second_burst_replaces_baseline_wholesale(self):
        ref = AgoraReferee()
        ref.record_burst_baseline('burst-test-4a')
        with ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = balance + 2500 WHERE agent_id='amos' AND instrument='CR'")
        ref.record_burst_baseline('burst-test-4b')
        baseline = ref.get_burst_baseline()
        self.assertEqual(baseline['burst_id'], 'burst-test-4b')
        # The new baseline reflects the *post*-first-burst-edit net worth.
        amos_after_edit = next(r for r in ref.get_leaderboard() if r['agent_id'] == 'amos')
        self.assertEqual(baseline['net_worth']['amos'], amos_after_edit['net_worth'] - amos_after_edit['delta_net_worth'])
        self.assertEqual(amos_after_edit['delta_net_worth'], 0)

    def test_baseline_persists_across_referee_reload(self):
        """A restart mid-burst must not lose the baseline (spec point 1):
        persisted to the sqlite file, readable by a fresh AgoraReferee
        pointed at the same db path."""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        os.remove(path)  # AgoraReferee creates its own file at this path
        try:
            ref1 = AgoraReferee(db_path=path)
            before = {row['agent_id']: row['net_worth'] for row in ref1.get_leaderboard()}
            ref1.record_burst_baseline('burst-test-reload')
            ref1.conn.commit()
            ref1.conn.close()

            ref2 = AgoraReferee(db_path=path)
            baseline = ref2.get_burst_baseline()
            self.assertIsNotNone(baseline)
            self.assertEqual(baseline['burst_id'], 'burst-test-reload')
            self.assertEqual(baseline['net_worth'], before)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class TestStartBurstRecordsBaseline(unittest.TestCase):
    def test_start_burst_persists_baseline(self):
        ref = AgoraReferee()
        ticker = TickerEngine(ref, interval_sec=999, inactivity_rounds=100)
        before = {row['agent_id']: row['net_worth'] for row in ref.get_leaderboard()}
        result = ticker.start_burst(rounds=1, interval_sec=0.05)
        baseline = ref.get_burst_baseline()
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline['burst_id'], result['burst_id'])
        self.assertEqual(baseline['net_worth'], before)
        ticker.cancel_burst(force=True)


class TestAnnouncerDeltaFormatting(unittest.TestCase):
    def test_ranks_by_delta_not_absolute_net_worth(self):
        """The zero-trade-winner scenario: APM (net_worth 30000) made no
        trades (delta 0), AVA (net_worth 25000) traded up from a lower
        starting point (delta +8000). AVA should be ranked first."""
        lb = {
            "leaderboard": [
                {
                    "agent_id": "amos",  # APM
                    "balance": {"CR": 30000},
                    "net_worth": 30000,
                    "baseline_net_worth": 30000,
                    "delta_net_worth": 0,
                },
                {
                    "agent_id": "zero",  # AVA
                    "balance": {"CR": 25000},
                    "net_worth": 25000,
                    "baseline_net_worth": 17000,
                    "delta_net_worth": 8000,
                },
            ]
        }
        out = format_final_standings(lb)
        ava_pos = out.index("Apex Vector Arbitrage")
        apm_pos = out.index("Atlantean Paperclip Manufacturing")
        self.assertLess(ava_pos, apm_pos, "fleet with the higher delta should rank first")
        self.assertIn("Δ NW", out)
        self.assertIn("Baseline", out)

    def test_no_baseline_falls_back_to_given_order(self):
        """Backward compatible: entries without delta fields render exactly
        as before (existing behavior/tests must not change)."""
        lb = {
            "leaderboard": [
                {"agent_id": "zero", "balance": {"CR": 120000, "FRAG": 500, "FUEL": 350, "FOOD": 200, "ORE": 100},
                 "mtm_net_worth": 165400},
                {"agent_id": "amos", "balance": {"CR": 95000, "FRAG": 200, "FUEL": 400, "FOOD": 150, "ORE": 50},
                 "mtm_net_worth": 138200},
            ]
        }
        out = format_final_standings(lb)
        self.assertIn("165,400 CR", out)
        self.assertIn("138,200 CR", out)
        self.assertNotIn("Δ NW", out)
        zero_pos = out.index("Apex Vector Arbitrage")
        amos_pos = out.index("Atlantean Paperclip Manufacturing")
        self.assertLess(zero_pos, amos_pos)


if __name__ == "__main__":
    unittest.main()
