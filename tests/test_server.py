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

from agora.referee import AgoraReferee
from agora.server import make_handler


class TestAgoraServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.referee = AgoraReferee()
        handler_class = make_handler(cls.referee)
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

    def _get(self, path: str):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return resp.status, data

    def _post(self, path: str, payload: dict):
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
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

    def test_03_accounts_query(self):
        # All accounts
        status, data = self._get('/referee/accounts')
        self.assertEqual(status, 200)
        self.assertTrue(len(data['accounts']) >= 6)

        # Filtered by agent_id
        status, data = self._get('/referee/accounts?agent_id=amos')
        self.assertEqual(status, 200)
        instruments = {a['instrument']: a['balance'] for a in data['accounts']}
        self.assertEqual(instruments.get('CREDITS'), 10000)
        self.assertEqual(instruments.get('BANANA'), 1000)

    def test_04_submit_order_and_fill(self):
        # 1. Amos posts ask: Sell 40 BANANA @ 15
        ask_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'http-ask-001', 'agent_id': 'amos', 'instrument': 'BANANA',
                'side': 'ask', 'qty': 40, 'limit_price': 15, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', ask_env)
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'market_tick')
        self.assertEqual(data['payload']['best_ask'], 15)

        # Check book shows resting ask
        _, book_data = self._get('/referee/book')
        self.assertEqual(len(book_data['book']['asks']), 1)
        self.assertEqual(book_data['book']['asks'][0]['order_id'], 'http-ask-001')

        # 2. Zero crosses ask: Buy 40 BANANA @ 15
        bid_env = {
            'v': 1, 'kind': 'order',
            'payload': {
                'order_id': 'http-bid-001', 'agent_id': 'zero', 'instrument': 'BANANA',
                'side': 'bid', 'qty': 40, 'limit_price': 15, 'seq_seen': self.referee.current_seq
            }
        }
        status, data = self._post('/referee/orders', bid_env)
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'market_tick')
        self.assertEqual(data['payload']['trades_count'], 1)

        # Check balances updated
        _, acct_zero = self._get('/referee/accounts?agent_id=zero')
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
        status, data = self._post('/referee/orders', insolvent_env)
        self.assertEqual(status, 400)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'insufficient_balance')

    def test_07_not_found(self):
        status, data = self._post('/referee/unknown', {})
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
