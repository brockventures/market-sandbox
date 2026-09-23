import unittest

from agora.referee import AgoraReferee
from agora.exchange import EXCHANGE_ID, MAX_SHARES


def game(**kw):
    ref = AgoraReferee(rival_shares=100, exchange_shares=100, **kw)
    ref.new_game(seed=7, warmup_rounds=3, rival_shares=100, exchange_shares=100)
    return ref


class TestEquityExchange(unittest.TestCase):
    def test_off_by_default(self):
        ref = AgoraReferee(rival_shares=100)
        ref.new_game(seed=7, warmup_rounds=3)
        self.assertEqual(ref.get_balance(EXCHANGE_ID, 'EQ_AMOS'), 0)
        self.assertIsNone(ref.books['ceres']['EQ_AMOS'].best_ask())

    def test_quotes_from_boot_without_new_game(self):
        # The live server builds the referee and may sit at round 0.
        ref = AgoraReferee(rival_shares=100, exchange_shares=100)
        book = ref.books['ceres']['EQ_ZERO']
        self.assertIsNotNone(book.best_bid())
        self.assertIsNotNone(book.best_ask())

    def test_genesis_takes_shares_from_treasury_not_minted(self):
        ref = game()
        self.assertEqual(ref.get_balance(EXCHANGE_ID, 'EQ_AMOS'), 100)
        self.assertEqual(ref.get_balance('amos', 'EQ_AMOS'), 600)
        self.assertEqual(ref.get_balance('zero', 'EQ_AMOS'), 100)
        total = sum(r[0] for r in ref.conn.execute(
            "SELECT balance FROM accounts WHERE instrument = 'EQ_AMOS' AND agent_id != 'SYSTEM'"))
        self.assertEqual(total, 1000)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_quotes_both_sides_from_genesis_and_every_round(self):
        ref = game()
        for _ in range(3):
            for sym in ('EQ_AMOS', 'EQ_MARV', 'EQ_ZERO', 'EQ_AERL'):
                book = ref.books['ceres'][sym]
                self.assertIsNotNone(book.best_bid(), sym)
                self.assertIsNotNone(book.best_ask(), sym)
                self.assertLess(book.best_bid(), book.best_ask())
            ref.step_round()

    def test_not_on_leaderboard_and_does_not_change_genesis_net_worth(self):
        with_x = {b['agent_id']: b['net_worth'] for b in game().get_leaderboard()}
        plain = AgoraReferee(rival_shares=100)
        plain.new_game(seed=7, warmup_rounds=3)
        without = {b['agent_id']: b['net_worth'] for b in plain.get_leaderboard()}
        self.assertNotIn(EXCHANGE_ID, with_x)
        # Rival stocks are marked at the book mid, which the exchange now sets
        # instead of NAV, so compare net worth before stocks.
        base = lambda r: {b['agent_id']: b['net_worth'] - b['stocks_value'] for b in r.get_leaderboard()}
        self.assertEqual(base(game()), base(plain))
        self.assertEqual(set(with_x), set(without))

    def test_a_fleet_can_buy_and_sell_against_it(self):
        ref = game()
        ask = ref.books['ceres']['EQ_ZERO'].best_ask()
        r = ref.submit_envelope({"v": 1, "kind": "order", "payload": {
            "order_id": "t-buy", "agent_id": "amos", "side": "bid", "qty": 5, "limit_price": ask,
            "instrument": "EQ_ZERO", "station_id": "ceres", "seq_seen": ref.current_seq}})
        self.assertEqual(r['payload']['trades_count'], 1)
        self.assertEqual(ref.get_balance('amos', 'EQ_ZERO'), 105)
        self.assertEqual(ref.get_balance(EXCHANGE_ID, 'EQ_ZERO'), 95)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_buying_pushes_the_price_up(self):
        def ask_after(buy):
            ref = AgoraReferee(rival_shares=100, exchange_shares=100, exchange_vol=0)
            ref.new_game(seed=7, warmup_rounds=3, rival_shares=100, exchange_shares=100, exchange_vol=0)
            if buy:
                ask = ref.books['ceres']['EQ_ZERO'].best_ask()
                ref.submit_envelope({"v": 1, "kind": "order", "payload": {
                    "order_id": "t-b", "agent_id": "amos", "side": "bid", "qty": 20, "limit_price": ask,
                    "instrument": "EQ_ZERO", "station_id": "ceres", "seq_seen": ref.current_seq}})
            ref.step_round()
            return ref.books['ceres']['EQ_ZERO'].best_ask()
        self.assertGreater(ask_after(True), ask_after(False))

    def test_seeded_games_reproduce_prices(self):
        def path():
            ref = game()
            out = []
            for _ in range(10):
                ref.step_round()
                out.append(ref.books['ceres']['EQ_MARV'].best_ask())
            return out
        self.assertEqual(path(), path())

    def test_share_cap_keeps_takeover_out_of_reach(self):
        ref = AgoraReferee(rival_shares=100, exchange_shares=999)
        self.assertEqual(ref.exchange_shares, MAX_SHARES)
        self.assertLess(3 * 100 + MAX_SHARES, 510)

    def test_replenish_from_system_when_cash_depleted(self):
        ref = game()
        # Artificially drain exchange cash below floor
        with ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 5000 WHERE agent_id = ? AND instrument = 'CR'", (EXCHANGE_ID,))
            ref.conn.execute("UPDATE accounts SET balance = balance + 95000 WHERE agent_id = 'SYSTEM' AND instrument = 'CR'")
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('drain', 0, ?, 'CR', -95000)", (EXCHANGE_ID,))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('drain', 0, 'SYSTEM', 'CR', 95000)")
        self.assertEqual(ref.get_balance(EXCHANGE_ID, 'CR'), 5000)

        ref.step_round()
        # Should be replenished back to SEED_CR (100_000)
        self.assertEqual(ref.get_balance(EXCHANGE_ID, 'CR'), 100_000)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_equitable_budget_allocation_across_symbols(self):
        ref = game()
        # Leave exchange with constrained cash so symbols must share
        with ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 30000 WHERE agent_id = ? AND instrument = 'CR'", (EXCHANGE_ID,))
            ref.conn.execute("UPDATE accounts SET balance = balance + 70000 WHERE agent_id = 'SYSTEM' AND instrument = 'CR'")
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('drain', 0, ?, 'CR', -70000)", (EXCHANGE_ID,))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('drain', 0, 'SYSTEM', 'CR', 70000)")
        
        # Manually refresh without stepping round so replenishment floor isn't triggered
        with ref.lock, ref.conn:
            ref.exchange.refresh_locked()

        # Verify all 4 symbols have active bids rather than only the first one
        for sym in ('EQ_AERL', 'EQ_AMOS', 'EQ_MARV', 'EQ_ZERO'):
            book = ref.books['ceres'][sym]
            bid = book.best_bid()
            self.assertIsNotNone(bid, f"Symbol {sym} missing bid under constrained budget")
            bids = [o for o in book.bids if o.agent_id == EXCHANGE_ID]
            self.assertGreater(len(bids), 0)


if __name__ == '__main__':
    unittest.main()
