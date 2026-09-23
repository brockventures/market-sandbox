"""
tests/test_transit_cancel_no_deadlock.py

initiate_transit() cancels the agent's resting orders at the origin
station while holding self.lock. It used to call cancel_order(), which
takes the same non-reentrant lock, so launching a transit with any open
order at the origin hung that thread forever with the lock held. Every
later order, cancel and burst step_round() then blocked behind it. Found
live 2026-09-22: burst-1790128076-11be51 froze at round 5 at 02:02 UTC.
"""

import threading
import unittest

from agora.referee import AgoraReferee


class TestTransitCancelNoDeadlock(unittest.TestCase):
    def test_transit_with_resting_order_at_origin_completes(self):
        ref = AgoraReferee(depots=True)
        placed = ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': {
            'order_id': 'rest-1', 'agent_id': 'amos', 'side': 'ask', 'qty': 5,
            'limit_price': 40, 'instrument': 'FRAG', 'station_id': 'ceres',
            'seq_seen': ref.current_seq}})
        self.assertEqual(placed.get('kind'), 'market_tick')

        result = {}
        th = threading.Thread(target=lambda: result.update(
            ref.initiate_transit(agent_id='amos', destination='earth', commodity='FRAG', cargo_qty=10)),
            daemon=True)
        th.start()
        th.join(5)
        self.assertFalse(th.is_alive(), "initiate_transit deadlocked on self.lock")
        self.assertFalse(ref.lock.locked())
        self.assertEqual(result.get('status'), 'in_transit')

        row = ref.conn.execute("SELECT status FROM orders WHERE order_id = 'rest-1'").fetchone()
        self.assertEqual(row['status'], 'cancelled')

        # The referee must keep working afterwards.
        ref.step_round()
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)


if __name__ == '__main__':
    unittest.main()
