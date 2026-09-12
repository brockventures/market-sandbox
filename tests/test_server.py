"""
tests/test_server.py - Integration and unit tests for Agora HTTP/REST referee server.
Exercises Section 3 endpoints: /referee/health, /referee/book, /referee/orders, /referee/ticks, /referee/accounts.
"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
from typing import Optional

from agora.referee import AgoraReferee
from agora.server import make_handler


class TestAgoraServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.referee = AgoraReferee()
        cls.auth_tokens = {
            'amos': 'tok-amos',
            'zero': 'tok-zero',
            'marvin': 'tok-marvin',
            'admin': 'tok-admin',
        }
        handler_class = make_handler(cls.referee, auth_tokens=cls.auth_tokens)
        # Bind to port 0 to dynamically select an ephemeral free port
        cls.server = HTTPServer(('127.0.0.1', 0), handler_class)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path: str, token: Optional[str] = None):
        url = f"{self.base_url}{path}"
        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return resp.status, data
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode('utf-8'))
            return e.code, data

    def _post(self, path: str, payload: dict, token: Optional[str] = None):
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(url, data=data_bytes, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return resp.status, data
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode('utf-8'))
            return e.code, data

    def test_01_health_check(self):
        status, data = self._get('/referee/health')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['floor'], 'open')
        self.assertTrue(data['invariants_valid'])

    def test_02_book_snapshot_initially_empty(self):
        status, data = self._get('/referee/book')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(len(data['book']['bids']), 0)
        self.assertEqual(len(data['book']['asks']), 0)

    def test_03_accounts_query_auth(self):
        # 1. Unauthenticated query rejected with 401
        status, data = self._get('/referee/accounts')
        self.assertEqual(status, 401)
        self.assertEqual(data['payload']['reason'], 'unauthorized')

        # 2. Admin token can view all accounts
        status, data = self._get('/referee/accounts', token='tok-admin')
        self.assertEqual(status, 200)
        self.assertTrue(len(data['accounts']) >= 6)

        # 3. Agent token views own account
        status, data = self._get('/referee/accounts', token='tok-amos')
        self.assertEqual(status, 200)
        instruments = {a['instrument']: a['balance'] for a in data['accounts']}
        self.assertEqual(instruments.get('CR'), 10000)
        self.assertEqual(instruments.get('FRAG'), 1000)

        # 4. Cross-agent inspection blocked with 403
        status, data = self._get('/referee/accounts?agent_id=marvin', token='tok-amos')
        self.assertEqual(status, 403)
        self.assertEqual(data['payload']['reason'], 'unauthorized')

    def test_04_submit_order_and_fill(self):
        # 1. Amos posts ask with tok-amos: Sell 40 FRAG @ 15
        ask_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'http-ask-001', 'agent_id': 'amos', 'instrument': 'FRAG',
                'side': 'ask', 'qty': 40, 'limit_price': 15, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', ask_env, token='tok-amos')
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'market_tick')
        self.assertEqual(data['payload']['best_ask'], 15)

        # Check book shows resting ask (public read)
        _, book_data = self._get('/referee/book')
        self.assertEqual(len(book_data['book']['asks']), 1)
        self.assertEqual(book_data['book']['asks'][0]['order_id'], 'http-ask-001')

        # 2. Zero crosses ask with tok-zero: Buy 40 FRAG @ 15
        bid_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'http-bid-001', 'agent_id': 'zero', 'instrument': 'FRAG',
                'side': 'bid', 'qty': 40, 'limit_price': 15, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', bid_env, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'market_tick')
        self.assertEqual(data['payload']['trades_count'], 1)

        # Check balances updated
        _, acct_zero = self._get('/referee/accounts', token='tok-zero')
        zero_map = {a['instrument']: a['balance'] for a in acct_zero['accounts']}
        self.assertEqual(zero_map['CR'], 10000 - 600)  # 40 * 15 = 600
        self.assertEqual(zero_map['FRAG'], 1000 + 40)

    def test_05_ticks_endpoint(self):
        status, data = self._get('/referee/ticks?since_seq=0')
        self.assertEqual(status, 200)
        self.assertTrue(data['current_seq'] > 0)
        self.assertTrue(len(data['ticks']) >= 2)

    def test_06_reject_insolvent_order(self):
        insolvent_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'http-insolvent', 'agent_id': 'marvin', 'instrument': 'FRAG',
                'side': 'bid', 'qty': 5000, 'limit_price': 100, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', insolvent_env, token='tok-marvin')
        self.assertEqual(status, 400)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'insufficient_balance')

    def test_07_not_found(self):
        status, data = self._post('/referee/unknown', {}, token='tok-zero')
        self.assertEqual(status, 404)

    def test_08_unauthenticated_order_rejected(self):
        # Marvin's exploit scenario: anonymous caller attempts to forge order as amos
        forged_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'attacker-forged-1', 'agent_id': 'amos', 'instrument': 'FRAG',
                'side': 'ask', 'qty': 500, 'limit_price': 1, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', forged_env, token=None)
        self.assertEqual(status, 401)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'unauthorized')
        self.assertIn('Missing Authorization header', data['payload']['detail'])

    def test_09_impersonated_order_rejected(self):
        # Caller authenticates as zero but claims agent_id is amos
        forged_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'cross-agent-forge-1', 'agent_id': 'amos', 'instrument': 'FRAG',
                'side': 'ask', 'qty': 500, 'limit_price': 1, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', forged_env, token='tok-zero')
        self.assertEqual(status, 403)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'unauthorized')
        self.assertIn("Authenticated as 'zero', but payload claims agent_id 'amos'", data['payload']['detail'])

    def test_10_invalid_token_rejected(self):
        env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'bad-tok-order', 'agent_id': 'zero', 'instrument': 'FRAG',
                'side': 'bid', 'qty': 10, 'limit_price': 10, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', env, token='wrong-garbage-token')
        self.assertEqual(status, 401)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'unauthorized')

    def test_11_leaderboard_endpoint(self):
        status, data = self._get('/referee/leaderboard')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('leaderboard', data)
        self.assertEqual(len(data['leaderboard']), 3)

        # Verify ranking and structure
        for entry in data['leaderboard']:
            self.assertIn('agent_id', entry)
            self.assertIn('net_worth', entry)
            self.assertIn('liquid', entry)
            self.assertIn('frags', entry)
            self.assertIn('mark_price', entry)
            self.assertTrue(isinstance(entry['net_worth'], int))

    def test_12_floor_control_and_kill_switch(self):
        # 1. Non-admin cannot set floor
        status, data = self._post('/referee/admin/floor', {'floor': 'closed'}, token='tok-amos')
        self.assertEqual(status, 403)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'unauthorized')

        # 2. Admin halts market (kill switch)
        status, data = self._post('/referee/admin/floor', {'floor': 'closed'}, token='tok-admin')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['floor'], 'closed')

        # 3. Floor is closed in health and public endpoint
        status, health = self._get('/referee/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['floor'], 'closed')

        status, floor_info = self._get('/referee/floor')
        self.assertEqual(status, 200)
        self.assertEqual(floor_info['floor'], 'closed')

        # 4. Order submission rejected while floor is closed
        order_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'halted-order-001', 'agent_id': 'amos', 'instrument': 'FRAG',
                'side': 'ask', 'qty': 10, 'limit_price': 20, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', order_env, token='tok-amos')
        self.assertEqual(status, 400)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'market_halted')

        # 5. Admin resumes market
        status, data = self._post('/referee/admin/floor', {'action': 'resume'}, token='tok-admin')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['floor'], 'open')

        # 6. Order submission succeeds after resume
        status, data = self._post('/referee/orders', order_env, token='tok-amos')
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'market_tick')
        self.assertEqual(data['floor'], 'open')

    def test_10_instructions_endpoint(self):
        # 1. Standard JSON query
        status, data = self._get('/referee/instructions')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['round_bell_role'], '<@&1543462881624858624>')
        self.assertIn('endpoints', data)
        self.assertIn('rules', data)
        self.assertIn('fleet', data)
        self.assertIn('markdown', data)

        # 2. Alias /referee/rules works identically
        status, rules_data = self._get('/referee/rules')
        self.assertEqual(status, 200)
        self.assertEqual(rules_data['title'], data['title'])

        # 3. Raw markdown request
        req = urllib.request.Request(f"{self.base_url}/referee/instructions?format=raw")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn('text/markdown', resp.headers.get('Content-Type', ''))
            content = resp.read().decode('utf-8')
            self.assertIn('Station Agora', content)
            self.assertIn('1543462881624858624', content)

    def test_11_cancel_orders(self):
        # 1. Place a resting bid as zero
        bid = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'zero-bid-cancel-1', 'agent_id': 'zero', 'instrument': 'FRAG',
                'side': 'bid', 'qty': 5, 'limit_price': 15, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', bid, token='tok-zero')
        self.assertEqual(status, 200)

        # 2. Cancel the resting bid
        status, data = self._post('/referee/orders/cancel', {'order_id': 'zero-bid-cancel-1'}, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'cancelled')
        self.assertEqual(data['payload']['released_qty'], 5)

        # 3. Idempotent cancel rejected cleanly
        status, data = self._post('/referee/orders/cancel', {'order_id': 'zero-bid-cancel-1'}, token='tok-zero')
        self.assertEqual(status, 400)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'order_not_cancellable')

        # 4. Place 2 resting asks and cancel_all
        ask1 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'zero-ask-cancel-1', 'agent_id': 'zero', 'instrument': 'FRAG',
                'side': 'ask', 'qty': 2, 'limit_price': 50, 'seq_seen': self.referee.current_seq
            }
        }
        ask2 = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'zero-ask-cancel-2', 'agent_id': 'zero', 'instrument': 'FRAG',
                'side': 'ask', 'qty': 3, 'limit_price': 55, 'seq_seen': self.referee.current_seq
            }
        }
        self._post('/referee/orders', ask1, token='tok-zero')
        self._post('/referee/orders', ask2, token='tok-zero')

        status, data = self._post('/referee/orders/cancel_all', {}, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'cancelled_all')
        self.assertEqual(data['payload']['count'], 2)

    def test_12_galnet_endpoints(self):
        # 1. Initial feed and drift
        status, data = self._get('/galnet/feed')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIsInstance(data['feed'], list)

        status, data = self._get('/galnet/drift?station_id=ceres&commodity=FUEL')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['drift_bias'], 0.0)

        # 2. Force shock via POST /galnet/shock (Template 0 is Ceres FUEL blowout, bias +0.40, duration 4)
        status, data = self._post('/galnet/shock', {'template_idx': 0, 'round': 1})
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['event']['station_id'], 'ceres')
        self.assertEqual(data['event']['commodity'], 'FUEL')
        self.assertEqual(data['event']['drift_bias'], 0.40)

        # 3. Verify active drift reflects shock
        status, data = self._get('/galnet/drift?station_id=ceres&commodity=FUEL')
        self.assertEqual(status, 200)
        self.assertEqual(data['drift_bias'], 0.40)

        # 4. Verify feed and active_shocks
        status, data = self._get('/galnet/events')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['active_shocks']), 1)
        self.assertEqual(data['active_shocks'][0]['station_id'], 'ceres')
        self.assertTrue(len(data['feed']) >= 1)

        # 5. Verify news wire tick is streamed via /referee/ticks
        status, data = self._get('/referee/ticks?since_seq=0')
        self.assertEqual(status, 200)
        news_ticks = [t for t in data['ticks'] if t['kind'] == 'news']
        self.assertTrue(len(news_ticks) >= 1)
        self.assertEqual(news_ticks[-1]['payload']['station_id'], 'ceres')
        self.assertIn('CERES', news_ticks[-1]['payload']['headline'])

        # 6. Step round past duration (round 6 > round 1 + duration 4) and verify expiration
        status, data = self._post('/galnet/step', {'round': 6})
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['round'], 6)

        status, data = self._get('/galnet/drift?station_id=ceres&commodity=FUEL')
        self.assertEqual(status, 200)
        self.assertEqual(data['drift_bias'], 0.0)

    def test_16_spatial_routes_and_prices_query(self):
        # 1. Prices query across Sol nodes
        status, data = self._get('/stations/prices')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('prices', data['data'])
        self.assertIn('ceres', data['data']['prices'])
        self.assertIn('mars', data['data']['prices'])
        self.assertIn('earth', data['data']['prices'])
        self.assertIn('luna', data['data']['prices'])

        # Filtered price query
        status, data = self._get('/stations/prices?station_id=mars&commodity=FRAG')
        self.assertEqual(status, 200)
        self.assertEqual(data['data']['station_id'], 'mars')
        self.assertEqual(data['data']['commodity'], 'FRAG')
        self.assertGreater(data['data']['spot_price'], 0)

        # 2. Routes query
        status, data = self._get('/stations/routes')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(len(data['routes']) >= 12)

        # Specific route lookup
        status, data = self._get('/stations/routes?origin=ceres&destination=mars')
        self.assertEqual(status, 200)
        self.assertEqual(data['route']['rounds'], 2)
        self.assertEqual(data['route']['fuel'], 20)

        # 3. Initial vessel locations query
        status, data = self._get('/stations/locations')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(len(data['locations']) >= 3)

        status, data = self._get('/stations/locations?agent_id=amos')
        self.assertEqual(status, 200)
        self.assertEqual(data['location']['agent_id'], 'amos')
        self.assertEqual(data['location']['station_id'], 'ceres')
        self.assertEqual(data['location']['status'], 'docked')

    def test_17_spatial_transit_lifecycle_and_invariants(self):
        # 1. Unauthenticated transit rejected
        status, data = self._post('/stations/transit', {
            'agent_id': 'zero',
            'destination': 'mars',
            'commodity': 'FRAG',
            'cargo_qty': 50
        })
        self.assertEqual(status, 401)

        # 2. Impersonation rejected
        status, data = self._post('/stations/transit', {
            'agent_id': 'amos',
            'destination': 'mars',
            'commodity': 'FRAG',
            'cargo_qty': 50
        }, token='tok-zero')
        self.assertEqual(status, 403)

        # 3. Invalid destination rejected
        status, data = self._post('/stations/transit', {
            'agent_id': 'zero',
            'destination': 'jupiter',
            'commodity': 'FRAG',
            'cargo_qty': 50
        }, token='tok-zero')
        self.assertEqual(status, 400)
        self.assertEqual(data['payload']['reason'], 'invalid_station')

        # 4. Check initial balances for zero
        status, data = self._get('/referee/accounts', token='tok-zero')
        self.assertEqual(status, 200)
        balances = {a['instrument']: a['balance'] for a in data['accounts']}
        init_fuel = balances.get('FUEL', 500)
        init_frag = balances.get('FRAG', 1000)

        # 5. Successful transit departure: zero departs ceres for mars with 100 FRAG cargo
        # Route ceres -> mars requires 20 fuel and 2 rounds
        status, data = self._post('/stations/transit', {
            'agent_id': 'zero',
            'destination': 'mars',
            'commodity': 'FRAG',
            'cargo_qty': 100
        }, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'in_transit')
        transit_payload = data['payload']
        self.assertEqual(transit_payload['origin'], 'ceres')
        self.assertEqual(transit_payload['destination'], 'mars')
        self.assertEqual(transit_payload['cargo_qty'], 100)
        self.assertEqual(transit_payload['fuel_burned'], 20)

        # 6. Verify fuel debit and cargo escrow
        status, data = self._get('/referee/accounts', token='tok-zero')
        self.assertEqual(status, 200)
        updated_balances = {a['instrument']: a['balance'] for a in data['accounts']}
        self.assertEqual(updated_balances['FUEL'], init_fuel - 20)
        self.assertEqual(updated_balances['FRAG'], init_frag - 100)

        # 7. Verify vessel location reflects in_transit
        status, data = self._get('/stations/locations?agent_id=zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['location']['status'], 'in_transit')
        self.assertEqual(data['location']['station_id'], 'in_transit')
        self.assertIsNotNone(data['location']['transit'])

        # 8. In-transit order submission rejected (vessel_in_transit)
        status, data = self._post('/referee/orders', {
            'kind': 'order',
            'payload': {
                'order_id': 'zero-flight-order-1',
                'agent_id': 'zero',
                'instrument': 'FRAG',
                'side': 'bid',
                'qty': 10,
                'limit_price': 10,
                'seq_seen': 0
            }
        }, token='tok-zero')
        self.assertEqual(status, 400)
        self.assertEqual(data['payload']['reason'], 'vessel_in_transit')

        # 9. Step round 1: still in flight (arrival is round 2)
        status, data = self._post('/stations/step_round', {'round': 1})
        self.assertEqual(status, 200)
        self.assertEqual(data['round'], 1)
        self.assertEqual(len(data['arrived_transits']), 0)

        # 10. Step round 2: arrival triggered!
        status, data = self._post('/stations/step_round', {'round': 2})
        self.assertEqual(status, 200)
        self.assertEqual(data['round'], 2)
        self.assertEqual(len(data['arrived_transits']), 1)
        self.assertEqual(data['arrived_transits'][0]['agent_id'], 'zero')
        self.assertEqual(data['arrived_transits'][0]['destination'], 'mars')

        # 11. Verify cargo released back to zero at destination mars
        status, data = self._get('/referee/accounts', token='tok-zero')
        self.assertEqual(status, 200)
        arrived_balances = {a['instrument']: a['balance'] for a in data['accounts']}
        self.assertEqual(arrived_balances['FRAG'], init_frag)
        self.assertEqual(arrived_balances['FUEL'], init_fuel - 20)

        # 12. Verify docked at mars
        status, data = self._get('/stations/locations?agent_id=zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['location']['status'], 'docked')
        self.assertEqual(data['location']['station_id'], 'mars')

        # 13. Submitting order at ceres rejected (vessel_not_docked)
        status, data = self._post('/referee/orders', {
            'kind': 'order',
            'payload': {
                'order_id': 'zero-ceres-order-1',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'ask',
                'qty': 10,
                'limit_price': 25,
                'seq_seen': 0
            }
        }, token='tok-zero')
        self.assertEqual(status, 400)
        self.assertEqual(data['payload']['reason'], 'vessel_not_docked')

        # 14. Submitting order at mars succeeds!
        status, data = self._post('/referee/orders', {
            'kind': 'order',
            'payload': {
                'order_id': 'zero-mars-order-1',
                'agent_id': 'zero',
                'station_id': 'mars',
                'instrument': 'FRAG',
                'side': 'ask',
                'qty': 10,
                'limit_price': 18,
                'seq_seen': 0
            }
        }, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'market_tick')
        self.assertEqual(data['payload']['station_id'], 'mars')

        # 15. Verify book at mars shows the resting ask
        status, data = self._get('/referee/book?station_id=mars&instrument=FRAG')
        self.assertEqual(status, 200)
        self.assertEqual(data['station_id'], 'mars')
        self.assertEqual(len(data['book']['asks']), 1)
        self.assertEqual(data['book']['asks'][0]['order_id'], 'zero-mars-order-1')

        # 16. Verify standing invariants hold completely (conservation, non-negativity, reconciliation)
        status, data = self._get('/referee/health')
        self.assertEqual(status, 200)
        self.assertTrue(data['invariants_valid'])
        self.assertEqual(len(data['errors']), 0)

    def test_terminal_hud_endpoint(self):
        """Verify root and /terminal serve public/terminal.html."""
        url = f"{self.base_url}/terminal"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn('text/html', resp.headers.get('Content-Type', ''))
            body = resp.read().decode('utf-8')
            self.assertIn('Sol System // Orbital Orrery &amp; Transit Radar', body)
            self.assertIn('LIQUIDITY DEPTH MOUNTAINS', body)
            self.assertIn('drawDepthMountain', body)
            self.assertIn('initOrbitalRadar', body)
            self.assertIn('DYNAMIC LULD CIRCUIT BREAKERS', body)
            self.assertIn('pollCircuitBreakerTelemetry', body)

    def test_18_equity_endpoints_and_borrow_flow(self):
        """Integration test for /equity/summary, /equity/loans, /equity/borrow, and /equity/return."""
        # 1. Verify summary endpoint
        status, data = self._get('/equity/summary')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('EQ_AMOS', data['equities'])
        self.assertIn('EQ_MARV', data['equities'])
        self.assertIn('EQ_ZERO', data['equities'])

        # 2. Unauthenticated borrow fails
        status, data = self._post('/equity/borrow', {
            'equity_symbol': 'EQ_AMOS',
            'shares': 50,
            'collateral_cr': 1200
        })
        self.assertEqual(status, 401)

        # 3. Self-short fails
        status, data = self._post('/equity/borrow', {
            'equity_symbol': 'EQ_AMOS',
            'shares': 50,
            'collateral_cr': 1200
        }, token='tok-amos')
        self.assertEqual(status, 400)
        self.assertEqual(data['reason'], 'self_short_prohibited')

        # 4. Valid borrow: zero shorts EQ_AMOS
        status, data = self._post('/equity/borrow', {
            'equity_symbol': 'EQ_AMOS',
            'shares': 50,
            'collateral_cr': 2700,
            'lender_id': 'amos'
        }, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        loan_id = data['loan_id']

        # 5. Verify active loans endpoint
        status, data = self._get('/equity/loans?borrower_id=zero')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['loans']), 1)
        self.assertEqual(data['loans'][0]['loan_id'], loan_id)
        self.assertEqual(data['loans'][0]['status'], 'active')

        # 6. Return loan
        status, data = self._post('/equity/return', {
            'loan_id': loan_id
        }, token='tok-zero')
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['shares_returned'], 50)
        self.assertEqual(data['collateral_released'], 2700)

        # 7. Verify standing invariants hold completely
        status, data = self._get('/referee/health')
        self.assertEqual(status, 200)
        self.assertTrue(data['invariants_valid'])
        self.assertEqual(len(data['errors']), 0)

    def test_19_orbital_windows_and_belt_mechanics(self):
        """Integration test for /stations/windows, alignment route data, and perishable transit."""
        # 1. Verify /stations/windows
        status, data = self._get('/stations/windows')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('windows', data)
        self.assertEqual(len(data['windows']), 3)
        corridors = {w['corridor_id'] for w in data['windows']}
        self.assertIn('earth_mars', corridors)
        self.assertIn('mars_ceres', corridors)
        self.assertIn('earth_ceres', corridors)

        # 2. Verify /stations/routes returns windows and route properties
        status, data = self._get('/stations/routes')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('windows', data)
        ceres_routes = [r for r in data['routes'] if r['origin'] == 'ceres' and r['destination'] == 'mars']
        self.assertTrue(len(ceres_routes) > 0)
        r = ceres_routes[0]
        self.assertTrue(r['is_belt_route'])
        self.assertEqual(r['toll'], 25)
        self.assertEqual(r['decay_rate'], 0.05)

        # 3. Step round to 4 (Earth-Mars opposition active)
        status, data = self._post('/stations/step_round', {'round': 4})
        self.assertEqual(status, 200)

        # Check /stations/routes during alignment
        status, data = self._get('/stations/routes?origin=earth&destination=mars')
        self.assertEqual(status, 200)
        self.assertTrue(data['route']['is_aligned'])
        self.assertEqual(data['route']['rounds'], 1)  # Halved from 2 to 1!

        # 4. Verify standing invariants hold completely
        status, data = self._get('/referee/health')
        self.assertEqual(status, 200)
        self.assertTrue(data['invariants_valid'])
        self.assertEqual(len(data['errors']), 0)

    def test_20_salvage_and_rescue_endpoints(self):
        # 1. GET /salvage/summary initially clean
        status, data = self._get('/salvage/summary')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('summary', data)

        # 2. amos declares distress
        status, data = self._post(
            '/salvage/distress',
            {
                'location': 'mars',
                'cargo_bounty': {'FRAG': 50},
                'fuel_needed': 15,
                'max_reward_cr': 200
            },
            token=self.auth_tokens['amos']
        )
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        beacon_id = data['beacon_id']
        rfq_id = data['rfq_id']

        # 3. GET /salvage/beacons verifies active beacon
        status, data = self._get('/salvage/beacons?status=active')
        self.assertEqual(status, 200)
        self.assertTrue(any(b['beacon_id'] == beacon_id for b in data['beacons']))

        # 4. marvin quotes rescue
        status, data = self._post(
            '/salvage/quote',
            {
                'rfq_id': rfq_id,
                'fuel_offered': 15,
                'price_cr': 120
            },
            token=self.auth_tokens['marvin']
        )
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        quote_id = data['quote_id']

        # 5. amos accepts rescue quote
        status, data = self._post(
            '/salvage/accept_quote',
            {'quote_id': quote_id},
            token=self.auth_tokens['amos']
        )
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'rescued')

        # 6. zero declares distress and marvin claims salvage
        status, data = self._post(
            '/salvage/distress',
            {
                'location': 'ceres_earth',
                'cargo_bounty': {'FRAG': 25},
                'fuel_needed': 30
            },
            token=self.auth_tokens['zero']
        )
        self.assertEqual(status, 200)
        zero_beacon = data['beacon_id']

        # marvin claims derelict
        status, data = self._post(
            '/salvage/claim',
            {'beacon_id': zero_beacon},
            token=self.auth_tokens['marvin']
        )
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'salvaged')

        # 7. Invariants check
        status, data = self._get('/referee/health')
        self.assertEqual(status, 200)
        self.assertTrue(data['invariants_valid'])
        self.assertEqual(len(data['errors']), 0)


    def test_21_circuit_breaker_endpoints(self):
        # 1. Verify GET /circuit_breaker/bands returns band data
        status, data = self._get('/circuit_breaker/bands')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('bands', data)
        self.assertTrue(len(data['bands']) > 0)
        ceres_fuel = next((b for b in data['bands'] if b['station_id'] == 'ceres' and b['instrument'] == 'FUEL'), None)
        self.assertIsNotNone(ceres_fuel)
        self.assertIn('vwap', ceres_fuel)
        self.assertIn('lower_limit', ceres_fuel)
        self.assertIn('upper_limit', ceres_fuel)
        self.assertGreater(ceres_fuel['upper_limit'], ceres_fuel['lower_limit'])

        # Filtered query
        status, data = self._get('/circuit_breaker/bands?station_id=ceres&instrument=FUEL')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(len(data['bands']), 1)
        self.assertEqual(data['bands'][0]['station_id'], 'ceres')
        self.assertEqual(data['bands'][0]['instrument'], 'FUEL')

        # 2. Verify GET /circuit_breaker/halts initially returns empty list
        status, data = self._get('/circuit_breaker/halts')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIsInstance(data['halts'], list)

        # 3. POST /circuit_breaker/halt without auth fails
        status, data = self._post('/circuit_breaker/halt', {
            'station_id': 'mars',
            'instrument': 'FRAG',
            'reason': 'test_volatility'
        })
        self.assertEqual(status, 401)

        # 4. POST /circuit_breaker/halt with auth triggers halt
        status, data = self._post('/circuit_breaker/halt', {
            'station_id': 'mars',
            'instrument': 'FRAG',
            'reason': 'test_volatility'
        }, token=self.auth_tokens['zero'])
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'halted')
        halt_id = data['halt_id']
        self.assertEqual(data['station_id'], 'mars')
        self.assertEqual(data['instrument'], 'FRAG')

        # 5. GET /circuit_breaker/halts verifies active halt
        status, data = self._get('/circuit_breaker/halts?station_id=mars&instrument=FRAG')
        self.assertEqual(status, 200)
        self.assertTrue(any(h['halt_id'] == halt_id and h['status'] == 'halted' for h in data['halts']))

        # 6. POST /circuit_breaker/reopen reopens the market
        status, data = self._post('/circuit_breaker/reopen', {
            'station_id': 'mars',
            'instrument': 'FRAG'
        }, token=self.auth_tokens['zero'])
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'reopened')
        self.assertIn('clearing_price', data)
        self.assertIn('reopen_volume', data)
        self.assertIn('trades', data)

        # 7. GET /circuit_breaker/halts?status=reopened verifies halt is reopened
        status, data = self._get('/circuit_breaker/halts?station_id=mars&status=reopened')
        self.assertEqual(status, 200)
        self.assertTrue(any(h['halt_id'] == halt_id and h['status'] == 'reopened' for h in data['halts']))

        # 8. Invariants check
        status, data = self._get('/referee/health')
        self.assertEqual(status, 200)
        self.assertTrue(data['invariants_valid'])
        self.assertEqual(len(data['errors']), 0)


if __name__ == '__main__':
    unittest.main()



