"""
tests/test_depot_solvency.py - depot quotes are capped by what the depot can
actually pay or deliver, so fleets selling into a depot can never drive its
balance negative. Found with tools/economy_sim.py: sustained hauling drove
depot_earth to -109,025 CR and failed verify_ledger_invariants().
"""

import unittest

from agora.referee import AgoraReferee


class TestDepotSolvency(unittest.TestCase):
    def _move(self, ref, frm, to, inst, qty):
        with ref.conn:
            for acct, d in ((frm, -qty), (to, qty)):
                ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
                ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('t', 0, ?, ?, ?)", (acct, inst, d))

    def test_poor_depot_cannot_be_overdrawn(self):
        ref = AgoraReferee(depots=True, asymmetric=True)
        # Leave depot_earth with 1,000 CR and give zero (docked at Earth) 1,000 ORE.
        cr = ref.get_balance('depot_earth', 'CR')
        self._move(ref, 'depot_earth', 'SYSTEM', 'CR', cr - 1000)
        self._move(ref, 'SYSTEM', 'zero', 'ORE', 1000)
        ref.refresh_depot_liquidity()

        bid_notional = sum(o.limit_price * o.remaining_qty
                           for books in [ref.books['earth']] for b in books.values()
                           for o in b.bids if o.agent_id == 'depot_earth')
        self.assertLessEqual(bid_notional, 1000)

        # Dump all 1,000 ORE at any price the depot will take.
        ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': {
            'order_id': 'dump', 'agent_id': 'zero', 'side': 'ask', 'qty': 1000,
            'limit_price': 1, 'instrument': 'ORE', 'station_id': 'earth',
            'seq_seen': ref.current_seq}})
        for _ in range(3):
            ref.step_round()
        self.assertGreaterEqual(ref.get_balance('depot_earth', 'CR'), 0)
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_depot_never_offers_more_than_it_holds(self):
        ref = AgoraReferee(depots=True, asymmetric=True)
        held = ref.get_balance('depot_ceres', 'ORE')
        self._move(ref, 'depot_ceres', 'SYSTEM', 'ORE', held - 200)
        ref.refresh_depot_liquidity()
        offered = sum(o.remaining_qty for o in ref.books['ceres']['ORE'].asks if o.agent_id == 'depot_ceres')
        self.assertLessEqual(offered, 200)


if __name__ == '__main__':
    unittest.main()
