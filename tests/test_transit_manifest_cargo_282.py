import unittest
import math
from agora.referee import AgoraReferee
from agora.spatial import get_route
from agora.piracy import cargo_value as piracy_cargo_value

def give(ref, acct, inst, qty):
    with ref.lock, ref.conn:
        ref.fleet._move(f"test-give-{acct}-{inst}-{ref.current_seq}", ((acct, inst, qty), ('SYSTEM', inst, -qty)))

def place_test_order(ref, agent, side, qty, px, inst, st, vessel=None, oid=None):
    p = {
        'order_id': oid or f"t-{agent}-{side}-{inst}-{ref.current_seq}",
        'agent_id': agent,
        'side': side,
        'qty': qty,
        'limit_price': px,
        'instrument': inst,
        'station_id': st,
        'seq_seen': ref.current_seq
    }
    if vessel is not None:
        p['vessel_id'] = vessel
    return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': p})

class TestTransitManifestCargo282(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(piracy='0.15,0.04')

    def test_resting_ask_move_succeeds_and_cancels_orders(self):
        """Issue #282: A ship with a resting SELL must be able to move with cargo_qty omitted."""
        ref = self.ref
        agent = 'amos'
        vessel = 'amos/1'
        loc = ref.get_vessel_location(agent)
        st = loc['station_id']
        dest = 'ceres' if st != 'ceres' else 'earth'

        give(ref, vessel, 'FUEL', 300)

        # Place a resting ask on 50 FRAG (amos/1 starts with 1000 FRAG)
        res_order = place_test_order(ref, agent, 'ask', 50, 9999, 'FRAG', st, vessel=vessel)
        self.assertEqual(res_order.get('kind'), 'market_tick')
        self.assertEqual(res_order['payload'].get('status'), 'open')
        self.assertEqual(ref.committed(agent, 'FRAG', vessel), 50)

        # Move with cargo_qty omitted (None / 0)
        res = ref.initiate_transit(agent_id=agent, destination=dest, vessel_id=vessel)
        self.assertEqual(res['status'], 'in_transit')
        payload = res['payload']

        # Hold cargo and whole-hold valuation must be reported
        self.assertIn('hold_cargo', payload)
        self.assertIn('total_cargo_value', payload)
        self.assertGreater(payload['total_cargo_value'], 0)

        # Piracy odds must be non-zero (evaluated against the 1000 FRAG hold)
        piracy = payload.get('piracy')
        self.assertIsNotNone(piracy)
        self.assertGreater(piracy['odds'], 0.0)

        # Resting ask must be cancelled on departure
        resting = {o.order_id for b in ref.books[st].values() for o in b.asks if o.agent_id == agent}
        self.assertEqual(len(resting), 0)

    def test_mixed_hold_whole_valuation(self):
        """Mixed holds (e.g. FOOD + ORE) must have their entire value evaluated for piracy risk."""
        ref = self.ref
        agent = 'marvin'
        vessel = 'marvin/1'
        loc = ref.get_vessel_location(agent)
        st = loc['station_id']
        dest = 'ceres' if st != 'ceres' else 'earth'

        # Seed 100 FOOD and 150 ORE aboard via balanced give()
        give(ref, vessel, 'FOOD', 100)
        give(ref, vessel, 'ORE', 150)
        give(ref, vessel, 'FUEL', 300)

        # Manifest only 150 ORE
        res = ref.initiate_transit(agent_id=agent, destination=dest, commodity='ORE', cargo_qty=150, vessel_id=vessel)
        self.assertEqual(res['status'], 'in_transit')
        payload = res['payload']

        # Manifest carries 150 ORE
        self.assertEqual(payload['commodity'], 'ORE')
        self.assertEqual(payload['cargo_qty'], 150)

        # Total cargo value must reflect 150 ORE + 100 FOOD + genesis 1000 FRAG
        expected_value = piracy_cargo_value('ORE', 150) + piracy_cargo_value('FOOD', 100) + piracy_cargo_value('FRAG', 1000)
        self.assertEqual(payload['total_cargo_value'], expected_value)

        # 100 FOOD must still be in the vessel hold
        self.assertEqual(ref._account_balance(vessel, 'FOOD'), 100)

    def test_partial_quantity_does_not_evade_piracy(self):
        """Specifying cargo_qty=1 when holding 250 must not evade raid odds."""
        ref = self.ref
        agent = 'zero'
        vessel = 'zero/1'
        loc = ref.get_vessel_location(agent)
        st = loc['station_id']
        dest = 'ceres' if st != 'ceres' else 'earth'

        give(ref, vessel, 'FOOD', 250)
        give(ref, vessel, 'FUEL', 300)

        res = ref.initiate_transit(agent_id=agent, destination=dest, commodity='FOOD', cargo_qty=1, vessel_id=vessel)
        self.assertEqual(res['status'], 'in_transit')
        payload = res['payload']

        # Whole-hold value must reflect all 250 FOOD plus genesis 1000 FRAG
        expected_value = piracy_cargo_value('FOOD', 250) + piracy_cargo_value('FRAG', 1000)
        self.assertEqual(payload['total_cargo_value'], expected_value)

        # Piracy odds calculated on total value
        piracy = payload['piracy']
        self.assertGreater(piracy['odds'], 0.0)

    def test_empty_ship_zero_risk(self):
        """An empty ship with 0 goods aboard must have 0.0 piracy raid odds."""
        ref = self.ref
        agent = 'aerial'
        vessel = 'aerial/1'
        loc = ref.get_vessel_location(agent)
        st = loc['station_id']
        dest = 'ceres' if st != 'ceres' else 'earth'

        # Transfer out all goods from aerial/1 to SYSTEM
        for g in ['FRAG', 'ORE', 'FOOD', 'MACHINERY', 'EQUIPMENT']:
            bal = ref._account_balance(vessel, g)
            if bal > 0:
                give(ref, vessel, g, -bal)
        give(ref, vessel, 'FUEL', 300)

        res = ref.initiate_transit(agent_id=agent, destination=dest, vessel_id=vessel)
        self.assertEqual(res['status'], 'in_transit')
        payload = res['payload']
        self.assertEqual(payload['total_cargo_value'], 0)
        self.assertEqual(payload['piracy']['odds'], 0.0)

    def test_perishable_decay_on_unmanifested_hold_goods(self):
        """Unmanifested perishable goods (FOOD) in hold decay across flight rounds."""
        ref = self.ref
        agent = 'amos'
        vessel = 'amos/1'
        # Force location to earth and destination to ceres (3 rounds, decay_rate > 0)
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE vessels SET station_id = 'earth', status = 'docked' WHERE vessel_id = ?", (vessel,))

        give(ref, vessel, 'FOOD', 200)
        give(ref, vessel, 'FUEL', 300)

        route = get_route('earth', 'ceres', ref.current_round)
        decay_rate = route.get('decay_rate', 0.0)
        self.assertGreater(decay_rate, 0.0)

        # Initiate move without specifying cargo
        res = ref.initiate_transit(agent_id=agent, destination='ceres', vessel_id=vessel)
        self.assertEqual(res['status'], 'in_transit')
        arr_round = res['payload']['arrival_round']
        dep_round = res['payload']['departure_round']
        duration = arr_round - dep_round

        # Step rounds until arrival (accounting for any hazard storm delays)
        while ref.get_vessel_location(agent)['status'] == 'in_transit':
            ref.step_round()

        # Verify vessel docked at ceres
        self.assertEqual(ref.get_vessel_location(agent)['station_id'], 'ceres')

        # Expected decay: 200 - floor(200 * decay_rate * transit_rounds)
        t_row = ref.conn.execute("SELECT arrival_round, departure_round FROM transits WHERE transit_id = ?", (res['payload']['transit_id'],)).fetchone()
        actual_rounds = t_row[0] - t_row[1]
        expected_decay = math.floor(200 * decay_rate * actual_rounds)
        expected_balance = 200 - expected_decay
        actual_balance = ref._account_balance(vessel, 'FOOD')
        self.assertEqual(actual_balance, expected_balance)

        # Verify double-entry ledger invariants
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, f"Ledger invariant errors: {errs}")

    def test_mixed_hold_piracy_surrender_settles_and_ledger_balances(self):
        """Marvin's review finding: Surrender on a mixed hold with unmanifested goods
        must take goods from the hold and keep the ledger balanced."""
        ref = self.ref
        agent = 'marvin'
        vessel = 'marvin/1'
        loc = ref.get_vessel_location(agent)
        st = loc['station_id']
        dest = 'ceres' if st != 'ceres' else 'earth'

        give(ref, vessel, 'FOOD', 100)
        give(ref, vessel, 'ORE', 150)
        give(ref, vessel, 'FUEL', 300)

        # Force piracy odds to 100% raid chance
        ref.piracy.odds = (1.0, 1.0)

        res = ref.initiate_transit(agent_id=agent, destination=dest, vessel_id=vessel)
        self.assertEqual(res['status'], 'in_transit')
        payload = res['payload']
        tid = payload['transit_id']

        # Piracy raid must be triggered
        self.assertTrue(payload.get('piracy', {}).get('raided', False))

        # Check pending raid
        raid_row = ref.conn.execute("SELECT * FROM piracy_raids WHERE transit_id = ?", (tid,)).fetchone()
        self.assertIsNotNone(raid_row)
        surrender_qty = raid_row['surrender_qty']
        self.assertGreater(surrender_qty, 0)

        # Respond with surrender
        resp = ref.piracy.respond(agent, tid, 'surrender')
        self.assertEqual(resp.get('kind'), 'piracy_respond_ok')
        self.assertEqual(resp['payload']['status'], 'surrendered')
        self.assertEqual(resp['payload']['qty_taken'], surrender_qty)

        # Verify hold balances decreased
        food_bal = ref._account_balance(vessel, 'FOOD')
        ore_bal = ref._account_balance(vessel, 'ORE')
        frag_bal = ref._account_balance(vessel, 'FRAG')
        total_remaining = food_bal + ore_bal + frag_bal
        # Genesis FRAG was 1000, FOOD 100, ORE 150 = 1250 total
        self.assertEqual(total_remaining, 1250 - surrender_qty)

        # Verify ledger invariants
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, f"Ledger invariant error after surrender: {errs}")

    def test_unmanifested_hold_hazard_hull_breach_drops_hold_and_ledger_balances(self):
        """Amos's review finding: 100 FRAG + 150 ORE aboard, move with qty omitted
        and forced hull breach: hold balances drop by lost_qty, ledger balances."""
        # Instantiate referee with 100% loss odds on hazards
        ref = AgoraReferee(hazards='0.0,1.0')
        ref.new_game(seed=42, warmup_rounds=2, hazards='0.0,1.0')
        agent = 'amos'
        vessel = 'amos/1'

        # Set hold to exact 100 FRAG and 150 ORE
        frag_bal = ref._account_balance(vessel, 'FRAG')
        give(ref, vessel, 'FRAG', 100 - frag_bal)
        give(ref, vessel, 'ORE', 150)
        give(ref, vessel, 'FUEL', 300)

        self.assertEqual(ref._account_balance(vessel, 'FRAG'), 100)
        self.assertEqual(ref._account_balance(vessel, 'ORE'), 150)

        loc = ref.get_vessel_location(agent)
        dest = 'ceres' if loc['station_id'] != 'ceres' else 'earth'

        res = ref.initiate_transit(agent_id=agent, destination=dest, vessel_id=vessel)
        self.assertEqual(res['status'], 'in_transit')
        payload = res['payload']
        hazard = payload.get('hazard')
        self.assertIsNotNone(hazard)
        lost_qty = hazard.get('lost_qty', 0)
        self.assertGreater(lost_qty, 0)

        # Check total remaining aboard
        rem_frag = ref._account_balance(vessel, 'FRAG')
        rem_ore = ref._account_balance(vessel, 'ORE')
        self.assertEqual((rem_frag + rem_ore), (100 + 150 - lost_qty))

        # Invariants must hold
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, f"Ledger invariant error after hazard: {errs}")

if __name__ == '__main__':
    unittest.main()
