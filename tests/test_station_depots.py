"""
tests/test_station_depots.py - Unit & integration tests for Station Depot AMM Liquidity Pools.
Tests Issue #71:
- Automated continuous two-sided liquidity quoting around BASE_PRICES
- Earth Depot sells FRAG @ ~10-11 CR and buys FUEL @ ~8-9 CR
- Ceres Depot buys FRAG @ ~21-22 CR and sells FUEL @ ~25-26 CR
- Multi-station spatial arbitrage execution & double-entry ledger invariants
- Round step quote updates with spot price drift
- HTTP endpoints: GET /referee/depots, POST /referee/admin/depots/refresh, admin reset/new_game
"""

import json
import threading
import unittest
from http.server import HTTPServer
import urllib.request

from agora.referee import AgoraReferee
from agora.server import make_handler
from agora.spatial import STATIONS, COMMODITIES, BASE_PRICES
from tests.legacy_surface import pre_162_surface


class TestStationDepots(unittest.TestCase):
    def test_depots_disabled_by_default(self):
        """Default AgoraReferee preserves clean empty books for backward compatibility."""
        ref = AgoraReferee()
        self.assertFalse(ref.depots_enabled)
        for st in STATIONS:
            for comm in COMMODITIES:
                book = ref.books[st][comm]
                self.assertEqual(len(book.bids), 0)
                self.assertEqual(len(book.asks), 0)

    def test_depot_genesis_invariants(self):
        """AgoraReferee(depots=True) initializes depots with valid ledger conservation."""
        ref = AgoraReferee(depots=True)
        self.assertTrue(ref.depots_enabled)

        # 1. Verify double-entry ledger invariants
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant errors: {errors}")
        self.assertEqual(len(errors), 0)

        # 2. Check each depot account balances
        for st in STATIONS:
            depot_id = f"depot_{st}"
            self.assertEqual(ref.get_balance(depot_id, 'CR'), 1000000)
            self.assertEqual(ref.get_balance(depot_id, 'FRAG'), 100000)
            self.assertEqual(ref.get_balance(depot_id, 'FUEL'), 100000)

            # Check docking location
            loc = ref.get_vessel_location(depot_id)
            self.assertEqual(loc['station_id'], st)
            self.assertEqual(loc['status'], 'docked')

        # 3. Leaderboard excludes depot accounts
        leaderboard = ref.get_leaderboard()
        leaderboard_agents = [r['agent_id'] for r in leaderboard]
        for st in STATIONS:
            self.assertNotIn(f"depot_{st}", leaderboard_agents)
        for expected in ('amos', 'marvin', 'zero', 'aerial'):
            self.assertIn(expected, leaderboard_agents)

        # 4. Fleet locations exclude depots
        vessel_locs = ref.get_all_vessel_locations()
        loc_agents = [l['agent_id'] for l in vessel_locs]
        for st in STATIONS:
            self.assertNotIn(f"depot_{st}", loc_agents)

    @pre_162_surface()
    def test_depot_pricing_spec(self):
        """
        Verify fundamental prices matching Issue #71 spec:
        - Earth Depot sells FRAG @ ~10-11 CR and buys FUEL @ ~8-9 CR
        - Ceres Depot buys FRAG @ ~21-22 CR and sells FUEL @ ~25-26 CR
        """
        ref = AgoraReferee(depots=True)
        summary = ref.get_depot_summary()
        self.assertTrue(summary['depots_enabled'])

        # Earth
        earth_frag = summary['stations']['earth']['FRAG']
        self.assertEqual(earth_frag['best_ask'], 11)   # Sells FRAG @ 11 CR (~10-11 CR)
        self.assertEqual(earth_frag['best_bid'], 10)
        self.assertEqual(earth_frag['bid_depth'], 1500)
        self.assertEqual(earth_frag['ask_depth'], 1500)

        earth_fuel = summary['stations']['earth']['FUEL']
        self.assertEqual(earth_fuel['best_bid'], 8)    # Buys FUEL @ 8 CR (~8-9 CR)
        self.assertEqual(earth_fuel['best_ask'], 9)

        # Ceres
        ceres_frag = summary['stations']['ceres']['FRAG']
        self.assertEqual(ceres_frag['best_bid'], 21)   # Buys FRAG @ 21 CR (~21-22 CR)
        self.assertEqual(ceres_frag['best_ask'], 22)

        ceres_fuel = summary['stations']['ceres']['FUEL']
        self.assertEqual(ceres_fuel['best_ask'], 26)   # Sells FUEL @ 26 CR (~25-26 CR)
        self.assertEqual(ceres_fuel['best_bid'], 25)

    @pre_162_surface()
    def test_cross_system_spatial_arbitrage_trade(self):
        """
        Full lifecycle test:
        1. Agent buys FRAG at Earth from Earth Depot (11 CR).
        2. Agent transits from Earth to Ceres carrying FRAG.
        3. Agent arrives at Ceres and sells FRAG to Ceres Depot (21 CR).
        4. Verifies profit, ledger conservation, and inventory balance updates.
        """
        ref = AgoraReferee(depots=True)

        # Dock zero at Earth
        with ref.conn:
            ref.conn.execute("UPDATE vessel_locations SET station_id = 'earth', docked_since = 0 WHERE agent_id = 'zero'")

        initial_zero_cr = ref.get_balance('zero', 'CR')
        initial_zero_frag = ref.get_balance('zero', 'FRAG')
        self.assertEqual(initial_zero_cr, 10000)

        # 1. Zero buys 100 FRAG at Earth (Earth depot ask = 11 CR)
        buy_res = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'zero-buy-earth-frag',
                'agent_id': 'zero',
                'station_id': 'earth',
                'instrument': 'FRAG',
                'side': 'bid',
                'qty': 100,
                'limit_price': 11,
                'seq_seen': 0
            }
        })
        self.assertEqual(buy_res['payload']['trades_count'], 1)
        self.assertEqual(buy_res['payload']['last_price'], 11)

        # Check balances
        self.assertEqual(ref.get_balance('zero', 'CR'), initial_zero_cr - 1100)
        self.assertEqual(ref.get_balance('zero', 'FRAG'), initial_zero_frag + 100)
        self.assertEqual(ref.get_balance('depot_earth', 'CR'), 1000000 + 1100)
        self.assertEqual(ref.get_balance('depot_earth', 'FRAG'), 100000 - 100)

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger error after buy: {errors}")

        # 2. Zero transits to Ceres carrying 100 FRAG (takes 3 rounds, burns 30 fuel)
        transit_res = ref.initiate_transit(
            agent_id='zero',
            destination='ceres',
            commodity='FRAG',
            cargo_qty=100
        )
        self.assertEqual(transit_res['status'], 'in_transit')
        self.assertEqual(transit_res['payload']['destination'], 'ceres')

        # Advance rounds until arrived
        ref.step_round()
        ref.step_round()
        step3 = ref.step_round()
        self.assertEqual(len(step3['arrived_transits']), 1)
        self.assertEqual(step3['arrived_transits'][0]['destination'], 'ceres')

        # Zero is now docked at Ceres
        zero_loc = ref.get_vessel_location('zero')
        self.assertEqual(zero_loc['station_id'], 'ceres')
        self.assertEqual(zero_loc['status'], 'docked')

        # 3. Zero sells 100 FRAG at Ceres to Ceres Depot at current resting bid
        depot_bid = ref.get_depot_summary()['stations']['ceres']['FRAG']['best_bid']
        self.assertIsNotNone(depot_bid)
        sell_res = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'zero-sell-ceres-frag',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'ask',
                'qty': 100,
                'limit_price': depot_bid,
                'seq_seen': ref.current_seq
            }
        })
        self.assertEqual(sell_res['payload']['trades_count'], 1)
        self.assertEqual(sell_res['payload']['last_price'], depot_bid)

        # Net cash change: -1100 CR (buy) - 25 CR (belt toll) + depot_bid*100 (sell)
        toll_paid = transit_res['payload']['toll_paid']
        expected_cash = (initial_zero_cr - 1100) - toll_paid + (depot_bid * 100)
        self.assertEqual(ref.get_balance('zero', 'CR'), expected_cash)
        self.assertEqual(ref.get_balance('zero', 'FRAG'), initial_zero_frag)
        self.assertGreater(expected_cash, initial_zero_cr)  # Profitable arbitrage!

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger error after round-trip: {errors}")

    def test_step_round_refreshes_depot_quotes_with_drift(self):
        """As rounds advance and prices drift, depot quotes refresh to track spot prices."""
        ref = AgoraReferee(depots=True)
        summary_0 = ref.get_depot_summary()
        self.assertIsNotNone(summary_0['stations']['earth']['FRAG']['best_ask'])

        # Advance 5 rounds
        for _ in range(5):
            ref.step_round()

        summary_5 = ref.get_depot_summary()
        # Ensure depot quotes remain intact and active
        for st in STATIONS:
            for comm in COMMODITIES:
                item = summary_5['stations'][st][comm]
                self.assertIsNotNone(item['best_bid'])
                self.assertIsNotNone(item['best_ask'])
                self.assertGreater(item['ask_depth'], 0)
                self.assertGreater(item['bid_depth'], 0)
                self.assertGreaterEqual(item['best_ask'], item['best_bid'] + 1)

    @pre_162_surface()
    def test_reset_and_new_game_depots_flags(self):
        """reset_to_genesis(depots=True) and new_game(depots=True) preserve depot depth."""
        ref = AgoraReferee()
        self.assertFalse(ref.depots_enabled)

        # Reset with depots=True
        ref.reset_to_genesis(depots=True)
        self.assertTrue(ref.depots_enabled)
        self.assertEqual(ref.get_depot_summary()['stations']['earth']['FRAG']['best_ask'], 11)

        # New game with depots=True
        ref.new_game(seed=999, warmup_rounds=3, depots=True)
        self.assertTrue(ref.depots_enabled)
        summary = ref.get_depot_summary()
        self.assertIsNotNone(summary['stations']['ceres']['FUEL']['best_ask'])

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid)


class TestDepotServerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.referee = AgoraReferee(depots=True)
        cls.auth_tokens = {
            'amos': 'tok-amos',
            'zero': 'tok-zero',
            'admin': 'tok-admin',
        }
        handler_class = make_handler(cls.referee, auth_tokens=cls.auth_tokens)
        cls.server = HTTPServer(('127.0.0.1', 0), handler_class)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.referee.reset_to_genesis(depots=True)

    def _get(self, path: str):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    def _post(self, path: str, payload: dict, token: str = 'tok-admin'):
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    def test_get_referee_depots(self):
        """GET /referee/depots returns active station depot quotes and depths."""
        status, data = self._get('/referee/depots')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(data['depots']['depots_enabled'])
        self.assertEqual(data['depots']['stations']['earth']['FRAG']['best_ask'], 21)  # #243 inverted surface (was 12 on #162)

    def test_post_admin_depots_refresh(self):
        """POST /referee/admin/depots/refresh refreshes quotes."""
        status, data = self._post('/referee/admin/depots/refresh', {}, token='tok-admin')
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'depots_refreshed')

    def test_admin_new_game_with_depots(self):
        """POST /referee/admin/new_game with depots=true re-seeds depot liquidity."""
        status, data = self._post('/referee/admin/new_game', {'confirm': True, 'depots': True}, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'new_game_ok')
        self.assertTrue(self.referee.depots_enabled)


if __name__ == '__main__':
    unittest.main()
