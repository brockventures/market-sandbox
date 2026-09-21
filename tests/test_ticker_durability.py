"""
tests/test_ticker_durability.py - Durable desired-state & resume-on-boot
for TickerEngine (Issue #63 follow-up).

In-memory ticker state (`_running = True` in a Python attribute) is lost
on any process restart. These tests cover the fix: desired_state,
quiet_round_count, and a lease are persisted to the referee's own SQLite
database (`ticker_state` table) so a fresh TickerEngine constructed
against the same referee/db can reconcile on boot instead of coming back
cold and silent.
"""

import time
import unittest

from agora.referee import AgoraReferee
from agora.ticker import TickerEngine


class TestTickerStatePersistence(unittest.TestCase):
    def test_get_ticker_state_none_on_fresh_db(self):
        referee = AgoraReferee()
        self.assertIsNone(referee.get_ticker_state())

    def test_set_and_get_ticker_state_roundtrip(self):
        referee = AgoraReferee()
        referee.set_ticker_state(desired_state='running', quiet_round_count=3, last_tick_at='2026-09-21T00:00:00.000Z')
        state = referee.get_ticker_state()
        self.assertEqual(state['desired_state'], 'running')
        self.assertEqual(state['quiet_round_count'], 3)
        self.assertEqual(state['last_tick_at'], '2026-09-21T00:00:00.000Z')

    def test_set_ticker_state_upserts_not_duplicates(self):
        referee = AgoraReferee()
        referee.set_ticker_state(desired_state='running', quiet_round_count=1)
        referee.set_ticker_state(desired_state='paused', quiet_round_count=5)
        state = referee.get_ticker_state()
        self.assertEqual(state['desired_state'], 'paused')
        self.assertEqual(state['quiet_round_count'], 5)
        row_count = referee.conn.execute("SELECT COUNT(*) FROM ticker_state").fetchone()[0]
        self.assertEqual(row_count, 1)

    def test_set_ticker_state_rejects_invalid_value(self):
        referee = AgoraReferee()
        with self.assertRaises(ValueError):
            referee.set_ticker_state(desired_state='sprinting')


class TestTickerEnginePersistsOnLifecycle(unittest.TestCase):
    def test_start_persists_running(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        ticker.start()
        try:
            state = referee.get_ticker_state()
            self.assertEqual(state['desired_state'], 'running')
            self.assertIsNotNone(state['lease_owner'])
            self.assertIsNotNone(state['lease_expires_at'])
        finally:
            ticker.stop()

    def test_pause_persists_paused_and_clears_lease(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        ticker.start()
        try:
            ticker.pause(reason='manual_test')
            state = referee.get_ticker_state()
            self.assertEqual(state['desired_state'], 'paused')
            self.assertIsNone(state['lease_owner'])
        finally:
            ticker.stop()

    def test_stop_persists_stopped(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        ticker.start()
        ticker.stop()
        state = referee.get_ticker_state()
        self.assertEqual(state['desired_state'], 'stopped')

    def test_tick_updates_quiet_round_count_in_db(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=100)
        ticker.start()
        try:
            deadline = time.time() + 2.0
            while referee.current_round < 2 and time.time() < deadline:
                time.sleep(0.02)
            state = referee.get_ticker_state()
            self.assertEqual(state['desired_state'], 'running')
            self.assertGreaterEqual(state['quiet_round_count'], 0)
        finally:
            ticker.stop()

    def test_watchdog_pause_persists_paused_not_running(self):
        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=2)
        ticker.start()
        try:
            deadline = time.time() + 3.0
            while not ticker.status()['paused'] and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(ticker.status()['paused'])
            state = referee.get_ticker_state()
            self.assertEqual(state['desired_state'], 'paused')
        finally:
            ticker.stop()


class TestResumeFromPersistedState(unittest.TestCase):
    def test_no_persisted_state_returns_none(self):
        referee = AgoraReferee()
        result = TickerEngine.resume_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
        self.assertIsNone(result)

    def test_persisted_paused_returns_none(self):
        referee = AgoraReferee()
        referee.set_ticker_state(desired_state='paused', quiet_round_count=4)
        result = TickerEngine.resume_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
        self.assertIsNone(result)

    def test_persisted_stopped_returns_none(self):
        referee = AgoraReferee()
        referee.set_ticker_state(desired_state='stopped')
        result = TickerEngine.resume_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
        self.assertIsNone(result)

    def test_persisted_running_auto_starts_and_restores_quiet_count(self):
        referee = AgoraReferee()
        referee.set_ticker_state(desired_state='running', quiet_round_count=7)
        engine = TickerEngine.resume_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
        try:
            self.assertIsNotNone(engine)
            status = engine.status()
            self.assertTrue(status['running'])
            self.assertFalse(status['paused'])
            self.assertEqual(status['quiet_round_count'], 7)
        finally:
            if engine:
                engine.stop()

    def test_simulated_restart_resumes_ticking(self):
        """A whole 'process A ticks, dies, process B reconciles from the same db' cycle."""
        referee = AgoraReferee()
        engine_a = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=100)
        engine_a.start()
        deadline = time.time() + 2.0
        while referee.current_round < 1 and time.time() < deadline:
            time.sleep(0.02)
        engine_a.stop()  # simulates the process dying mid-run without a clean pause
        # Force the persisted state back to 'running' the way a hard kill would
        # (stop() marks it 'stopped', so re-simulate an unclean death):
        referee.set_ticker_state(desired_state='running', quiet_round_count=0)

        engine_b = TickerEngine.resume_from_persisted_state(referee, interval_sec=0.05, inactivity_rounds=100)
        try:
            self.assertIsNotNone(engine_b)
            start_round = referee.current_round
            deadline2 = time.time() + 2.0
            while referee.current_round <= start_round and time.time() < deadline2:
                time.sleep(0.02)
            self.assertGreater(referee.current_round, start_round, "resumed engine must keep ticking")
        finally:
            if engine_b:
                engine_b.stop()


if __name__ == "__main__":
    unittest.main()
