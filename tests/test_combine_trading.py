"""
tests/test_combine_trading.py - Unit & integration tests for Agora Combine trading enhancements.
Verifies:
- Universal combine token (agora-combine-2026) allows placing orders on behalf of any syndicate
- POST /referee/quick_order accepts flat simplified trade submissions
- GET /referee/leaderboard exposes food and ore balances
"""

import json
import threading
import unittest
from http.server import HTTPServer
import urllib.request
import urllib.error

from agora.referee import AgoraReferee
from agora.server import make_handler


class TestCombineTrading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.referee = AgoraReferee(depots=True)
        handler_cls = make_handler(
            cls.referee,
            auth_tokens={
                'admin': 'test-admin-secret',
                'zero': 'test-zero-token',
                'combine': 'agora-combine-2026'
            }
        )
        cls.server = HTTPServer(('127.0.0.1', 0), handler_cls)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path: str, payload: dict, token: str = "") -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f"Bearer {token}"
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8')
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {'raw': raw}

    def _get(self, path: str, token: str = "") -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {}
        if token:
            headers['Authorization'] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8')
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {'raw': raw}

    def test_combine_token_places_order_for_any_agent(self):
        """Universal combine token can submit orders for amos, marvin, zero."""
        for agent in ('amos', 'marvin', 'zero'):
            order = {
                'v': 1,
                'kind': 'order',
                'payload': {
                    'agent_id': agent,
                    'side': 'bid',
                    'qty': 5,
                    'limit_price': 10,
                    'order_id': f"combine-test-{agent}-1",
                    'instrument': 'FRAG',
                    'station_id': 'ceres'
                }
            }
            status, res = self._post('/referee/orders', order, token='agora-combine-2026')
            self.assertEqual(status, 200, f"Failed for agent {agent}: {res}")
            self.assertEqual(res.get('kind'), 'market_tick')

    def test_quick_order_endpoint(self):
        """POST /referee/quick_order accepts flat simplified order."""
        quick_order = {
            'agent_id': 'amos',
            'side': 'buy',
            'qty': 10,
            'limit_price': 25,
            'instrument': 'FOOD',
            'station_id': 'ceres'
        }
        status, res = self._post('/referee/quick_order', quick_order, token='agora-combine-2026')
        self.assertEqual(status, 200, f"Quick order failed: {res}")
        self.assertEqual(res.get('kind'), 'market_tick')

    def test_leaderboard_exposes_food_and_ore(self):
        """GET /referee/leaderboard exposes food and ore asset breakdown."""
        status, data = self._get('/referee/leaderboard')
        self.assertEqual(status, 200)
        lb = data.get('leaderboard', [])
        self.assertTrue(len(lb) >= 3)
        for entry in lb:
            self.assertIn('liquid', entry)
            self.assertIn('frags', entry)
            self.assertIn('fuel', entry)
            self.assertIn('food', entry)
            self.assertIn('ore', entry)


if __name__ == '__main__':
    unittest.main()
