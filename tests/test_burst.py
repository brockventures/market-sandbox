"""
tests/test_burst.py - Discrete Burst Engine & Admin Control API (Issue #62).

Covers TickerEngine.start_burst()/cancel_burst() directly, and the
POST /referee/admin/burst, /referee/admin/ticker/pause,
/referee/admin/ticker/resume HTTP endpoints.
"""

import json
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
from typing import Optional

from agora.referee import AgoraReferee
from agora.server import make_handler
from agora.ticker import TickerEngine


class TestBurstEngine(unittest.TestCase):
    def test_burst_advances_exact_round_count(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)  # continuous loop never fires in-test
        start_round = referee.current_round
        result = ticker.start_burst(rounds=3, interval_sec=0.05)
        self.assertIn("burst_id", result)

        deadline = time.time() + 3.0
        while ticker.status()["burst_active"] and time.time() < deadline:
            time.sleep(0.02)

        self.assertFalse(ticker.status()["burst_active"], "burst should have concluded")
        self.assertEqual(referee.current_round, start_round + 3)

    def test_burst_rejects_concurrent_burst(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        ticker.start_burst(rounds=5, interval_sec=0.5)
        with self.assertRaises(ValueError):
            ticker.start_burst(rounds=2, interval_sec=0.05)
        ticker.cancel_burst()

    def test_burst_rejects_invalid_rounds(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        with self.assertRaises(ValueError):
            ticker.start_burst(rounds=0, interval_sec=1.0)
        with self.assertRaises(ValueError):
            ticker.start_burst(rounds=1000, interval_sec=1.0)

    def test_cancel_burst_stops_early(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        start_round = referee.current_round
        ticker.start_burst(rounds=10, interval_sec=0.3)
        time.sleep(0.05)
        cancelled = ticker.cancel_burst()
        self.assertTrue(cancelled)

        deadline = time.time() + 2.0
        while ticker.status()["burst_active"] and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(ticker.status()["burst_active"])
        self.assertLess(referee.current_round, start_round + 10, "cancel mid-burst must stop before all rounds complete")

    def test_cancel_burst_when_none_active_returns_false(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        self.assertFalse(ticker.cancel_burst())

    def test_burst_pauses_and_restores_continuous_ticker(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=100)
        ticker.start()
        try:
            time.sleep(0.12)  # let the continuous ticker tick a round or two
            self.assertFalse(ticker.status()["paused"])

            ticker.start_burst(rounds=2, interval_sec=0.05)
            time.sleep(0.03)
            self.assertTrue(ticker.status()["paused"], "continuous ticker must pause during a burst")

            deadline = time.time() + 3.0
            while ticker.status()["burst_active"] and time.time() < deadline:
                time.sleep(0.02)

            deadline2 = time.time() + 1.0
            while ticker.status()["paused"] and time.time() < deadline2:
                time.sleep(0.02)
            self.assertFalse(ticker.status()["paused"], "continuous ticker must resume after burst concludes")
        finally:
            ticker.stop()

    def test_burst_events_recorded_in_book_events(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        start_seq = referee.current_seq
        ticker.start_burst(rounds=2, interval_sec=0.05)

        deadline = time.time() + 3.0
        while ticker.status()["burst_active"] and time.time() < deadline:
            time.sleep(0.02)

        ticks = referee.get_ticks(since_seq=start_seq)
        burst_kinds = [t["kind"] for t in ticks if t["kind"] == "burst"]
        self.assertGreaterEqual(len(burst_kinds), 4, "expect at least start + 2 ticks + conclude")


class TestBurstAdminEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.referee = AgoraReferee()
        cls.ticker = TickerEngine(cls.referee, interval_sec=999, inactivity_rounds=100)
        cls.auth_tokens = {'amos': 'tok-amos', 'admin': 'tok-admin'}
        handler_class = make_handler(cls.referee, auth_tokens=cls.auth_tokens, ticker=cls.ticker)
        cls.server = HTTPServer(('127.0.0.1', 0), handler_class)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path: str, payload: dict, token: Optional[str] = None):
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(url, data=data_bytes, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def _get(self, path: str, token: Optional[str] = None):
        url = f"{self.base_url}{path}"
        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def test_burst_requires_auth(self):
        status, data = self._post('/referee/admin/burst', {'rounds': 2})
        self.assertEqual(status, 401)

    def test_burst_requires_rounds(self):
        status, data = self._post('/referee/admin/burst', {}, token='tok-amos')
        self.assertEqual(status, 400)
        self.assertEqual(data['payload']['reason'], 'rounds_required')

    def test_burst_start_ok_then_reject_while_active(self):
        status, data = self._post('/referee/admin/burst', {'rounds': 5, 'interval_sec': 1.0}, token='tok-amos')
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'burst_started')
        self.assertEqual(data['payload']['triggered_by'], 'amos')

        status2, data2 = self._post('/referee/admin/burst', {'rounds': 2}, token='tok-amos')
        self.assertEqual(status2, 409)

        self.ticker.cancel_burst()

    def test_ticker_pause_and_resume_require_auth(self):
        status, _ = self._post('/referee/admin/ticker/pause', {}, token=None)
        self.assertEqual(status, 401)
        status2, _ = self._post('/referee/admin/ticker/resume', {}, token=None)
        self.assertEqual(status2, 401)

    def test_ticker_pause_and_resume_ok(self):
        status, data = self._post('/referee/admin/ticker/pause', {}, token='tok-admin')
        self.assertEqual(status, 200)
        self.assertTrue(data['payload']['paused'])

        status2, data2 = self._post('/referee/admin/ticker/resume', {}, token='tok-admin')
        self.assertEqual(status2, 200)
        self.assertFalse(data2['payload']['paused'])


class TestBurstEndpointWithoutTicker(unittest.TestCase):
    def test_burst_rejected_when_ticker_not_configured(self):
        referee = AgoraReferee()
        handler_class = make_handler(referee, auth_tokens={'amos': 'tok-amos'})  # no ticker
        server = HTTPServer(('127.0.0.1', 0), handler_class)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/referee/admin/burst"
            req = urllib.request.Request(
                url,
                data=json.dumps({'rounds': 2}).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer tok-amos'},
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail("expected HTTPError")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 409)
                data = json.loads(e.read().decode('utf-8'))
                self.assertEqual(data['payload']['reason'], 'ticker_not_configured')
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
