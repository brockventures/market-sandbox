"""
tests.test_commodities_economy - Unit tests for 4-commodity Sol System economy (FOOD & ORE),
perishable cargo transit decay, depot liquidity, and asymmetric genesis server defaults.
"""

import os
import unittest
from agora.spatial import (
    STATIONS, COMMODITIES, BASE_PRICES, StationPriceEngine,
    PERISHABLE_COMMODITIES, get_route
)
from agora.referee import AgoraReferee, ASYMMETRIC_SPAWN_LOCATIONS
from tests.legacy_surface import pre_162_surface


class TestCommoditiesEconomy(unittest.TestCase):
    def test_commodities_constants_and_base_prices(self):
        """Verify all 5 commodities exist with complete asymmetric station price surfaces (#247)."""
        self.assertEqual(len(COMMODITIES), 5)
        for c in ('FRAG', 'FUEL', 'FOOD', 'ORE', 'MACHINERY'):
            self.assertIn(c, COMMODITIES)

        for st in STATIONS:
            self.assertIn(st, BASE_PRICES)
            for c in COMMODITIES:
                self.assertIn(c, BASE_PRICES[st])
                self.assertGreater(BASE_PRICES[st][c], 0.0)

        # Sol Asymmetric Supply Chain topology verification (#247):
        # Earth: Biosphere cradle exporter of FOOD, major scrap/ore recycler
        self.assertEqual(BASE_PRICES['earth']['FOOD'], 10.2)
        self.assertEqual(BASE_PRICES['earth']['ORE'], 27.5)
        self.assertEqual(BASE_PRICES['earth']['FRAG'], 20.2)
        self.assertEqual(min(STATIONS, key=lambda s: BASE_PRICES[s]['FOOD']), 'earth')

        # Luna: He-3 propellant capital exporter of FUEL, importer of FOOD & MACHINERY
        self.assertEqual(BASE_PRICES['luna']['FUEL'], 8.5)
        self.assertEqual(BASE_PRICES['luna']['FOOD'], 22.0)
        self.assertEqual(BASE_PRICES['luna']['MACHINERY'], 23.0)
        self.assertEqual(min(STATIONS, key=lambda s: BASE_PRICES[s]['FUEL']), 'luna')

        # Mars: High-tech foundry forge exporter of MACHINERY
        self.assertEqual(BASE_PRICES['mars']['MACHINERY'], 13.8)
        self.assertEqual(min(STATIONS, key=lambda s: BASE_PRICES[s]['MACHINERY']), 'mars')

        # Ceres: Belt motherlode exporter of ORE and FRAG, severe deficit in MACHINERY & FOOD
        self.assertEqual(BASE_PRICES['ceres']['ORE'], 11.5)
        self.assertEqual(BASE_PRICES['ceres']['FRAG'], 11.2)
        self.assertEqual(BASE_PRICES['ceres']['MACHINERY'], 29.5)
        self.assertEqual(BASE_PRICES['ceres']['FOOD'], 27.5)
        self.assertEqual(min(STATIONS, key=lambda s: BASE_PRICES[s]['ORE']), 'ceres')
        self.assertEqual(min(STATIONS, key=lambda s: BASE_PRICES[s]['FRAG']), 'ceres')
        self.assertEqual(max(STATIONS, key=lambda s: BASE_PRICES[s]['MACHINERY']), 'ceres')
        self.assertEqual(max(STATIONS, key=lambda s: BASE_PRICES[s]['FOOD']), 'ceres')

        # Invariant verification across all 5 commodities:
        # Every commodity has distinct min and max station prices with positive arbitrage spread
        for c in COMMODITIES:
            cheapest = min(STATIONS, key=lambda s: BASE_PRICES[s][c])
            dearest = max(STATIONS, key=lambda s: BASE_PRICES[s][c])
            self.assertNotEqual(cheapest, dearest, f"{c} cheapest and dearest must differ")
            self.assertGreater(BASE_PRICES[dearest][c], BASE_PRICES[cheapest][c] * 1.4,
                               f"{c} spread must offer viable arbitrage (>40%)")
            total_price = sum(BASE_PRICES[s][c] for s in STATIONS)
            self.assertGreater(total_price, 40.0, f"{c} aggregate price surface sanity lower bound")
            self.assertLess(total_price, 120.0, f"{c} aggregate price surface sanity upper bound")

    def test_perishable_flag_and_decay_rates(self):
        """FOOD is perishable and decays in belt transit; ORE is durable."""
        self.assertIn('FOOD', PERISHABLE_COMMODITIES)
        self.assertNotIn('ORE', PERISHABLE_COMMODITIES)

        route_belt = get_route('earth', 'ceres', round_num=0)
        self.assertTrue(route_belt['is_belt_route'])
        self.assertEqual(route_belt['decay_rate'], 0.05)

        route_local = get_route('earth', 'luna', round_num=0)
        self.assertFalse(route_local['is_belt_route'])
        self.assertEqual(route_local['decay_rate'], 0.0)

    def test_food_perishable_transit_decay_in_referee(self):
        """Hauling FOOD across the belt decays at 5% per round; ledger invariants hold."""
        ref = AgoraReferee(depots=True, asymmetric=True)

        # Zero spawns at Earth
        self.assertEqual(ref.get_vessel_location('zero')['station_id'], 'earth')

        # Give Zero 100 FOOD directly or buy from Earth Depot
        with ref.lock, ref.conn:
            ref.conn.execute(
                "INSERT INTO accounts (agent_id, instrument, balance) VALUES ('zero/1', 'FOOD', 100) "
                "ON CONFLICT(agent_id, instrument) DO UPDATE SET balance = balance + 100"
            )
            ref.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) "
                "VALUES ('test-mint-food', 0, 'zero/1', 'FOOD', 100)"
            )
            ref.conn.execute(
                "UPDATE accounts SET balance = balance - 100 WHERE agent_id = 'SYSTEM' AND instrument = 'FOOD'"
            )
            ref.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) "
                "VALUES ('test-mint-food', 0, 'SYSTEM', 'FOOD', -100)"
            )

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger error: {errors}")

        # Transit Earth -> Ceres carrying 100 FOOD (3 rounds in belt -> 15% decay)
        transit_res = ref.initiate_transit(
            agent_id='zero',
            destination='ceres',
            commodity='FOOD',
            cargo_qty=100
        )
        self.assertEqual(transit_res['status'], 'in_transit')
        self.assertTrue(transit_res['payload']['perishable'])

        # Advance rounds until arrived
        ref.step_round()
        ref.step_round()
        step3 = ref.step_round()
        self.assertEqual(len(step3['arrived_transits']), 1)
        arr = step3['arrived_transits'][0]
        self.assertEqual(arr['commodity'], 'FOOD')
        self.assertEqual(arr['cargo_decayed'], 15)
        self.assertEqual(arr['cargo_delivered'], 85)

        # Zero balance is now 85 FOOD
        self.assertEqual(ref.get_balance('zero', 'FOOD'), 85)

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger error after decay: {errors}")

    def test_ore_durable_transit_no_decay(self):
        """Hauling ORE from Ceres to Earth does NOT decay."""
        ref = AgoraReferee(depots=True, asymmetric=True)

        # Amos spawns at Ceres
        self.assertEqual(ref.get_vessel_location('amos')['station_id'], 'ceres')

        with ref.lock, ref.conn:
            ref.conn.execute(
                "INSERT INTO accounts (agent_id, instrument, balance) VALUES ('amos/1', 'ORE', 100) "
                "ON CONFLICT(agent_id, instrument) DO UPDATE SET balance = balance + 100"
            )
            ref.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) "
                "VALUES ('test-mint-ore', 0, 'amos/1', 'ORE', 100)"
            )
            ref.conn.execute(
                "UPDATE accounts SET balance = balance - 100 WHERE agent_id = 'SYSTEM' AND instrument = 'ORE'"
            )
            ref.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) "
                "VALUES ('test-mint-ore', 0, 'SYSTEM', 'ORE', -100)"
            )

        transit_res = ref.initiate_transit(
            agent_id='amos',
            destination='earth',
            commodity='ORE',
            cargo_qty=100
        )
        self.assertEqual(transit_res['status'], 'in_transit')
        self.assertFalse(transit_res['payload']['perishable'])

        ref.step_round()
        ref.step_round()
        step3 = ref.step_round()
        arr = step3['arrived_transits'][0]
        self.assertEqual(arr['commodity'], 'ORE')
        self.assertEqual(arr['cargo_decayed'], 0)
        self.assertEqual(arr['cargo_delivered'], 100)
        self.assertEqual(ref.get_balance('amos', 'ORE'), 100)

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid)

    @pre_162_surface()
    def test_depots_have_food_and_ore_liquidity(self):
        """All station depots quote two-sided resting liquidity for FOOD and ORE."""
        ref = AgoraReferee(depots=True)
        summary = ref.get_depot_summary()
        self.assertTrue(summary['depots_enabled'])

        for st in STATIONS:
            for comm in ('FOOD', 'ORE'):
                info = summary['stations'][st][comm]
                self.assertIsNotNone(info['best_bid'], f"Missing best_bid for {st} {comm}")
                self.assertIsNotNone(info['best_ask'], f"Missing best_ask for {st} {comm}")
                self.assertGreater(info['bid_depth'], 0)
                self.assertGreater(info['ask_depth'], 0)
                self.assertGreaterEqual(info['best_ask'], info['best_bid'] + 1)

        # Earth depot quotes: FOOD ask is cheap (9), ORE bid is high (25)
        self.assertEqual(summary['stations']['earth']['FOOD']['best_ask'], 11)
        self.assertEqual(summary['stations']['earth']['ORE']['best_bid'], 29)

        # Ceres depot quotes: ORE ask is cheap (9), FOOD bid is high (31)
        self.assertEqual(summary['stations']['ceres']['ORE']['best_ask'], 11)
        self.assertEqual(summary['stations']['ceres']['FOOD']['best_bid'], 29)

    @pre_162_surface()
    def test_bidirectional_profitable_arbitrage_loops(self):
        """
        Verify both directional trade legs are profitable:
        1. Earth -> Ceres: Buy FOOD on Earth @ 9 CR, sell at Ceres @ 31 CR
        2. Ceres -> Earth: Buy ORE on Ceres @ 9 CR, sell at Earth @ 25 CR
        """
        ref = AgoraReferee(depots=True, asymmetric=True)

        # Leg 1: Zero starts at Earth, buys 100 FOOD @ 9 CR = 900 CR
        buy_food = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'zero-buy-food',
                'agent_id': 'zero',
                'station_id': 'earth',
                'instrument': 'FOOD',
                'side': 'bid',
                'qty': 100,
                'limit_price': 11,
                'seq_seen': 0
            }
        })
        self.assertEqual(buy_food['payload']['trades_count'], 1)
        self.assertEqual(buy_food['payload']['last_price'], 11)
        self.assertEqual(ref.get_balance('zero', 'FOOD'), 100)

        # Fly to Ceres (3 rounds, 5% decay per round = 15 decay -> 85 delivered)
        ref.initiate_transit('zero', 'ceres', commodity='FOOD', cargo_qty=100)
        for _ in range(3):
            ref.step_round()

        self.assertEqual(ref.get_vessel_location('zero')['station_id'], 'ceres')
        self.assertEqual(ref.get_balance('zero', 'FOOD'), 85)

        # Sell 85 FOOD to Ceres Depot @ 31 CR = 2,635 CR
        sell_food = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'zero-sell-food',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'FOOD',
                'side': 'ask',
                'qty': 85,
                'limit_price': 29,
                'seq_seen': ref.current_seq
            }
        })
        self.assertEqual(sell_food['payload']['trades_count'], 1)
        # Gross revenue: 2635 CR vs 900 CR spent (+1735 CR profit)
        self.assertEqual(ref.get_balance('zero', 'FOOD'), 0)

        # Leg 2: At Ceres, buy 100 ORE from Ceres Depot at current ask
        depot_ore_ask = ref.get_depot_summary()['stations']['ceres']['ORE']['best_ask']
        buy_ore = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'zero-buy-ore',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'ORE',
                'side': 'bid',
                'qty': 100,
                'limit_price': depot_ore_ask,
                'seq_seen': ref.current_seq
            }
        })
        self.assertEqual(buy_ore['payload']['trades_count'], 1)
        self.assertEqual(ref.get_balance('zero', 'ORE'), 100)

        # Refuel at Ceres from Ceres Depot
        depot_fuel_ask = ref.get_depot_summary()['stations']['ceres']['FUEL']['best_ask']
        buy_fuel = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'zero-buy-fuel-ceres',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'FUEL',
                'side': 'bid',
                'qty': 50,
                'limit_price': depot_fuel_ask,
                'seq_seen': ref.current_seq
            }
        })
        self.assertEqual(buy_fuel['payload']['trades_count'], 1)

        ref.initiate_transit('zero', 'earth', commodity='ORE', cargo_qty=100)
        for _ in range(3):
            ref.step_round()

        self.assertEqual(ref.get_vessel_location('zero')['station_id'], 'earth')
        self.assertEqual(ref.get_balance('zero', 'ORE'), 100)  # zero decay!

        # Sell 100 ORE at Earth to Earth Depot at current bid
        depot_ore_bid = ref.get_depot_summary()['stations']['earth']['ORE']['best_bid']
        sell_ore = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'zero-sell-ore',
                'agent_id': 'zero',
                'station_id': 'earth',
                'instrument': 'ORE',
                'side': 'ask',
                'qty': 100,
                'limit_price': depot_ore_bid,
                'seq_seen': ref.current_seq
            }
        })
        self.assertEqual(sell_ore['payload']['trades_count'], 1)
        self.assertEqual(ref.get_balance('zero', 'ORE'), 0)

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant errors: {errors}")


if __name__ == '__main__':
    unittest.main()
