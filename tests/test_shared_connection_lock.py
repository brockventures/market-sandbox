"""
tests/test_shared_connection_lock.py

#197: the referee's one sqlite connection is shared by every request thread,
so every use of it must hold ref.lock, and a failed cancel must not orphan
the order. tools/race_fuzz.py is the at-size check; these pin the pieces.
"""

import threading
import unittest

from agora.referee import AgoraReferee


def _place(ref, order_id='rest-1', agent='amos'):
    return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': {
        'order_id': order_id, 'agent_id': agent, 'side': 'ask', 'qty': 5,
        'limit_price': 40, 'instrument': 'FRAG', 'station_id': 'ceres',
        'seq_seen': ref.current_seq}})


def _resting(ref, order_id):
    return any(o.order_id == order_id
               for st in ref.books.values() for b in st.values() for o in (*b.bids, *b.asks))


class TestSharedConnectionLock(unittest.TestCase):
    def test_lock_is_reentrant(self):
        ref = AgoraReferee()
        with ref.lock:
            # Self-locking helpers must work from inside a locked writer.
            self.assertIsInstance(ref.current_seq, int)
            ref.get_vessel_location('amos')
            ref.fleet_out('amos')

    def test_failed_cancel_write_leaves_order_resting(self):
        ref = AgoraReferee(depots=True)
        self.assertEqual(_place(ref).get('kind'), 'market_tick')
        self.assertTrue(_resting(ref, 'rest-1'))
        ref.conn.execute("""CREATE TRIGGER fail_cancel BEFORE INSERT ON book_events
                            WHEN NEW.kind = 'cancel' BEGIN SELECT RAISE(ABORT, 'boom'); END""")
        with self.assertRaises(Exception):
            ref.cancel_order('amos', 'rest-1')
        # The write failed, so the order must still be where the DB says it is.
        self.assertTrue(_resting(ref, 'rest-1'), 'order dropped from the book by a failed cancel')
        status = ref.conn.execute("SELECT status FROM orders WHERE order_id = 'rest-1'").fetchone()[0]
        self.assertEqual(status, 'open')

        ref.conn.execute("DROP TRIGGER fail_cancel")
        self.assertEqual(ref.cancel_order('amos', 'rest-1').get('status'), 'cancelled')
        self.assertFalse(_resting(ref, 'rest-1'))

    def test_admin_fleet_upsert_waits_for_the_lock(self):
        ref = AgoraReferee()
        done = threading.Event()
        with ref.lock:
            th = threading.Thread(target=lambda: (ref.upsert_fleet_roster(
                'newcorp', 'New Corp', 'ceres', 1000, 10, 10), done.set()), daemon=True)
            th.start()
            # Must not run (and commit) inside another thread's locked section.
            self.assertFalse(done.wait(0.3))
        th.join(5)
        self.assertTrue(done.is_set())
        row = ref.conn.execute("SELECT display_name FROM fleet_roster WHERE agent_id = 'newcorp'").fetchone()
        self.assertEqual(row[0], 'New Corp')


if __name__ == '__main__':
    unittest.main()
