"""Fleet stocks as a live market: cross-holdings, leaderboard value, trade from anywhere, no fog."""
import unittest

from agora.referee import AgoraReferee, STOCK_EXCHANGE_STATION


def order(ref, oid, agent, side, qty, px, sym, st=None):
    p = {'order_id': oid, 'agent_id': agent, 'side': side, 'qty': qty, 'limit_price': px,
         'instrument': sym, 'seq_seen': ref.current_seq}
    if st:
        p['station_id'] = st
    return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': p})


class TestStocks(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(depots=True, rival_shares=100)

    def test_genesis_cross_holdings(self):
        self.assertEqual(self.ref.get_balance('amos', 'EQ_AMOS'), 700)
        for f in ('marvin', 'zero', 'aerial'):
            self.assertEqual(self.ref.get_balance(f, 'EQ_AMOS'), 100)
        ok, errs = self.ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_trade_from_any_station_and_counts_on_leaderboard(self):
        ref = self.ref
        before = {e['agent_id']: e for e in ref.get_leaderboard()}
        self.assertEqual(order(ref, 's1', 'zero', 'ask', 10, 40, 'EQ_AMOS', st='earth')['kind'], 'market_tick')
        self.assertEqual(order(ref, 'b1', 'marvin', 'bid', 10, 40, 'EQ_AMOS')['kind'], 'market_tick')
        self.assertEqual(ref.get_balance('marvin', 'EQ_AMOS'), 110)
        marks = ref.stock_marks({})
        self.assertEqual(marks['EQ_AMOS']['mark'], 40.0)
        after = {e['agent_id']: e for e in ref.get_leaderboard()}
        self.assertEqual(after['marvin']['stocks']['EQ_AMOS'], 110)
        self.assertNotIn('EQ_AMOS', after['amos']['stocks'])  # own shares never count
        self.assertGreater(after['marvin']['stocks_value'], before['marvin']['stocks_value'])
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_off_keeps_old_genesis(self):
        ref = AgoraReferee()
        self.assertEqual(ref.get_balance('amos', 'EQ_AMOS'), 1000)
        self.assertEqual(STOCK_EXCHANGE_STATION, 'ceres')

    def test_stock_ticks_not_fogged(self):
        ref = AgoraReferee(depots=True, rival_shares=100)
        ref.new_game(seed=3, fog=True, rival_shares=100)
        order(ref, 's2', 'zero', 'ask', 5, 30, 'EQ_MARV')
        order(ref, 'b2', 'aerial', 'bid', 5, 30, 'EQ_MARV')
        seen = ref.fog.filter_ticks(ref, None, ref.get_ticks())
        self.assertTrue(any(t['kind'] == 'trade' and t['payload'].get('instrument') == 'EQ_MARV' for t in seen))


if __name__ == '__main__':
    unittest.main()
