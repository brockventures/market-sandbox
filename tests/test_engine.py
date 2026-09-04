"""
tests/test_engine.py - Complete unit test suite for AGORA OrderBook and AgoraReferee.
Exercises order crossing, price-time priority, double-entry ledger conservation,
solvency validation, and idempotency dedup.
"""

import unittest
from agora.order_book import OrderBook, Order
from agora.referee import AgoraReferee


class TestAgoraEngine(unittest.TestCase):
    def test_genesis_invariants(self):
        referee = AgoraReferee()
        valid, errors = referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Genesis invariants failed: {errors}")
        self.assertEqual(referee.get_balance('amos', 'CASH'), 10000)
        self.assertEqual(referee.get_balance('amos', 'BANANA'), 1000)
        self.assertEqual(referee.get_balance('SYSTEM', 'CASH'), -30000)

    def test_order_matching_and_double_entry_settlement(self):
        referee = AgoraReferee()

        # Amos places an ask: Sell 100 BANANA @ 12
        ask_env = {
            'v': 1,
            'kind': 'order',
            'payload': {
                'order_id': 'ord-amos-001',
                'agent_id': 'amos',
                'instrument': 'BANANA',
                'side': 'ask',
                'qty': 100,
                'limit_price': 12,
                'seq_seen': referee.current_seq
            }
        }
        tick1 = referee.submit_envelope(ask_env)
        self.assertEqual(tick1['kind'], 'market_tick')
        self.assertEqual(tick1['payload']['best_ask'], 12)
        self.assertEqual(tick1['payload']['trades_count'], 0)

        # Zero places a crossing bid: Buy 60 BANANA @ 15
        bid_env = {
            'v': 1,
            'kind': 'order',
            'payload': {
                'order_id': 'ord-zero-001',
                'agent_id': 'zero',
                'instrument': 'BANANA',
                'side': 'bid',
                'qty': 60,
                'limit_price': 15,
                'seq_seen': referee.current_seq
            }
        }
        tick2 = referee.submit_envelope(bid_env)
        self.assertEqual(tick2['kind'], 'market_tick')
        self.assertEqual(tick2['payload']['trades_count'], 1)
        self.assertEqual(tick2['payload']['last_price'], 12)  # Filled at resting ask price
        self.assertEqual(tick2['payload']['last_qty'], 60)

        # Verify Account Balances:
        # Execution cost = 60 * 12 = 720 CASH
        # Zero (buyer): CASH 10000 - 720 = 9280, BANANA 1000 + 60 = 1060
        # Amos (seller): CASH 10000 + 720 = 10720, BANANA 1000 - 60 = 940
        self.assertEqual(referee.get_balance('zero', 'CASH'), 9280)
        self.assertEqual(referee.get_balance('zero', 'BANANA'), 1060)
        self.assertEqual(referee.get_balance('amos', 'CASH'), 10720)
        self.assertEqual(referee.get_balance('amos', 'BANANA'), 940)

        # Verify Ledger Invariants (sum delta == 0)
        valid, errors = referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariants breached after trade: {errors}")

    def test_solvency_rejection_buyer(self):
        referee = AgoraReferee()

        # Marvin attempts to buy 2000 BANANA @ 20 (cost 40,000, balance only 10,000)
        bid_env = {
            'v': 1,
            'kind': 'order',
            'payload': {
                'order_id': 'ord-marvin-insolvent',
                'agent_id': 'marvin',
                'instrument': 'BANANA',
                'side': 'bid',
                'qty': 2000,
                'limit_price': 20,
                'seq_seen': referee.current_seq
            }
        }
        reject = referee.submit_envelope(bid_env)
        self.assertEqual(reject['kind'], 'reject')
        self.assertEqual(reject['payload']['reason'], 'insufficient_balance')

        # Ensure balances and invariants completely undisturbed
        self.assertEqual(referee.get_balance('marvin', 'CASH'), 10000)
        valid, errors = referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Invariants breached on reject: {errors}")

    def test_solvency_rejection_seller(self):
        referee = AgoraReferee()

        # Marvin attempts to sell 5000 BANANA (balance only 1000)
        ask_env = {
            'v': 1,
            'kind': 'order',
            'payload': {
                'order_id': 'ord-marvin-oversell',
                'agent_id': 'marvin',
                'instrument': 'BANANA',
                'side': 'ask',
                'qty': 5000,
                'limit_price': 5,
                'seq_seen': referee.current_seq
            }
        }
        reject = referee.submit_envelope(ask_env)
        self.assertEqual(reject['kind'], 'reject')
        self.assertEqual(reject['payload']['reason'], 'insufficient_balance')

    def test_idempotency_dedup(self):
        referee = AgoraReferee()

        order_env = {
            'v': 1,
            'kind': 'order',
            'payload': {
                'order_id': 'ord-idemp-1',
                'agent_id': 'zero',
                'instrument': 'BANANA',
                'side': 'ask',
                'qty': 10,
                'limit_price': 25,
                'seq_seen': referee.current_seq
            }
        }
        res1 = referee.submit_envelope(order_env)
        self.assertEqual(res1['kind'], 'market_tick')

        # Resubmit identical order ID -> no-op
        res2 = referee.submit_envelope(order_env)
        self.assertEqual(res2['status'], 'noop_duplicate')

        # Resubmit with conflicting parameters -> duplicate_order reject
        bad_order_env = dict(order_env)
        bad_order_env['payload'] = dict(order_env['payload'])
        bad_order_env['payload']['limit_price'] = 99
        res3 = referee.submit_envelope(bad_order_env)
        self.assertEqual(res3['kind'], 'reject')
        self.assertEqual(res3['payload']['reason'], 'duplicate_order')

    def test_leaderboard_scoring(self):
        referee = AgoraReferee()
        board = referee.get_leaderboard()
        self.assertEqual(len(board), 3)
        # Flat start: 10000 cash + 1000 bananas * 10 = 20000
        for entry in board:
            self.assertEqual(entry['net_worth'], 20000)


if __name__ == '__main__':
    unittest.main()