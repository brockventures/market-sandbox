"""
tests/test_auction_new_instrument_credit.py

A circuit-breaker reopening auction must credit the buyer even when the
buyer has never held that instrument. The auction settled with a bare
UPDATE on accounts, which is a silent no-op when the (agent, instrument)
row does not exist yet: the buyer's CR was debited and the commodity was
never credited. Found live 2026-09-22: amos bid 1,010 ORE at Ceres,
paid 11,110 CR in the reopen auction, and ended with 0 ORE.
"""

import unittest

from agora.referee import AgoraReferee


class TestAuctionNewInstrumentCredit(unittest.TestCase):
    def _seed(self, ref, agent, inst, qty):
        # A fleet's goods live on its ship 1 ('<agent>/1') since #175.
        if inst != 'CR' and ref.fleet.is_corp(agent):
            agent = f"{agent}/1"
        with ref.conn:
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES ('SYSTEM', ?, 0)", (inst,))
            ref.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id='SYSTEM' AND instrument=?", (qty, inst))
            ref.conn.execute("INSERT INTO accounts (agent_id, instrument, balance) VALUES (?, ?, ?)", (agent, inst, qty))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('seed', 0, 'SYSTEM', ?, ?)", (inst, -qty))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('seed', 0, ?, ?, ?)", (agent, inst, qty))

    def _order(self, ref, oid, agent, side, qty, px):
        return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': {
            'order_id': oid, 'agent_id': agent, 'side': side, 'qty': qty,
            'limit_price': px, 'instrument': 'ORE', 'station_id': 'ceres',
            'seq_seen': ref.current_seq}})

    def test_reopen_auction_credits_first_time_holder(self):
        ref = AgoraReferee()
        self._seed(ref, 'zero', 'ORE', 100)
        self.assertEqual(ref.get_balance('amos', 'ORE'), 0)
        cr_before = ref.get_balance('amos', 'CR')

        ref.circuit_breaker.trigger_halt('ceres', 'ORE', 10, 'test', ref.current_round)
        self._order(ref, 'ask-1', 'zero', 'ask', 50, 10)
        self._order(ref, 'bid-1', 'amos', 'bid', 50, 10)

        res = ref.circuit_breaker.execute_auction_reopen('ceres', 'ORE', ref.current_round + 2)
        self.assertTrue(res.get('ok', True), res)

        self.assertEqual(ref.get_balance('amos', 'ORE'), 50)
        self.assertEqual(ref.get_balance('amos', 'CR'), cr_before - 500)
        self.assertEqual(ref.get_balance('zero', 'ORE'), 50)
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)


if __name__ == '__main__':
    unittest.main()
