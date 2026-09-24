"""
tests/test_leaderboard_in_transit_cargo.py - Verify that in-transit cargo is
included in leaderboard net worth and holdings, net of projected perishable
decay (#195).
"""

import unittest
from agora.referee import AgoraReferee
from agora.spatial import STATIONS


class TestLeaderboardInTransitCargo(unittest.TestCase):
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

    def test_in_transit_ore_included_in_net_worth(self):
        ref = AgoraReferee()
        agent = 'amos'
        initial_nw = self._entry(ref, agent)['net_worth']

        # Give amos 200 ORE and fuel to move
        self._give(ref, agent, 'ORE', 200)
        self._give(ref, agent, 'FUEL', 100)
        ore_mark = round(ref.spatial.get_station_price('ceres', 'ORE'))
        nw_with_ore = self._entry(ref, agent)['net_worth']
        self.assertEqual(nw_with_ore, initial_nw + 200 * ore_mark)

        # Move to earth with 200 ORE
        res = ref.initiate_transit(agent, 'earth', commodity='ORE', cargo_qty=200)
        self.assertEqual(res['status'], 'in_transit')
        toll = res['payload']['toll_paid']

        # Leaderboard should reflect in-transit cargo in net_worth and in_transit_cargo,
        # but keep docked holdings (ore) as docked accounts only (#202 review).
        entry = self._entry(ref, agent)
        self.assertEqual(entry['ore'], 0)
        self.assertEqual(entry['in_transit_cargo'].get('ORE'), 200)
        # Net worth reflects held cargo, minus the toll paid in liquid cash
        self.assertEqual(entry['net_worth'], nw_with_ore - toll)

    def test_in_transit_perishable_food_decay(self):
        ref = AgoraReferee()
        agent = 'aerial'
        # Start at ceres, move to earth (belt route has BELT_CARGO_DECAY_RATE)
        self._give(ref, agent, 'FOOD', 100)
        self._give(ref, agent, 'FUEL', 100)
        food_mark = round(ref.spatial.get_station_price('ceres', 'FOOD'))
        initial_nw = self._entry(ref, agent)['net_worth']

        res = ref.initiate_transit(agent, 'earth', commodity='FOOD', cargo_qty=100, perishable=True)
        self.assertEqual(res['status'], 'in_transit')
        toll = res['payload']['toll_paid']

        # Round 0: 0 decay elapsed
        entry0 = self._entry(ref, agent)
        self.assertEqual(entry0['food'], 0)
        self.assertEqual(entry0['in_transit_cargo'].get('FOOD'), 100)
        self.assertEqual(entry0['net_worth'], initial_nw - toll)

        # Step 1 round forward
        ref.step_round()
        entry1 = self._entry(ref, agent)
        # Expected decay: 100 * 0.05 * 1 = 5 food decayed -> 95 food remaining
        expected_remaining = 95
        self.assertEqual(entry1['food'], 0)
        expected_nw = entry1['liquid'] + entry1['frags'] * entry1['commodity_marks']['FRAG'] + expected_remaining * food_mark
        self.assertEqual(entry1['net_worth'], expected_nw)

    def test_arrival_settlement_no_double_count(self):
        ref = AgoraReferee()
        agent = 'amos'
        self._give(ref, agent, 'ORE', 100)
        self._give(ref, agent, 'FUEL', 100)

        res = ref.initiate_transit(agent, 'earth', commodity='ORE', cargo_qty=100)
        self.assertEqual(res['status'], 'in_transit')
        arr_round = res['payload']['arrival_round']

        # Advance rounds until arrived
        while ref.current_round < arr_round:
            ref.step_round()

        # Transit has arrived, cargo returned to docked account
        loc = ref.get_vessel_location(agent)
        self.assertEqual(loc['status'], 'docked')
        entry = self._entry(ref, agent)
        self.assertEqual(entry['ore'], 100)
        self.assertEqual(entry['in_transit_cargo'].get('ORE', 0), 0)

    def test_departure_net_worth_invariance_apart_from_toll_and_hazard(self):
        """
        #195: A fleet's net worth should barely change on the round it departs,
        apart from fuel, the toll and any hazard loss rolled at departure.
        transits.cargo_qty is already net of hazard loss at departure, so valuing
        transits.cargo_qty without double-subtracting hazard loss preserves the invariance.
        """
        ref = AgoraReferee()
        agent = 'amos'
        with ref.conn:
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES ('amos', 'CR', 5000)")
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES ('amos', 'FUEL', 100)")
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES ('amos', 'ORE', 300)")

        ore_mark = round(ref.spatial.get_station_price('ceres', 'ORE'))
        lb_before = next(e for e in ref.get_leaderboard() if e['agent_id'] == agent)
        nw_before = lb_before['net_worth']

        res = ref.initiate_transit(agent, 'earth', commodity='ORE', cargo_qty=300)
        toll = res['payload']['toll_paid']

        cur = ref.conn.cursor()
        tx = cur.execute('SELECT cargo_qty FROM transits WHERE agent_id=? AND status="in_transit"', (agent,)).fetchone()
        c_qty_in_transit = tx['cargo_qty']
        hz_lost = 300 - c_qty_in_transit

        lb_after = next(e for e in ref.get_leaderboard() if e['agent_id'] == agent)
        nw_after = lb_after['net_worth']

        expected_nw = nw_before - toll - hz_lost * ore_mark
        self.assertEqual(nw_after, expected_nw)


if __name__ == '__main__':
    unittest.main()
