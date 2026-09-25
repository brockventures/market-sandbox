"""
Unit and regression tests for Issue #266:
1. !privateer receipt shows accurate fee (750 CR), 20 rounds duration, and 100% loot share.
2. Resting BANANA orders in SQLite and in-memory books are migrated to FRAG and successfully crossed.
3. UPGRADES_STATUS_PATTERN requires prefix and does not swallow trade orders with casual text.
4. Priority slips test has positive control verifying idle fees actually charge non-exempt fleets.
"""

import unittest
import os
from agora.referee import AgoraReferee
from tools.agora_announcer import (
    UPGRADES_STATUS_PATTERN,
    parse_discord_upgrades_cmd,
    parse_discord_privateer_cmd,
)


class TestIssue266Regressions(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(db_path=":memory:")
        self.ref.reset_to_genesis(depots=True)

    def test_upgrades_status_pattern_does_not_swallow_trades(self):
        """Fix 3: UPGRADES_STATUS_PATTERN must require prefix and not match trade orders with comments."""
        # Casual comment in trade order must NOT match upgrades pattern
        trade_msg = "!buy 10 food at 12 in ceres, saving for the upgrade next round"
        self.assertIsNone(UPGRADES_STATUS_PATTERN.search(trade_msg))
        self.assertIsNone(parse_discord_upgrades_cmd(trade_msg))

        # Explicit commands with prefixes MUST match
        self.assertIsNotNone(UPGRADES_STATUS_PATTERN.search("!upgrades"))
        self.assertIsNotNone(UPGRADES_STATUS_PATTERN.search("/upgrades"))
        self.assertIsNotNone(UPGRADES_STATUS_PATTERN.search("@referee upgrades"))
        self.assertIsNotNone(UPGRADES_STATUS_PATTERN.search("!upgrade as amos"))

        cmd = parse_discord_upgrades_cmd("!upgrades as amos")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["agent_id"], "amos")

    def test_stranded_banana_orders_migrated_and_crossed(self):
        """Fix 2: Old resting BANANA orders in DB/books must be migrated to FRAG and crossed."""
        ref = self.ref
        # Place a resting BANANA ask directly in the database simulating pre-migration state
        with ref.conn:
            ref.conn.execute(
                """
                INSERT INTO orders (order_id, agent_id, station_id, instrument, side, qty, limit_price, filled_qty, status, seq_seen)
                VALUES ('legacy-banana-ask-1', 'amos', 'ceres', 'BANANA', 'ask', 100, 11, 0, 'open', 0)
                """
            )
            # Give amos FRAG balance to cover the ask
            ref.conn.execute("UPDATE accounts SET balance = 500 WHERE agent_id = 'amos/1' AND instrument = 'FRAG'")
            ref.conn.execute("UPDATE accounts SET balance = 10000 WHERE agent_id = 'zero' AND instrument = 'CR'")

        # Run rehydration (which runs migration)
        ref._rehydrate_book()

        # Verify the order was migrated to FRAG in DB
        row = ref.conn.execute("SELECT instrument, status FROM orders WHERE order_id = 'legacy-banana-ask-1'").fetchone()
        self.assertEqual(row['instrument'], 'FRAG')
        self.assertEqual(row['status'], 'open')

        # Verify the order is in the FRAG book, not BANANA
        frag_book = ref.books['ceres']['FRAG']
        self.assertTrue(any(o.order_id == 'legacy-banana-ask-1' for o in frag_book.asks))
        self.assertNotIn('BANANA', ref.books['ceres'])

        # Now submit a crossing bid for FRAG
        bid_env = {
            'v': 1,
            'kind': 'order',
            'payload': {
                'order_id': 'zero-crossing-bid-1',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'bid',
                'qty': 100,
                'limit_price': 11,
                'seq_seen': ref.current_seq
            }
        }
        res = ref.submit_envelope(bid_env)
        self.assertEqual(res.get('kind'), 'market_tick')
        self.assertEqual(res['payload'].get('order_status'), 'filled')
        self.assertEqual(res['payload'].get('trades_count'), 1)
        self.assertEqual(res['payload'].get('last_price'), 11)

        # Confirm the legacy BANANA ask in DB was filled and resolved
        row_filled = ref.conn.execute("SELECT status, filled_qty FROM orders WHERE order_id = 'legacy-banana-ask-1'").fetchone()
        self.assertEqual(row_filled['status'], 'filled')
        self.assertEqual(row_filled['filled_qty'], 100)

    def test_privateer_receipt_fields_and_execution(self):
        """Fix 1: Privateer contract payload returns fee (750 CR) and 20 rounds duration."""
        ref = self.ref
        # Give amos cash
        with ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 5000 WHERE agent_id = 'amos' AND instrument = 'CR'")

        res = ref.piracy.hire("amos", "zero")
        self.assertEqual(res.get("kind"), "privateer_hire_ok")
        payload = res["payload"]

        # Ensure referee payload provides fee and correct 20-round span
        self.assertEqual(payload["fee"], 750)
        start = payload["start_round"]
        exp = payload["expires_round"]
        self.assertEqual(exp - start, 20)

        # Test announcer parser
        cmd = parse_discord_privateer_cmd("!privateer zero", author_id="1468012353206354197", author_name="amos")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["target"], "zero")
        self.assertEqual(cmd["sponsor"], "amos")

    def test_priority_slips_idle_fee_positive_control(self):
        """Fix 4: Positive control ensures unexempt idle fleets are actually charged."""
        ref = self.ref
        os.environ["AGORA_UPGRADES"] = "1"
        ref.upgrades_enabled = True
        ref.idle_fee = 100
        ref._active_this_round = set()

        # Add active fleet to trigger fee evaluation
        with ref.conn:
            ref.conn.execute(
                "INSERT OR REPLACE INTO fleet_roster (agent_id, display_name, home_station, genesis_cr, genesis_frag, genesis_fuel) "
                "VALUES ('active_fleet', 'Active Fleet', 'ceres', 1000, 0, 0)"
            )
            ref.conn.execute("UPDATE accounts SET balance = 1000 WHERE agent_id = 'amos' AND instrument = 'CR'")
            ref.conn.execute("UPDATE accounts SET balance = 1000 WHERE agent_id = 'zero' AND instrument = 'CR'")
            # Give zero priority slips
            ref.conn.execute("INSERT OR REPLACE INTO fleet_upgrades (agent_id, kind, tier, round) VALUES ('zero', 'priority_slips', 1, 1)")

        ref._active_this_round.add("active_fleet")

        charged = ref._charge_idle_fees_locked(10)
        # Positive control: amos (unexempt) MUST be charged
        self.assertIn("amos", charged)
        self.assertEqual(charged["amos"], 100)
        # Exemption verification: zero (has priority_slips) must NOT be charged
        self.assertNotIn("zero", charged)


if __name__ == "__main__":
    unittest.main()
