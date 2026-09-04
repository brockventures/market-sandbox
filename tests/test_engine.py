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
        self.assertEqual(referee.get_balance('amos', 'CREDITS'), 10000)
        self.assertEqual(referee.get_balance('amos', 'BANANA'), 1000)
        self.assertEqual(referee.get_balance('SYSTEM', 'CREDITS'), -30000)

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
        # Execution cost = 60 * 12 = 720 CREDITS
        # Zero (buyer): CREDITS 10000 - 720 = 9280, BANANA 1000 + 60 = 1060
        # Amos (seller): CREDITS 10000 + 720 = 10720, BANANA 1000 - 60 = 940
        self.assertEqual(referee.get_balance('zero', 'CREDITS'), 9280)
        self.assertEqual(referee.get_balance('zero', 'BANANA'), 1060)
        self.assertEqual(referee.get_balance('amos', 'CREDITS'), 10720)
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
        self.assertEqual(referee.get_balance('marvin', 'CREDITS'), 10000)
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

    def test_resting_order_escrow_committed_exposure(self):
        referee = AgoraReferee()

        # Buyer has 10,000 CASH. Posts bid for 60 BANANA @ 100 (6,000). Rests.
        bid1 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'bid-1', 'agent_id': 'zero', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 60, 'limit_price': 100, 'seq_seen': referee.current_seq
            }
        }
        res1 = referee.submit_envelope(bid1)
        self.assertEqual(res1['kind'], 'market_tick')
        self.assertEqual(res1['payload']['trades_count'], 0)

        # Second bid for 60 BANANA @ 100 requires 6,000, but available is 10,000 - 6,000 = 4,000.
        bid2 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'bid-2', 'agent_id': 'zero', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 60, 'limit_price': 100, 'seq_seen': referee.current_seq
            }
        }
        res2 = referee.submit_envelope(bid2)
        self.assertEqual(res2['kind'], 'reject')
        self.assertEqual(res2['payload']['reason'], 'insufficient_balance')
        self.assertIn('committed', res2['payload']['detail'])

        # Third bid for 40 BANANA @ 100 requires 4,000 <= 4,000 available. Accepted!
        bid3 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'bid-3', 'agent_id': 'zero', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 40, 'limit_price': 100, 'seq_seen': referee.current_seq
            }
        }
        res3 = referee.submit_envelope(bid3)
        self.assertEqual(res3['kind'], 'market_tick')

        # Seller has 1,000 BANANA. Posts ask for 600 BANANA @ 100. Rests.
        ask1 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'ask-1', 'agent_id': 'amos', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 600, 'limit_price': 100, 'seq_seen': referee.current_seq
            }
        }
        res_ask1 = referee.submit_envelope(ask1)
        # Note: ask1 crosses resting bid1 (60) and bid3 (40), executing 100 BANANA total!
        # Remaining 500 BANANA rests on book.
        self.assertEqual(res_ask1['payload']['trades_count'], 2)
        self.assertEqual(referee.get_balance('zero', 'CREDITS'), 0)  # 10,000 - 6,000 - 4,000
        self.assertEqual(referee.get_balance('zero', 'BANANA'), 1100)
        self.assertEqual(referee.get_balance('amos', 'CREDITS'), 20000)
        self.assertEqual(referee.get_balance('amos', 'BANANA'), 900)

        # Amos now has 900 BANANA, with 500 committed in resting ask1. Available = 400.
        ask2 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'ask-2', 'agent_id': 'amos', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 500, 'limit_price': 100, 'seq_seen': referee.current_seq
            }
        }
        res_ask2 = referee.submit_envelope(ask2)
        self.assertEqual(res_ask2['kind'], 'reject')
        self.assertEqual(res_ask2['payload']['reason'], 'insufficient_balance')
        self.assertIn('committed', res_ask2['payload']['detail'])

        valid, errors = referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Invariants breached after fills: {errors}")

    def test_composite_pk_order_id_across_agents(self):
        referee = AgoraReferee()

        # Amos submits order_id 'ord-common-001'
        env_amos = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'ord-common-001', 'agent_id': 'amos', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 10, 'limit_price': 20, 'seq_seen': referee.current_seq
            }
        }
        res_amos = referee.submit_envelope(env_amos)
        self.assertEqual(res_amos['kind'], 'market_tick')

        # Marvin submits identical order_id 'ord-common-001' - must succeed because PK is (agent_id, order_id)
        env_marvin = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'ord-common-001', 'agent_id': 'marvin', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 15, 'limit_price': 25, 'seq_seen': referee.current_seq
            }
        }
        res_marvin = referee.submit_envelope(env_marvin)
        self.assertEqual(res_marvin['kind'], 'market_tick')

        # Re-submission by Marvin with same params is idempotent no-op
        res_marvin_dup = referee.submit_envelope(env_marvin)
        self.assertEqual(res_marvin_dup['status'], 'noop_duplicate')

    def test_cross_currency_mismatch_and_reconciliation_invariant(self):
        referee = AgoraReferee()

        curr = referee.get_currency_instrument('zero')
        mismatched = 'CASH' if curr == 'CREDITS' else 'CREDITS'

        # Simulate Amos's repro: rename zero's account to mismatched currency while amos remains on curr
        referee.conn.execute("UPDATE accounts SET instrument = ? WHERE agent_id = 'zero' AND instrument = ?", (mismatched, curr))

        # Invariant 3 immediately catches that zero's accounts row does not match ledger_entries sum!
        valid, errors = referee.verify_ledger_invariants()
        self.assertFalse(valid)
        self.assertTrue(any("Reconciliation breach" in e for e in errors))

        # Further, if zero attempts a cross trade against amos, settlement raises RuntimeError on currency mismatch
        # Amos posts ask on curr (rests on book)
        ask_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'ask-cross-curr', 'agent_id': 'amos', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 10, 'limit_price': 10, 'seq_seen': referee.current_seq
            }
        }
        referee.submit_envelope(ask_env)
        self.assertEqual(len(referee.book.asks), 1)

        # Zero attempts to cross with CREDITS -> Pre-match currency audit cleanly rejects without touching book
        bid_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'bid-cross-curr', 'agent_id': 'zero', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 10, 'limit_price': 10, 'seq_seen': referee.current_seq
            }
        }
        res_bid = referee.submit_envelope(bid_env)
        self.assertEqual(res_bid['kind'], 'reject')
        self.assertEqual(res_bid['payload']['reason'], 'currency_mismatch')

        # Crucial Marvin bug check: Amos's resting ask must STILL be on book and escrowed
        self.assertEqual(len(referee.book.asks), 1)
        self.assertEqual(referee.book.asks[0].order_id, 'ask-cross-curr')
        self.assertEqual(referee.book.asks[0].remaining_qty, 10)

    def test_book_rollback_on_settlement_failure(self):
        referee = AgoraReferee()

        # Amos rests an ask: 100 BANANA @ 10
        ask_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'ask-rollback', 'agent_id': 'amos', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 100, 'limit_price': 10, 'seq_seen': referee.current_seq
            }
        }
        referee.submit_envelope(ask_env)
        self.assertEqual(referee.book.depth(), (0, 100))

        # Monkeypatch get_currency_instrument during settlement to simulate unexpected error in settlement loop
        orig_get_curr = referee.get_currency_instrument
        def broken_get_curr(agent_id):
            if agent_id == 'amos':
                raise RuntimeError("Simulated transient failure during DB settlement")
            return orig_get_curr(agent_id)

        referee.get_currency_instrument = broken_get_curr

        bid_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'bid-fail', 'agent_id': 'zero', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 100, 'limit_price': 10, 'seq_seen': referee.current_seq
            }
        }

        with self.assertRaises(RuntimeError) as cm:
            referee.submit_envelope(bid_env)
        self.assertIn("Simulated transient failure", str(cm.exception))

        # Restore method
        referee.get_currency_instrument = orig_get_curr

        # Verify book was rolled back atomically: Amos's ask is preserved, depth is still (0, 100)
        self.assertEqual(referee.book.depth(), (0, 100))
        self.assertEqual(len(referee.book.asks), 1)
        self.assertEqual(referee.book.asks[0].order_id, 'ask-rollback')
        self.assertEqual(referee.book.asks[0].remaining_qty, 100)

    def test_pre_match_currency_gate_quantity_accounting(self):
        referee = AgoraReferee()
        curr = referee.get_currency_instrument('marvin')
        mismatched = 'CASH' if curr == 'CREDITS' else 'CREDITS'

        # 1. Marvin rests ask: 10 @ 10
        ask_marvin = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'ask-marvin-10', 'agent_id': 'marvin', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 10, 'limit_price': 10, 'seq_seen': referee.current_seq
            }
        }
        res_m = referee.submit_envelope(ask_marvin)
        self.assertEqual(res_m['kind'], 'market_tick')

        # 2. Reassign Zero to mismatched currency to simulate mixed book during migration
        referee.conn.execute("UPDATE accounts SET instrument = ? WHERE agent_id = 'zero' AND instrument = ?", (mismatched, curr))

        # Zero rests ask: 10 @ 10 right behind Marvin in time priority
        ask_zero = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'ask-zero-10', 'agent_id': 'zero', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 10, 'limit_price': 10, 'seq_seen': referee.current_seq
            }
        }
        res_z = referee.submit_envelope(ask_zero)
        self.assertEqual(res_z['kind'], 'market_tick')
        self.assertEqual(len(referee.book.asks), 2)

        # 3. Amos submits bid for 10 @ 10
        # Fully satisfied by Marvin's resting ask; should NOT be poisoned by Zero's resting ask behind it
        bid_amos_1 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'bid-amos-10', 'agent_id': 'amos', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 10, 'limit_price': 10, 'seq_seen': referee.current_seq
            }
        }
        res_amos_1 = referee.submit_envelope(bid_amos_1)
        self.assertEqual(res_amos_1['kind'], 'market_tick')
        self.assertEqual(res_amos_1['payload']['trades_count'], 1)
        self.assertEqual(referee.get_balance('marvin', curr), 10100)
        self.assertEqual(referee.get_balance('amos', curr), 9900)

        # Zero's ask remains on the book untouched
        self.assertEqual(len(referee.book.asks), 1)
        self.assertEqual(referee.book.asks[0].order_id, 'ask-zero-10')

        # 4. Amos submits second bid for 10 @ 10
        # This one WOULD consume Zero's mismatched ask -> cleanly rejected by pre-match gate
        bid_amos_2 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'bid-amos-20', 'agent_id': 'amos', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 10, 'limit_price': 10, 'seq_seen': referee.current_seq
            }
        }
        res_amos_2 = referee.submit_envelope(bid_amos_2)
        self.assertEqual(res_amos_2['kind'], 'reject')
        self.assertEqual(res_amos_2['payload']['reason'], 'currency_mismatch')
        self.assertEqual(len(referee.book.asks), 1)
        self.assertEqual(referee.book.asks[0].order_id, 'ask-zero-10')

    def test_restart_rehydration_and_oversell_prevention(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            # Phase 1: Initialize referee with file-backed DB and place resting ask
            ref1 = AgoraReferee(db_path=db_path)
            # Amos has 1000 BANANA initially
            ask_env = {
                'v': 1, 'kind': 'order',
                'payload': {
                    'order_id': 'persist-1',
                    'agent_id': 'amos',
                    'instrument': 'BANANA',
                    'side': 'ask',
                    'qty': 1000,
                    'limit_price': 10,
                    'seq_seen': ref1.current_seq
                }
            }
            res = ref1.submit_envelope(ask_env)
            self.assertEqual(res['kind'], 'market_tick')
            self.assertEqual(len(ref1.book.asks), 1)
            ref1.conn.close()

            # Phase 2: Simulate process restart (new instance against same db file)
            ref2 = AgoraReferee(db_path=db_path)
            # Verify book was rehydrated
            self.assertEqual(len(ref2.book.asks), 1, "Resting asks must be rehydrated from orders table on startup")
            self.assertEqual(ref2.book.asks[0].order_id, 'persist-1')
            self.assertEqual(ref2.book.asks[0].remaining_qty, 1000)

            # Verify oversell prevention: Amos attempts to sell another 10 BANANA, but all 1000 are committed
            oversell_env = {
                'v': 1, 'kind': 'order',
                'payload': {
                    'order_id': 'persist-2',
                    'agent_id': 'amos',
                    'instrument': 'BANANA',
                    'side': 'ask',
                    'qty': 10,
                    'limit_price': 10,
                    'seq_seen': ref2.current_seq
                }
            }
            rej = ref2.submit_envelope(oversell_env)
            self.assertEqual(rej['kind'], 'reject')
            self.assertEqual(rej['payload']['reason'], 'insufficient_balance')

            # Phase 3: Zero crosses 400 BANANA against the rehydrated ask (cost: 400 * 10 = 4,000 CREDITS <= 10,000)
            bid_env = {
                'v': 1, 'kind': 'order',
                'payload': {
                    'order_id': 'persist-3',
                    'agent_id': 'zero',
                    'instrument': 'BANANA',
                    'side': 'bid',
                    'qty': 400,
                    'limit_price': 10,
                    'seq_seen': ref2.current_seq
                }
            }
            trade_res = ref2.submit_envelope(bid_env)
            self.assertEqual(trade_res['kind'], 'market_tick')
            self.assertEqual(trade_res['payload']['trades_count'], 1)
            self.assertEqual(len(ref2.book.asks), 1)
            self.assertEqual(ref2.book.asks[0].remaining_qty, 600)
            ref2.conn.close()

            # Phase 4: Second restart after partial fill
            ref3 = AgoraReferee(db_path=db_path)
            self.assertEqual(len(ref3.book.asks), 1)
            self.assertEqual(ref3.book.asks[0].remaining_qty, 600)
            self.assertEqual(ref3.book.asks[0].filled_qty, 400)
            ref3.conn.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


if __name__ == '__main__':
    unittest.main()