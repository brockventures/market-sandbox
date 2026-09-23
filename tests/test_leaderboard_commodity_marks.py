"""
tests/test_leaderboard_commodity_marks.py - FOOD and ORE count toward net
worth at their mean station spot price. Before this, a fleet that bought
ORE to haul showed a pure loss on the leaderboard.
"""

import unittest

from agora.referee import AgoraReferee
from agora.spatial import STATIONS


class TestLeaderboardCommodityMarks(unittest.TestCase):
    def _give(self, ref, agent, inst, qty):
        with ref.conn:
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES ('SYSTEM', ?, 0)", (inst,))
            ref.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id='SYSTEM' AND instrument=?", (qty, inst))
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (agent, inst))
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id=? AND instrument=?", (qty, agent, inst))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('seed', 0, 'SYSTEM', ?, ?)", (inst, -qty))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('seed', 0, ?, ?, ?)", (agent, inst, qty))

    def _entry(self, ref, agent):
        return next(e for e in ref.get_leaderboard() if e['agent_id'] == agent)

    def test_starting_net_worth_unchanged(self):
        ref = AgoraReferee()
        self.assertEqual(self._entry(ref, 'amos')['net_worth'], 20000)

    def test_ore_and_food_are_marked(self):
        ref = AgoraReferee()
        before = self._entry(ref, 'amos')['net_worth']
        self._give(ref, 'amos', 'ORE', 100)
        self._give(ref, 'amos', 'FOOD', 50)
        e = self._entry(ref, 'amos')
        ore_mark = round(sum(ref.spatial.get_station_price(s, 'ORE') for s in STATIONS) / len(STATIONS))
        food_mark = round(sum(ref.spatial.get_station_price(s, 'FOOD') for s in STATIONS) / len(STATIONS))
        self.assertGreater(ore_mark, 0)
        self.assertEqual(e['net_worth'], before + 100 * ore_mark + 50 * food_mark)
        self.assertEqual(e['commodity_marks'], {'FOOD': food_mark, 'ORE': ore_mark})


if __name__ == '__main__':
    unittest.main()
