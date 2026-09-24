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
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=100, min_interval_sec=0.01)
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
        ticker = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=2, min_interval_sec=0.01)
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
        engine_a = TickerEngine(referee, interval_sec=0.05, inactivity_rounds=100, min_interval_sec=0.01)
        engine_a.start()
        deadline = time.time() + 2.0
        while referee.current_round < 1 and time.time() < deadline:
            time.sleep(0.02)
        engine_a.stop()  # simulates the process dying mid-run without a clean pause
        # Force the persisted state back to 'running' the way a hard kill would
        # (stop() marks it 'stopped', so re-simulate an unclean death):
        referee.set_ticker_state(desired_state='running', quiet_round_count=0)

        engine_b = TickerEngine.resume_from_persisted_state(referee, interval_sec=0.05, inactivity_rounds=100, min_interval_sec=0.01)
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


class TestBootFromPersistedState(unittest.TestCase):
    """The server boot path must always hand back a started engine (#70 rebase)."""

    def test_fresh_db_boots_running(self):
        referee = AgoraReferee()
        engine = TickerEngine.boot_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
        try:
            status = engine.status()
            self.assertTrue(status['running'])
            self.assertFalse(status['paused'])
            self.assertEqual(referee.get_ticker_state()['desired_state'], 'running')
        finally:
            engine.stop()

    def test_persisted_running_boots_running_with_quiet_count(self):
        referee = AgoraReferee()
        referee.set_ticker_state(desired_state='running', quiet_round_count=9)
        engine = TickerEngine.boot_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
        try:
            status = engine.status()
            self.assertTrue(status['running'])
            self.assertFalse(status['paused'])
            self.assertEqual(status['quiet_round_count'], 9)
        finally:
            engine.stop()

    def test_persisted_paused_boots_paused_and_admin_resume_ticks(self):
        """Regression: a paused-then-restarted server must still be revivable by resume()."""
        referee = AgoraReferee()
        referee.set_ticker_state(desired_state='paused', quiet_round_count=4)
        engine = TickerEngine.boot_from_persisted_state(referee, interval_sec=0.05, min_interval_sec=0.01, inactivity_rounds=100)
        try:
            status = engine.status()
            self.assertTrue(status['running'])
            self.assertTrue(status['paused'])
            self.assertTrue(status['pause_reason'].startswith('restored_from_persisted_state:paused'))
            self.assertEqual(referee.get_ticker_state()['desired_state'], 'paused')

            start_round = referee.current_round
            time.sleep(0.2)
            self.assertEqual(referee.current_round, start_round, "paused engine must not tick")

            engine.resume()
            deadline = time.time() + 2.0
            while referee.current_round <= start_round and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreater(referee.current_round, start_round, "resume() after a paused boot must tick")
            self.assertEqual(referee.get_ticker_state()['desired_state'], 'running')
        finally:
            engine.stop()

    def test_persisted_stopped_boots_paused(self):
        referee = AgoraReferee()
        referee.set_ticker_state(desired_state='stopped')
        engine = TickerEngine.boot_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
        try:
            status = engine.status()
            self.assertTrue(status['running'])
            self.assertTrue(status['paused'])
        finally:
            engine.stop()


class TestShutdownDoesNotOverwriteDesiredState(unittest.TestCase):
    def test_stop_without_persist_leaves_running(self):
        referee = AgoraReferee()
        engine = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        engine.start()
        engine.stop(persist=False)
        self.assertEqual(referee.get_ticker_state()['desired_state'], 'running')
        resumed = TickerEngine.resume_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
        try:
            self.assertIsNotNone(resumed)
        finally:
            if resumed:
                resumed.stop()

    def test_stop_without_persist_leaves_paused(self):
        referee = AgoraReferee()
        engine = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        engine.start()
        engine.pause(reason='manual_test')
        engine.stop(persist=False)
        self.assertEqual(referee.get_ticker_state()['desired_state'], 'paused')


class TestBurstPauseIsNotDurable(unittest.TestCase):
    """A burst (#62) pauses the continuous ticker only for the burst's lifetime."""

    def test_burst_from_running_persists_running(self):
        referee = AgoraReferee()
        engine = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        engine.start()
        try:
            engine.start_burst(rounds=5, interval_sec=500)
            self.assertTrue(engine.status()['paused'])
            self.assertEqual(referee.get_ticker_state()['desired_state'], 'running')
            # A restart mid-burst comes back running.
            resumed = TickerEngine.resume_from_persisted_state(referee, interval_sec=999, inactivity_rounds=100)
            try:
                self.assertIsNotNone(resumed)
                self.assertFalse(resumed.status()['paused'])
            finally:
                if resumed:
                    resumed.stop(persist=False)
        finally:
            engine.cancel_burst(force=True)
            engine.stop()

    def test_burst_from_paused_persists_paused(self):
        referee = AgoraReferee()
        engine = TickerEngine(referee, interval_sec=999, inactivity_rounds=100)
        engine.start()
        try:
            engine.pause(reason='manual_test')
            engine.start_burst(rounds=5, interval_sec=500)
            self.assertEqual(referee.get_ticker_state()['desired_state'], 'paused')
        finally:
            engine.cancel_burst(force=True)
            engine.stop()


if __name__ == "__main__":
    unittest.main()
