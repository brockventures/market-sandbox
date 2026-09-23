"""
tests/test_exchange_depth.py - the exchange's depth follows traded volume,
and its holdings never pass MAX_SHARES (#187 track 1).

Takeover is 51% of 1,000 shares. A raider starts with 100 of a rival and can
buy the other two rivals' 200; everything else it can only get from the
issuer or the exchange. So the exchange holding at most 200 is what keeps a
raider at 500 through the exchange alone, one share short of 501.

Every game here is the live one: tools/economy_sim.start_game builds it with
agora.server.build_referee_from_env and a bare new_game.
"""

import unittest

from agora.exchange import EXCHANGE_ID, MAX_SHARES, MIN_DEPTH, MAX_DEPTH, VOLUME_ROUNDS
from tools import economy_sim as sim

TAKEOVER_AT = 501  # total_shares // 2 + 1; corporate.TAKEOVER_SHARES (510) is looser


def _sell_all(ref, agent, sym):
    """Hit the best bid with everything the agent holds, then pull any rest."""
    bid = ref.books['ceres'][sym].best_bid()
    have = sim.available(ref, agent, sym)
    if bid and have > 0:
        sim.order(ref, agent, 'ask', have, bid, sym, 'ceres', 'dump')
    sim.cancel_stock_orders(ref, agent)


def _buy_all(ref, agent, sym):
    """Take every share offered up to twice the best ask, then pull any rest."""
    ask = ref.books['ceres'][sym].best_ask()
    cash = sim.available(ref, agent, 'CR')
    if ask and cash >= 2 * ask:
        sim.order(ref, agent, 'bid', cash // (2 * ask), 2 * ask, sym, 'ceres', 'take')
    sim.cancel_stock_orders(ref, agent)


def _grant(ref, agent, cr):
    """Extra CR from SYSTEM on a balanced ledger leg."""
    with ref.lock, ref.conn:
        for acct, d in ((agent, cr), ('SYSTEM', -cr)):
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'",
                             (d, acct))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) "
                             "VALUES ('test-grant', ?, ?, 'CR', ?)", (ref.current_seq, acct, d))


class TestVolumeScaledDepth(unittest.TestCase):
    def _quoted(self, ref, sym):
        book = ref.books['ceres'][sym]
        return sum(o.remaining_qty for o in book.asks if o.agent_id == EXCHANGE_ID)

    def test_quiet_stock_quotes_min_depth(self):
        ref = sim.start_game(3)
        for _ in range(5):
            ref.step_round()
        for sym in ('EQ_AMOS', 'EQ_MARV', 'EQ_ZERO', 'EQ_AERL'):
            self.assertEqual(ref.exchange.depths[sym], MIN_DEPTH, sym)
            self.assertEqual(self._quoted(ref, sym), MIN_DEPTH, sym)

    def test_depth_grows_with_volume_is_bounded_and_decays(self):
        ref = sim.start_game(3)
        sym = 'EQ_ZERO'
        _grant(ref, 'amos', 500_000)
        peak = 0
        # Round-trip the stock through the exchange: buy its ask, sell back
        # into its bid, every round. Holdings stay put; volume piles up.
        for _ in range(3 * VOLUME_ROUNDS):
            _buy_all(ref, 'amos', sym)
            _sell_all(ref, 'amos', sym)
            ref.step_round()
            peak = max(peak, ref.exchange.depths[sym])
            self.assertLessEqual(ref.exchange.depths[sym], MAX_DEPTH)
        self.assertGreater(peak, 2 * MIN_DEPTH)
        self.assertEqual(ref.exchange.depths['EQ_MARV'], MIN_DEPTH)  # untouched stock stays quiet
        for _ in range(VOLUME_ROUNDS + 1):
            ref.step_round()
        self.assertEqual(ref.exchange.depths[sym], MIN_DEPTH)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)


class TestExchangeHoldingsCap(unittest.TestCase):
    def test_holdings_stay_at_or_below_cap_under_one_sided_selling(self):
        """Every holder, the issuer included, dumps one stock into the
        exchange's bid every round. Checked after every order and every round."""
        ref = sim.start_game(5)
        sym, issuer = 'EQ_AMOS', 'amos'
        worst = 0
        for _ in range(150):
            for agent in (issuer, 'zero', 'marvin', 'aerial'):
                _sell_all(ref, agent, sym)
                held = ref.get_balance(EXCHANGE_ID, sym)
                worst = max(worst, held)
                self.assertLessEqual(held, MAX_SHARES)
            ref.step_round()
            held = ref.get_balance(EXCHANGE_ID, sym)
            worst = max(worst, held)
            self.assertLessEqual(held, MAX_SHARES)
            # The bid never offers to buy past the cap.
            bids = sum(o.remaining_qty for o in ref.books['ceres'][sym].bids if o.agent_id == EXCHANGE_ID)
            self.assertLessEqual(held + bids, MAX_SHARES)
        self.assertEqual(worst, MAX_SHARES)  # the flow was heavy enough to reach the cap
        self.assertGreater(ref.get_balance(issuer, sym), 0)  # sellers were left holding, not the exchange
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_whale_cannot_reach_takeover_through_the_exchange(self):
        """A rival with 150k CR extra buys everything the exchange offers every
        round, while the other two rivals sell it everything they hold. Short
        of the issuer selling its own treasury, 500 is the most there is."""
        ref = sim.start_game(9)
        whale, sym, issuer = 'zero', 'EQ_AMOS', 'amos'
        others = ('marvin', 'aerial')
        _grant(ref, whale, 150_000)
        whale_max = x_max = 0
        for _ in range(200):
            for agent in others:
                _sell_all(ref, agent, sym)
                x_max = max(x_max, ref.get_balance(EXCHANGE_ID, sym))
            _buy_all(ref, whale, sym)
            whale_max = max(whale_max, ref.get_balance(whale, sym))
            x_max = max(x_max, ref.get_balance(EXCHANGE_ID, sym))
            self.assertLessEqual(x_max, MAX_SHARES)
            ref.step_round()
        self.assertLess(whale_max, TAKEOVER_AT)
        self.assertGreater(whale_max, 100)  # the whale did buy
        self.assertEqual(ref.get_balance(issuer, sym), 600)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)


class TestWhaleCheckTool(unittest.TestCase):
    def test_whale_in_styles_game_stays_below_takeover(self):
        from tools import whale_check
        r = whale_check.one(seed=1, rounds=60, variant='styles')
        self.assertLess(r['whale_max'], TAKEOVER_AT)
        self.assertLessEqual(r['exchange_max'], MAX_SHARES)
        self.assertIsNone(r['takeover_round'])
        self.assertIsNone(r['invariant_failure'])


if __name__ == '__main__':
    unittest.main()
