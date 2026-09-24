"""
tests/test_ticker.py - Background ticker & inactivity watchdog (Issue #61).
"""

import json
import socket
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from agora.referee import AgoraReferee
from agora.server import make_handler
from agora.ticker import TickerEngine


def _wait_for_server(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", port))
            s.close()
            return
        except OSError:
            time.sleep(0.01)
    raise RuntimeError(f"Server failed to start on port {port} within {timeout}s")


class TestTickerEngine(unittest.TestCase):
    def test_start_advances_rounds_on_schedule(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=100, min_interval_sec=0.01)
        self.assertEqual(referee.current_round, 0)

        ticker.start()
        try:
            deadline = time.time() + 5.0
            while referee.current_round < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(referee.current_round, 2)
        finally:
            ticker.stop()

    def test_status_reports_running_state(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=5.0, inactivity_rounds=100)
        ticker.start()
        try:
            status = ticker.status()
            self.assertTrue(status["running"])
            self.assertFalse(status["paused"])
            self.assertEqual(status["interval_sec"], 5.0)
            self.assertIsNotNone(status["next_tick_eta_sec"])
        finally:
            ticker.stop()

    def test_manual_pause_and_resume(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=100, min_interval_sec=0.01)
        ticker.start()
        try:
            ticker.pause(reason="manual_test")
            # Wait for pause to be reflected in status
            deadline = time.time() + 5.0
            while not ticker.status()["paused"] and time.time() < deadline:
                time.sleep(0.01)
            status = ticker.status()
            self.assertTrue(status["paused"])
            self.assertEqual(status["pause_reason"], "manual_test")
            round_after_pause = referee.current_round

            # Paused ticker must not advance rounds
            time.sleep(0.12)
            self.assertEqual(referee.current_round, round_after_pause, "paused ticker must not advance rounds")

            ticker.resume()
            deadline = time.time() + 5.0
            while referee.current_round <= round_after_pause and time.time() < deadline:
                time.sleep(0.01)
            self.assertGreater(referee.current_round, round_after_pause)
        finally:
            ticker.stop()

    def test_inactivity_watchdog_auto_pauses(self):
        referee = AgoraReferee()
        # 2 consecutive quiet rounds (no seq-advancing activity) should trip the watchdog
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=2, min_interval_sec=0.01)
        ticker.start()
        try:
            deadline = time.time() + 5.0
            while not ticker.status()["paused"] and time.time() < deadline:
                time.sleep(0.01)
            status = ticker.status()
            self.assertTrue(status["paused"])
            self.assertIn("inactivity_watchdog", status["pause_reason"])
            self.assertGreaterEqual(status["quiet_round_count"], 2)
        finally:
            ticker.stop()

    def test_activity_resets_quiet_counter(self):
        referee = AgoraReferee()
        recorded_quiet = []
        ticker = TickerEngine(
            referee,
            interval_sec=0.05,
            inactivity_rounds=5,
            min_interval_sec=0.01,
            on_tick=lambda _: recorded_quiet.append(ticker.status()["quiet_round_count"])
        )
        ticker.start()
        try:
            # Let at least 1 quiet round accumulate
            deadline = time.time() + 5.0
            while referee.current_round < 1 and time.time() < deadline:
                time.sleep(0.01)
            # Simulate order activity advancing the seq counter between ticks
            referee.submit_envelope({
                "v": 1,
                "kind": "order",
                "payload": {
                    "order_id": "ticker-activity-1",
                    "agent_id": "amos",
                    "instrument": "FRAG",
                    "side": "bid",
                    "qty": 1,
                    "limit_price": 1,
                    "seq_seen": referee.current_seq,
                },
            })
            rnd_before = referee.current_round
            deadline = time.time() + 5.0
            while referee.current_round <= rnd_before and time.time() < deadline:
                time.sleep(0.01)
            self.assertIn(0, recorded_quiet, "an order between ticks must reset the quiet-round counter")
        finally:
            ticker.stop()
    def test_bad_round_does_not_kill_ticker_thread(self):
        referee = AgoraReferee()
        original_step_round = referee.step_round
        call_count = {"n": 0}

        def flaky_step_round(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated referee failure")
            return original_step_round(*args, **kwargs)

        referee.step_round = flaky_step_round
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=100, min_interval_sec=0.01)
        ticker.start()
        try:
            deadline = time.time() + 5.0
            while call_count["n"] < 3 and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(call_count["n"], 3, "ticker thread must survive a step_round() exception")
        finally:
            ticker.stop()


class TestTickerStatusEndpoint(unittest.TestCase):
    def test_ticker_status_endpoint_not_configured(self):
        referee = AgoraReferee()
        handler_class = make_handler(referee)  # no ticker passed
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_for_server(port)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/referee/ticker/status")
            resp = conn.getresponse()
            body = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
            self.assertFalse(body["running"])
            self.assertEqual(body["pause_reason"], "ticker_not_configured")
        finally:
            server.shutdown()
            server.server_close()

    def test_ticker_status_endpoint_reports_live_ticker(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=5.0, inactivity_rounds=100)
        ticker.start()
        handler_class = make_handler(referee, ticker=ticker)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_for_server(port)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/referee/ticker/status")
            resp = conn.getresponse()
            body = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
            self.assertTrue(body["running"])
            self.assertFalse(body["paused"])
            self.assertEqual(body["interval_sec"], 5.0)
        finally:
            ticker.stop()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
