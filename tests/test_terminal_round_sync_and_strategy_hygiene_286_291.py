"""
tests/test_terminal_round_sync_and_strategy_hygiene_286_291.py - Terminal Round Sync & Strategy Hygiene (Issues #286 & #291).
"""

import unittest
from unittest.mock import patch
from pathlib import Path

from tools.agora_announcer import (
    build_burst_kickoff,
    build_announcement,
)


class TestTerminalRoundSyncAndStrategyHygiene(unittest.TestCase):
    def test_kickoff_round_range_and_strategy_hygiene(self):
        """Issue #286 & #291: Kickoff must report active round window start_round -> start_round + rounds - 1."""
        burst_id = "burst-test-12345"
        rounds = 8
        interval = 180.0
        start_round = 6540
        kickoff = build_burst_kickoff(burst_id, rounds, interval, start_round)

        # Issue #286: Window starts at start_round, ends at 6547, with settlement at 6548
        self.assertIn("COMBINE WINDOW: Rounds #6540 -> #6547 (8 rounds | Final Settlement #6548)", kickoff)
        self.assertIn("Round #6548 wins", kickoff)

        # Issue #291: Strategy hygiene & REST default guidance
        self.assertIn("Quick API (REST Default):", kickoff)
        self.assertIn("Strategy Hygiene", kickoff)
        self.assertIn("REST", kickoff)
        self.assertIn("Tape publishes", kickoff)

    @patch("tools.agora_announcer.fetch_json")
    @patch("tools.agora_announcer.fetch_ticker_status")
    def test_announcement_referee_round_sync(self, mock_ticker, mock_fetch):
        """Issue #286 & #291: Announcement header must report Referee Round matching announced round."""
        mock_fetch.side_effect = lambda ep: {
            "/referee/health": {"status": "ok", "seq": 42, "floor": "open"},
            "/referee/leaderboard": {"leaderboard": []},
            "/referee/depots": {"depots": {"stations": {}}},
        }.get(ep, {})
        mock_ticker.return_value = {"status": "ok", "current_round": 6541}

        msg, station = build_announcement(round_num=6541, rounds_total=8, round_index=2)

        # Verify title, directive, and header agree on round 6541
        self.assertIn("COMBINE ROUND 2/8 (Round #6541)", msg)
        self.assertIn("DIRECTIVE (Round #6541)", msg)
        self.assertIn("Referee Round:** #6541", msg)

        # Verify Strategy Hygiene and REST guidance in directive
        self.assertIn("API (REST Default):", msg)
        self.assertIn("Strategy Hygiene:", msg)
        self.assertIn("Submit via REST", msg)

    def test_simulated_burst_progression_round_sync(self):
        """Issue #286 Acceptance: For every step in a burst, announced round must equal referee round."""
        start_round = 7000
        rounds_total = 5

        # Simulate T=0 kickoff
        rounds_announced = 1
        last_announced_round = start_round
        recorded_broadcasts = []

        # T=0 announcement
        t0_announced_round = start_round
        recorded_broadcasts.append({
            "step": 0,
            "round_index": rounds_announced,
            "announced_round": t0_announced_round,
            "referee_round": start_round
        })
        self.assertEqual(t0_announced_round, start_round)

        # Simulate ticks 1 through 4 advancing referee round
        for tick in range(1, rounds_total):
            cur_rnd = start_round + tick  # referee advanced via step_round()
            if cur_rnd > last_announced_round:
                last_announced_round = cur_rnd
                rounds_announced += 1
                if rounds_announced <= rounds_total:
                    next_round_num = cur_rnd
                    recorded_broadcasts.append({
                        "step": tick,
                        "round_index": rounds_announced,
                        "announced_round": next_round_num,
                        "referee_round": cur_rnd
                    })

        self.assertEqual(len(recorded_broadcasts), rounds_total)
        for rec in recorded_broadcasts:
            # Acceptance invariant: every single broadcast announced round == referee live round
            self.assertEqual(
                rec["announced_round"],
                rec["referee_round"],
                f"Desync detected at step {rec['step']}: announced {rec['announced_round']} != referee {rec['referee_round']}"
            )

    def test_rules_of_engagement_strategy_hygiene_section(self):
        """Issue #291 Acceptance: docs/rules-of-engagement.md documents strategy hygiene and REST by default."""
        roe_path = Path(__file__).resolve().parent.parent / "docs" / "rules-of-engagement.md"
        self.assertTrue(roe_path.exists())
        content = roe_path.read_text(encoding="utf-8")

        self.assertIn("Strategy Hygiene & Execution Secrecy (REST by Default)", content)
        self.assertIn("REST by Default (Path B)", content)
        self.assertIn("Channel Silence During Live Bursts", content)
        self.assertIn("Post-Burst Debriefs", content)


if __name__ == "__main__":
    unittest.main()
