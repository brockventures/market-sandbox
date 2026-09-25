import unittest
from agora.referee import AgoraReferee
from agora.spatial import StationSpotPrice


class TestHistoryRollbackAtomicity(unittest.TestCase):
    def test_trade_settlement_failure_rolls_back_orders_and_history(self):
        """Verify that an exception raised after record_trade rolls back both orders and history."""
        ref = AgoraReferee(depots=False)

        # Baseline: get initial volume for earth/FRAG at round 0
        cur = ref.conn.cursor()
        cur.execute("SELECT volume FROM price_history WHERE station_id = 'earth' AND instrument = 'FRAG' AND round = 0")
        initial_vol = cur.fetchone()["volume"]
        self.assertEqual(initial_vol, 0)

        # Simulate trade transaction with simulated failure after record_trade
        with self.assertRaises(RuntimeError):
            with ref.conn:
                ref.conn.execute("""
                    INSERT INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status)
                    VALUES ('ord-atomic-1', 'zero', 'FRAG', 'bid', 10, 20, 1, 'filled')
                """)
                ref.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload)
                    VALUES (1, 'order', '{"test": 1}')
                """)
                # Record trade in history engine
                ref.history_engine.record_trade("earth", "FRAG", 20.0, 10, round_num=0)

                # Simulated error later in transaction (e.g. currency mismatch or account transfer failure)
                raise RuntimeError("Simulated mid-settlement currency mismatch")

        # Verify full rollback: neither the order, book_event, nor the history record should persist
        cur.execute("SELECT COUNT(*) AS c FROM orders WHERE order_id = 'ord-atomic-1'")
        self.assertEqual(cur.fetchone()["c"], 0, "Order was not rolled back!")

        cur.execute("SELECT COUNT(*) AS c FROM book_events WHERE seq = 1")
        self.assertEqual(cur.fetchone()["c"], 0, "Book event was not rolled back!")

        cur.execute("SELECT volume FROM price_history WHERE station_id = 'earth' AND instrument = 'FRAG' AND round = 0")
        self.assertEqual(cur.fetchone()["volume"], 0, "Price history trade volume was committed despite exception!")

    def test_step_round_failure_rolls_back_round_start_and_station_prices(self):
        """Verify that an exception raised during step_round rolls back round start price_history."""
        ref = AgoraReferee(depots=False)
        test_round = 99

        spot_prices = [
            StationSpotPrice(station_id="earth", commodity="FRAG", round=test_round, base_price=20.0, drift_bias=0.0, spot_price=22.5)
        ]

        with self.assertRaises(RuntimeError):
            with ref.conn:
                for p in spot_prices:
                    ref.conn.execute("""
                        INSERT OR REPLACE INTO station_prices (station_id, commodity, round, base_price, drift_bias, spot_price, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    """, (p.station_id, p.commodity, p.round, p.base_price, p.drift_bias, p.spot_price))

                ref.history_engine.record_round_start(test_round, spot_prices)
                # Failure before transaction completes (e.g. in piracy or arrival settlement)
                raise RuntimeError("Simulated piracy crash during round step")

        cur = ref.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM station_prices WHERE round = ?", (test_round,))
        self.assertEqual(cur.fetchone()["c"], 0, "Station prices were not rolled back!")

        cur.execute("SELECT COUNT(*) AS c FROM price_history WHERE round = ?", (test_round,))
        self.assertEqual(cur.fetchone()["c"], 0, "Round start price history was not rolled back!")


if __name__ == "__main__":
    unittest.main()
