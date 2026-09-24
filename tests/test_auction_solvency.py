"""#204: a circuit-breaker call auction filled a resting bid whose CR had
been taken since it was placed, driving the fleet's CR negative.

Funding is checked when an order is placed. A halted book holds its orders
for halt_duration rounds, and in that time a debt payment, contract
penalty, sabotage fine or borrow fee may take CR a resting bid had
committed (by design, #162). The continuous book cancels such orders before
matching (_prune_unfunded_locked); the auction uncross did not. The fuzzer
chain was: a penalty left the corp in debt, the next step_round's corporate
step paid the debt out of the committed CR, and the auction reopen later in
that same step_round filled the stale bid.
"""

import unittest

from agora.referee import AgoraReferee


def put(ref, agent, side, qty, price, tag):
    return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': {
        'order_id': f'{agent}-{tag}', 'agent_id': agent, 'side': side, 'qty': qty,
        'limit_price': price, 'instrument': 'FRAG', 'station_id': 'ceres', 'seq_seen': 0}})


def drain(ref, agent, inst, keep):
    """Take everything above `keep` to SYSTEM in one balanced entry, the way
    a fine or a debt payment would."""
    if inst != 'CR' and ref.fleet.is_corp(agent):
        agent = f"{agent}/1"  # a fleet's goods are on its ship 1 (#175)
    gone = ref.get_balance(agent, inst) - keep
    with ref.conn:
        for acct, d in ((agent, -gone), ('SYSTEM', gone)):
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('t-drain', 0, ?, ?, ?)",
                             (acct, inst, d))


def status(ref, order_id):
    return ref.conn.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()[0]


def auction_rows(ref):
    return ref.conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE txn_id LIKE 'auction-%'").fetchone()[0]


class TestAuctionSolvency(unittest.TestCase):

    def halted(self, **kw):
        ref = AgoraReferee(**kw)
        ref.circuit_breaker.trigger_halt('ceres', 'FRAG', trigger_price=30.0, reason='test', current_round=ref.current_round)
        self.assertEqual(put(ref, 'zero', 'bid', 100, 30, 'stale')['payload'].get('auction_resting'), True)
        self.assertEqual(put(ref, 'marvin', 'ask', 100, 25, 'ask')['payload'].get('auction_resting'), True)
        return ref

    def reopen_by_stepping(self, ref):
        for _ in range(ref.circuit_breaker.halt_duration):
            ref.step_round()
        self.assertFalse(ref.circuit_breaker.is_halted('ceres', 'FRAG'))

    def assert_clean(self, ref):
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_stale_bid_is_cancelled_not_filled_at_reopen(self):
        ref = self.halted()
        frag0 = ref.get_balance('marvin', 'FRAG')
        drain(ref, 'zero', 'CR', 50)          # 3,000 CR committed, 50 left
        self.reopen_by_stepping(ref)
        self.assertEqual(ref.get_balance('zero', 'CR'), 50)
        self.assertEqual(status(ref, 'zero-stale'), 'cancelled')
        self.assertEqual(ref.get_balance('marvin', 'FRAG'), frag0)
        self.assertEqual(auction_rows(ref), 0)
        self.assert_clean(ref)

    def test_manual_reopen_also_prunes(self):
        ref = self.halted()
        drain(ref, 'zero', 'CR', 0)
        res = ref.reopen_circuit_breaker_auction('ceres', 'FRAG')
        self.assertTrue(res['ok'])
        self.assertEqual(res['trades'], [])
        self.assertEqual(res['reopen_volume'], 0)
        self.assertEqual(ref.get_balance('zero', 'CR'), 0)
        self.assert_clean(ref)

    def test_stale_ask_is_cancelled_too(self):
        ref = self.halted()
        cr0 = ref.get_balance('zero', 'CR')
        drain(ref, 'marvin', 'FRAG', 3)       # 100 FRAG committed, 3 left
        self.reopen_by_stepping(ref)
        self.assertEqual(ref.get_balance('marvin', 'FRAG'), 3)
        self.assertEqual(status(ref, 'marvin-ask'), 'cancelled')
        self.assertEqual(ref.get_balance('zero', 'CR'), cr0)
        self.assert_clean(ref)

    def test_debt_payment_in_the_same_step_round_then_auction(self):
        """The fuzzer's exact chain: corporate debt is paid out of committed
        CR by step_round, and the same step_round's auction reopen follows."""
        ref = AgoraReferee(corporate=True)
        ref.circuit_breaker.trigger_halt('ceres', 'FRAG', trigger_price=30.0, reason='test', current_round=ref.current_round)
        ref.step_round()                      # one round of the halt passes
        put(ref, 'zero', 'bid', 100, 30, 'stale')
        put(ref, 'marvin', 'ask', 100, 25, 'ask')
        cr = ref.get_balance('zero', 'CR')
        with ref.lock, ref.conn:
            ref.corporate.add_debt('zero', cr - 40, 'test penalty')
        ref.step_round()                      # pays the debt, then reopens
        self.assertFalse(ref.circuit_breaker.is_halted('ceres', 'FRAG'))
        self.assertEqual(ref.get_balance('zero', 'CR'), 40)
        self.assertEqual(status(ref, 'zero-stale'), 'cancelled')
        self.assertEqual(auction_rows(ref), 0)
        self.assert_clean(ref)

    def test_funded_orders_still_clear(self):
        ref = self.halted()
        cr0, frag0 = ref.get_balance('zero', 'CR'), ref.get_balance('zero', 'FRAG')
        self.reopen_by_stepping(ref)
        halt = ref.circuit_breaker.get_halts()[0]
        self.assertEqual(halt['reopen_volume'], 100)
        px = int(halt['reopen_price'])
        self.assertEqual(ref.get_balance('zero', 'CR'), cr0 - 100 * px)
        self.assertEqual(ref.get_balance('zero', 'FRAG'), frag0 + 100)
        self.assertEqual(status(ref, 'zero-stale'), 'filled')
        self.assert_clean(ref)

    def test_backstop_shrinks_a_fill_nothing_pruned(self):
        """Depots are not pruned (their quotes are re-funded at refresh), so
        the per-fill check is what keeps them solvent: an auction fill is cut
        to what the buyer's CR covers."""
        ref = self.halted()
        cb = ref.circuit_breaker
        book = ref.books['ceres']['FRAG']
        bid = next(o for o in book.bids if o.agent_id == 'zero')
        ask = next(o for o in book.asks if o.agent_id == 'marvin')
        drain(ref, 'zero', 'CR', 30 * 7 + 5)
        with ref.lock:
            qty, dropped = cb._fundable_qty(book, bid, ask, 30, 100, 'FRAG')
        self.assertEqual((qty, dropped), (7, False))
        drain(ref, 'zero', 'CR', 29)
        with ref.lock:
            qty, dropped = cb._fundable_qty(book, bid, ask, 30, 100, 'FRAG')
        self.assertEqual((qty, dropped), (0, True))
        self.assertNotIn(bid, book.bids)
        self.assertEqual(status(ref, 'zero-stale'), 'cancelled')


if __name__ == '__main__':
    unittest.main()
