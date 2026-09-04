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
        self.assertEqual(instruments.get('CREDITS'), 10000)
        self.assertEqual(instruments.get('BANANA'), 1000)

        # 4. Cross-agent inspection blocked with 403
        status, data = self._get('/referee/accounts?agent_id=marvin', token='tok-amos')
        self.assertEqual(status, 403)
        self.assertEqual(data['payload']['reason'], 'unauthorized')

    def test_04_submit_order_and_fill(self):
        # 1. Amos posts ask with tok-amos: Sell 40 BANANA @ 15
        ask_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'http-ask-001', 'agent_id': 'amos', 'instrument': 'BANANA',
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

        # 2. Zero crosses ask with tok-zero: Buy 40 BANANA @ 15
        bid_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'http-bid-001', 'agent_id': 'zero', 'instrument': 'BANANA',
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
        self.assertEqual(zero_map['CREDITS'], 10000 - 600)  # 40 * 15 = 600
        self.assertEqual(zero_map['BANANA'], 1000 + 40)

    def test_05_ticks_endpoint(self):
        status, data = self._get('/referee/ticks?since_seq=0')
        self.assertEqual(status, 200)
        self.assertTrue(data['current_seq'] > 0)
        self.assertTrue(len(data['ticks']) >= 2)

    def test_06_reject_insolvent_order(self):
        insolvent_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'http-insolvent', 'agent_id': 'marvin', 'instrument': 'BANANA',
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
                'order_id': 'attacker-forged-1', 'agent_id': 'amos', 'instrument': 'BANANA',
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
                'order_id': 'cross-agent-forge-1', 'agent_id': 'amos', 'instrument': 'BANANA',
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
                'order_id': 'bad-tok-order', 'agent_id': 'zero', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 10, 'limit_price': 10, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', env, token='wrong-garbage-token')
        self.assertEqual(status, 401)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'unauthorized')


if __name__ == '__main__':
    unittest.main()

