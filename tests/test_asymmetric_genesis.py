"""
Unit tests for Issue #72: Asymmetric Fleet Genesis Spawn Locations.
Tests dispersal across Sol stations, admin reset / new_game endpoints,
ledger conservation invariants, and spatial order locality.
"""

import json
import unittest
from agora.referee import AgoraReferee, ASYMMETRIC_SPAWN_LOCATIONS


class TestAsymmetricGenesis(unittest.TestCase):
    def test_default_remains_ceres_baseline(self):
        """Default initialization preserves flat Ceres baseline for backward compatibility."""
        ref = AgoraReferee()
        locs = {l['agent_id']: l['station_id'] for l in ref.get_all_vessel_locations()}
        for agent in ('amos', 'marvin', 'zero', 'aerial'):
            self.assertEqual(locs.get(agent), 'ceres')

    def test_asymmetric_init_disperses_fleets(self):
        """AgoraReferee(asymmetric=True) disperses fleets across Sol nodes."""
        ref = AgoraReferee(asymmetric=True)
        locs = {l['agent_id']: l['station_id'] for l in ref.get_all_vessel_locations()}
        self.assertEqual(locs.get('zero'), 'earth')
        self.assertEqual(locs.get('amos'), 'ceres')
        self.assertEqual(locs.get('marvin'), 'mars')
        self.assertEqual(locs.get('aerial'), 'luna')

        # Double-entry ledger conservation invariant holds
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid)
        self.assertEqual(len(errors), 0)

    def test_reset_to_genesis_with_asymmetric_flag(self):
        """reset_to_genesis(asymmetric=True) resets and applies asymmetric dispersal."""
        ref = AgoraReferee()
        # Initially at Ceres
        self.assertEqual(ref.get_vessel_location('aerial')['station_id'], 'ceres')

        # Reset with asymmetric flag
        ref.reset_to_genesis(asymmetric=True)
        locs = {l['agent_id']: l['station_id'] for l in ref.get_all_vessel_locations()}
        self.assertEqual(locs.get('zero'), 'earth')
        self.assertEqual(locs.get('amos'), 'ceres')
        self.assertEqual(locs.get('marvin'), 'mars')
        self.assertEqual(locs.get('aerial'), 'luna')

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid)

    def test_new_game_with_asymmetric_flag(self):
        """new_game(asymmetric=True) rolls opening market and applies asymmetric dispersal."""
        ref = AgoraReferee()
        res = ref.new_game(seed=12345, warmup_rounds=4, asymmetric=True)
        self.assertEqual(res['seq'], 0)

        locs = {l['agent_id']: l['station_id'] for l in ref.get_all_vessel_locations()}
        self.assertEqual(locs.get('zero'), 'earth')
        self.assertEqual(locs.get('amos'), 'ceres')
        self.assertEqual(locs.get('marvin'), 'mars')
        self.assertEqual(locs.get('aerial'), 'luna')

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid)

    def test_custom_spawn_map(self):
        """Custom spawn maps allow arbitrary tournament layouts."""
        ref = AgoraReferee()
        custom = {'zero': 'luna', 'aerial': 'mars', 'amos': 'earth', 'marvin': 'ceres'}
        ref.set_asymmetric_roster(custom)

        locs = {l['agent_id']: l['station_id'] for l in ref.get_all_vessel_locations()}
        for ag, st in custom.items():
            self.assertEqual(locs.get(ag), st)

    def test_spatial_order_locality_at_asymmetric_stations(self):
        """Vessels can only trade at their docked station."""
        ref = AgoraReferee(asymmetric=True)

        # Aerial is docked at Luna
        self.assertEqual(ref.get_vessel_location('aerial')['station_id'], 'luna')

        # Aerial places bid at Luna -> OK
        ok_order = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'aerial-luna-bid-1',
                'agent_id': 'aerial',
                'station_id': 'luna',
                'instrument': 'FRAG',
                'side': 'bid',
                'qty': 10,
                'limit_price': 12,
                'seq_seen': 0
            }
        })
        self.assertIn(ok_order['kind'], ('status', 'market_tick'))

        # Aerial attempts to place order at Ceres -> Rejected (vessel_not_docked)
        bad_order = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'aerial-ceres-bid-1',
                'agent_id': 'aerial',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'bid',
                'qty': 10,
                'limit_price': 22,
                'seq_seen': 0
            }
        })
        self.assertEqual(bad_order['kind'], 'reject')
        self.assertEqual(bad_order['payload']['reason'], 'vessel_not_docked')



class TestAsymmetricServerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        import urllib.request
        from http.server import HTTPServer
        from agora.server import make_handler

        cls.referee = AgoraReferee()
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

    def _post(self, path: str, payload: dict, token: str = 'tok-admin'):
        import urllib.request
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    def test_http_admin_reset_asymmetric(self):
        """POST /referee/admin/reset with asymmetric=true disperses fleets."""
        status, data = self._post('/referee/admin/reset', {'confirm': True, 'asymmetric': True})
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'reset_ok')

        locs = {l['agent_id']: l['station_id'] for l in self.referee.get_all_vessel_locations()}
        self.assertEqual(locs.get('zero'), 'earth')
        self.assertEqual(locs.get('amos'), 'ceres')
        self.assertEqual(locs.get('marvin'), 'mars')
        self.assertEqual(locs.get('aerial'), 'luna')

    def test_http_admin_new_game_asymmetric(self):
        """POST /referee/admin/new_game with asymmetric=true rolls opening market and disperses fleets."""
        status, data = self._post('/referee/admin/new_game', {'confirm': True, 'asymmetric': True}, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'new_game_ok')

        locs = {l['agent_id']: l['station_id'] for l in self.referee.get_all_vessel_locations()}
        self.assertEqual(locs.get('zero'), 'earth')
        self.assertEqual(locs.get('amos'), 'ceres')
        self.assertEqual(locs.get('marvin'), 'mars')
        self.assertEqual(locs.get('aerial'), 'luna')


if __name__ == '__main__':
    unittest.main()
