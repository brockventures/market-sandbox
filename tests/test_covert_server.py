"""
tests/test_covert_server.py - HTTP integration tests for Covert Ops & Rivalry routes.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from agora.server import make_handler
from agora.referee import AgoraReferee


class TestCovertServerHTTP(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(events=True, corporate=True, piracy='0.3,0.1')
        self.server = HTTPServer(('127.0.0.1', 0), make_handler(
            self.ref, auth_tokens={'amos': 'ta', 'zero': 'tz', 'marvin': 'tm', 'combine': 'tc'}))
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, body, tok=None):
        headers = {'Content-Type': 'application/json'}
        if tok:
            headers['Authorization'] = f'Bearer {tok}'
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method='POST',
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path, tok=None):
        headers = {'Authorization': f'Bearer {tok}'} if tok else {}
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_covert_http_wiretap_and_intel(self):
        # 1. Wiretap without token -> 401
        code, r = self._post('/referee/covert/wiretap', {'target': 'amos'})
        self.assertEqual(code, 401)

        # 2. Plant wiretap over HTTP -> 200
        code, r = self._post('/referee/covert/wiretap', {'target': 'amos'}, 'tz')
        self.assertEqual(code, 200, r)
        self.assertEqual(r['kind'], 'wiretap_ok')
        self.assertEqual(r['payload']['target'], 'amos')

        # 3. List active wiretaps over HTTP
        code, r = self._get('/referee/covert/wiretaps', 'tz')
        self.assertEqual(code, 200)
        self.assertEqual(len(r['wiretaps']), 1)
        self.assertEqual(r['wiretaps'][0]['target'], 'amos')

        # 4. Intel on tapped target over HTTP
        code, r = self._get('/referee/covert/intel?target=amos', 'tz')
        self.assertEqual(code, 200, r)
        self.assertEqual(r['kind'], 'intel_ok')
        self.assertIn('cargo', r['payload'])

        # 5. Untapped intel rejected
        code, r = self._get('/referee/covert/intel?target=marvin', 'tz')
        self.assertEqual(code, 400)
        self.assertEqual(r['payload']['reason'], 'no_wiretap')

    def test_covert_http_sabotage_and_rivalry_scoreboard(self):
        # Force trace off for this test to test unknown attacker on scoreboard
        self.ref.covert.bags.force('sabotage_trace', False)

        # Sabotage over HTTP -> 200
        code, r = self._post('/referee/covert/sabotage', {'target': 'amos', 'mode': 'docked'}, 'tz')
        self.assertEqual(code, 200, r)
        self.assertEqual(r['kind'], 'sabotage_ok')
        self.assertFalse(r['payload']['traced'])

        # Rivalry scoreboard over HTTP from victim's perspective (Amos)
        code, r = self._get('/referee/corporate/rivalry', 'ta')
        self.assertEqual(code, 200)
        self.assertIn('rivalries', r)
        # Victim Amos sees an attack from 'unknown'
        victims_board = [entry for entry in r['rivalries'] if entry['victim'] == 'amos']
        self.assertGreater(len(victims_board), 0)
        self.assertEqual(victims_board[0]['aggressor'], 'unknown')
        self.assertGreater(r['hostility']['amos']['suffered'], 0)


if __name__ == '__main__':
    unittest.main()
