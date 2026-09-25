"""
tests/test_leaderboard_commodity_marks.py - FOOD, ORE, and FRAG count toward
net worth at their local station spot price (#196). Before this, mean-spot marks
rewarded buying at the cheapest station without hauling.
"""

import unittest

from agora.referee import AgoraReferee
from agora.spatial import STATIONS


class TestLeaderboardCommodityMarks(unittest.TestCase):
    def _give(self, ref, agent, inst, qty):
        # A fleet's goods live on its ship 1 ('<agent>/1') since #175.
        if inst != 'CR' and ref.fleet.is_corp(agent):
            agent = f"{agent}/1"
        with ref.conn:
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES ('SYSTEM', ?, 0)", (inst,))
            ref.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id='SYSTEM' AND instrument=?", (qty, inst))
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (agent, inst))
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id=? AND instrument=?", (qty, agent, inst))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('seed', 0, 'SYSTEM', ?, ?)", (inst, -qty))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('seed', 0, ?, ?, ?)", (agent, inst, qty))

    def _entry(self, ref, agent):
        return next(e for e in ref.get_leaderboard() if e['agent_id'] == agent)

    def test_starting_net_worth_local_spot(self):
        ref = AgoraReferee()
        # At Ceres (default home station), FRAG spot is round(11.2) = 11 under #243.
        # 10000 liquid CR + 1000 FRAG * 11 = 21000 CR.
        self.assertEqual(self._entry(ref, 'amos')['net_worth'], 21000)

    def test_ore_food_and_frag_are_marked_at_local_spot(self):
        ref = AgoraReferee()
        before = self._entry(ref, 'amos')['net_worth']
        self._give(ref, 'amos', 'ORE', 100)
        self._give(ref, 'amos', 'FOOD', 50)
        e = self._entry(ref, 'amos')
        ore_mark = round(ref.spatial.get_station_price('ceres', 'ORE'))
        food_mark = round(ref.spatial.get_station_price('ceres', 'FOOD'))
        frag_mark = round(ref.spatial.get_station_price('ceres', 'FRAG'))
        machinery_mark = round(ref.spatial.get_station_price('ceres', 'MACHINERY'))
        self.assertGreater(ore_mark, 0)
        self.assertGreater(food_mark, 0)
        self.assertGreater(frag_mark, 0)
        self.assertGreater(machinery_mark, 0)
        self.assertEqual(e['net_worth'], before + 100 * ore_mark + 50 * food_mark)
        self.assertEqual(e['commodity_marks'], {'FRAG': frag_mark, 'FOOD': food_mark, 'ORE': ore_mark, 'MACHINERY': machinery_mark})

    def test_spatial_variation_across_stations(self):
        ref = AgoraReferee(asymmetric=True)
        # Zero starts at Earth, Amos at Ceres
        zero = self._entry(ref, 'zero')
        amos = self._entry(ref, 'amos')
        self.assertEqual(zero['station_id'], 'earth')
        self.assertEqual(amos['station_id'], 'ceres')
        self.assertEqual(zero['commodity_marks']['FOOD'], round(ref.spatial.get_station_price('earth', 'FOOD')))
        self.assertEqual(amos['commodity_marks']['FOOD'], round(ref.spatial.get_station_price('ceres', 'FOOD')))
        self.assertLess(zero['commodity_marks']['FOOD'], amos['commodity_marks']['FOOD'])

    def test_anti_self_marking_buying_from_depot_does_not_inflate_net_worth(self):
        ref = AgoraReferee()
        ref.seed_depots()
        # Move amos to Earth (where FOOD is cheapest in the solar system)
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE vessel_locations SET station_id='earth' WHERE agent_id='amos'")
        initial_nw = self._entry(ref, 'amos')['net_worth']
        book = ref.books['earth']['FOOD']
        ask_before = book.best_ask()
        # Buy 500 units to clear the top ask level and move the depot's best ask
        env = {
            'kind': 'order',
            'payload': {
                'order_id': 'amos-test-buy',
                'agent_id': 'amos',
                'instrument': 'FOOD',
                'side': 'bid',
                'qty': 500,
                'limit_price': ask_before,
                'station_id': 'earth'
            }
        }
        res = ref.submit_envelope(env)
        self.assertEqual(res['kind'], 'market_tick')
        self.assertGreater(book.best_ask(), ask_before)
        after_nw = self._entry(ref, 'amos')['net_worth']
        # Buying at Earth depot must NOT inflate net worth even when moving depot ask: ask >= spot
        self.assertLessEqual(after_nw, initial_nw)


if __name__ == '__main__':
    unittest.main()
