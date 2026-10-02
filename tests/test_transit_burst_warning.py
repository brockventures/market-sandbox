"""
tests/test_transit_burst_warning.py - Tests for transit arrival warnings during burst runs (Issue #289).
"""

import unittest
from agora.referee import AgoraReferee
from agora.ticker import TickerEngine
from agora.briefing import build_briefing


class TestTransitBurstWarning(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee()
        self.ref.current_round = 10
        # Seed test agent at Earth with fuel, frags, and docked ship
        self.agent = "test_trader"
        self.ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, 'CR', 10000)", (self.agent,))
        self.ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, 'FUEL', 1000)", (self.agent,))
        self.ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, 'FRAG', 100)", (self.agent,))
        self.ref.conn.execute("""
            INSERT OR REPLACE INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status)
            VALUES (?, ?, ?, 'earth', 10, 'docked')
        """, (f"{self.agent}/1", self.agent, "Ship 1"))

    def test_no_burst_active_has_no_warning(self):
        # When no burst is active, transit proceeds with arrives_after_burst_end=False
        res = self.ref.initiate_transit(self.agent, "luna", commodity="FRAG", cargo_qty=10)
        self.assertEqual(res.get("kind"), "status")
        self.assertEqual(res.get("status"), "in_transit")
        self.assertFalse(res.get("arrives_after_burst_end"))
        self.assertIsNone(res.get("warning"))
        self.assertFalse(res["payload"].get("arrives_after_burst_end"))
        self.assertIsNone(res["payload"].get("warning"))

        briefing = build_briefing(self.ref, viewer=self.agent)
        self.assertNotIn("Rounds left in burst:", briefing)

    def test_arrival_boundary_at_burst_end(self):
        # Dep round = 10. Earth -> Luna is 1 round duration: arrival_round = 11.
        # Set burst end_round = 11 (boundary: arrival == burst end).
        self.ref._manual_burst_info = {
            "active": True,
            "burst_id": "test-burst",
            "rounds_remaining": 1,
            "rounds_total": 5,
            "end_round": 11,
        }

        res = self.ref.initiate_transit(self.agent, "luna", commodity="FRAG", cargo_qty=10)
        self.assertEqual(res.get("kind"), "status")
        self.assertEqual(res["payload"]["arrival_round"], 11)
        self.assertTrue(res.get("arrives_after_burst_end"))
        self.assertTrue(res["payload"].get("arrives_after_burst_end"))
        self.assertIn("at or after the burst's final round", res.get("warning"))
        self.assertIn("at or after the burst's final round", res["payload"].get("warning"))

        # Briefing check
        briefing = build_briefing(self.ref, viewer=self.agent)
        self.assertIn("**Rounds left in burst: 1**", briefing)

    def test_arrival_after_burst_end(self):
        # Dep round = 10. Earth -> Mars is 2 rounds duration: arrival_round = 12.
        # Set burst end_round = 11 (arrival strictly after burst end).
        self.ref._manual_burst_info = {
            "active": True,
            "burst_id": "test-burst",
            "rounds_remaining": 1,
            "rounds_total": 5,
            "end_round": 11,
        }

        res = self.ref.initiate_transit(self.agent, "mars", commodity="FRAG", cargo_qty=10)
        self.assertEqual(res.get("kind"), "status")
        self.assertEqual(res["payload"]["arrival_round"], 12)
        self.assertTrue(res.get("arrives_after_burst_end"))
        self.assertTrue(res["payload"].get("arrives_after_burst_end"))
        self.assertIn("at or after the burst's final round", res.get("warning"))

    def test_arrival_before_burst_end(self):
        # Dep round = 10. Earth -> Luna arrives round 11. Burst ends round 20.
        self.ref._manual_burst_info = {
            "active": True,
            "burst_id": "test-burst",
            "rounds_remaining": 10,
            "rounds_total": 20,
            "end_round": 20,
        }

        res = self.ref.initiate_transit(self.agent, "luna", commodity="FRAG", cargo_qty=10)
        self.assertEqual(res.get("kind"), "status")
        self.assertEqual(res["payload"]["arrival_round"], 11)
        self.assertFalse(res.get("arrives_after_burst_end"))
        self.assertFalse(res["payload"].get("arrives_after_burst_end"))
        self.assertIsNone(res.get("warning"))
        self.assertIsNone(res["payload"].get("warning"))

    def test_ticker_burst_integration(self):
        ticker = TickerEngine(self.ref, interval_sec=10.0, inactivity_rounds=100)
        # referee.ticker is wired
        self.assertEqual(self.ref.ticker, ticker)

        start_rnd = self.ref.current_round
        burst_info = ticker.start_burst(rounds=3, interval_sec=10.0)
        self.assertIn("burst_id", burst_info)

        # status check
        st = ticker.status()
        self.assertTrue(st["burst_active"])
        self.assertEqual(st["burst_rounds_total"], 3)
        self.assertEqual(st["burst_end_round"], start_rnd + 3)

        info = self.ref.get_burst_info()
        self.assertTrue(info["active"])
        self.assertEqual(info["end_round"], start_rnd + 3)

        # Cancel burst clears burst_end_round
        ticker.cancel_burst(force=True)
        st_after = ticker.status()
        self.assertFalse(st_after["burst_active"])
        self.assertIsNone(st_after["burst_end_round"])
        self.assertFalse(self.ref.get_burst_info()["active"])
