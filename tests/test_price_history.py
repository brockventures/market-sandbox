import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer

from agora.referee import AgoraReferee
from agora.history import PriceHistoryEngine
from agora.briefing import build_briefing
from agora.server import make_handler
from agora.spatial import STATIONS, COMMODITIES


class TestPriceHistory(unittest.TestCase):
    def test_genesis_price_history(self):
        ref = AgoraReferee(depots=True, exchange_shares=100)
        hist = ref.get_price_history(station_id="earth", instrument="FRAG", rounds=5)
        self.assertGreaterEqual(len(hist), 1)
        candle = hist[0]
        self.assertEqual(candle["round"], 0)
        self.assertGreater(candle["open"], 0)
        self.assertEqual(candle["high"], candle["open"])
        self.assertEqual(candle["low"], candle["open"])
        self.assertEqual(candle["close"], candle["open"])
        self.assertEqual(candle["volume"], 0)

    def test_stock_price_history(self):
        ref = AgoraReferee(depots=True, exchange_shares=100)
        hist = ref.get_price_history(station_id="ceres", instrument="EQ_AMOS", rounds=5)
        self.assertGreaterEqual(len(hist), 1)
        candle = hist[0]
        self.assertEqual(candle["round"], 0)
        self.assertGreater(candle["open"], 0)
        self.assertEqual(candle["volume"], 0)

    def test_trade_updates_ohlcv(self):
        ref = AgoraReferee(depots=True)
        ref.history_engine.record_trade("earth", "FRAG", 20.0, 50, round_num=0)
        ref.history_engine.record_trade("earth", "FRAG", 25.0, 30, round_num=0)
        ref.history_engine.record_trade("earth", "FRAG", 18.0, 20, round_num=0)

        hist = ref.get_price_history(station_id="earth", instrument="FRAG", rounds=5)
        candle = [h for h in hist if h["round"] == 0][0]
        self.assertGreaterEqual(candle["high"], 25.0)
        self.assertLessEqual(candle["low"], 18.0)
        self.assertEqual(candle["close"], 18.0)
        self.assertGreaterEqual(candle["volume"], 100)

    def test_round_stepping_candles(self):
        ref = AgoraReferee(depots=True)
        ref.step_round()
        ref.step_round()

        hist = ref.get_price_history(station_id="mars", instrument="ORE", rounds=10)
        rounds = [h["round"] for h in hist]
        self.assertIn(0, rounds)
        self.assertIn(1, rounds)
        self.assertIn(2, rounds)

    def test_fog_of_war_price_history(self):
        ref = AgoraReferee(depots=True, fog=True, asymmetric=True)
        for _ in range(5):
            ref.step_round()

        # Exact for docked station
        hist_docked = ref.get_price_history(station_id="earth", instrument="FRAG", rounds=10, viewer="zero")
        self.assertTrue(any(h["round"] == ref.current_round for h in hist_docked))
        self.assertIsNotNone(hist_docked[-1]["volume"])

        # Remote station is lagged and has hidden volume (None)
        hist_remote = ref.get_price_history(station_id="mars", instrument="ORE", rounds=10, viewer="zero")
        self.assertTrue(all(h["round"] <= ref.current_round - ref.fog.lag for h in hist_remote))
        self.assertTrue(all(h["volume"] is None for h in hist_remote))

        # Admin sees exact everywhere
        hist_admin = ref.get_price_history(station_id="mars", instrument="ORE", rounds=10, viewer="admin")
        self.assertTrue(any(h["round"] == ref.current_round for h in hist_admin))
        self.assertIsNotNone(hist_admin[-1]["volume"])

        # Stock is public exchange, exact everywhere
        hist_stock = ref.get_price_history(station_id="ceres", instrument="EQ_AMOS", rounds=10, viewer="zero")
        self.assertTrue(any(h["round"] == ref.current_round for h in hist_stock))

    def test_briefing_table_integration(self):
        ref = AgoraReferee(depots=True)
        ref.step_round()
        briefing = build_briefing(ref, viewer="zero")
        self.assertIn("## Recent price history", briefing)
        self.assertIn("Full OHLCV history: `GET /referee/history", briefing)


class TestHistoryServerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = AgoraReferee(depots=True, exchange_shares=100)
        cls.auth_tokens = {'zero': 'tok-zero', 'admin': 'tok-admin'}
        handler = make_handler(cls.ref, auth_tokens=cls.auth_tokens)
        cls.server = HTTPServer(('127.0.0.1', 0), handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path, token=None):
        url = f'http://127.0.0.1:{self.port}{path}'
        req = urllib.request.Request(url)
        if token:
            req.add_header('Authorization', f'Bearer {token}')
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_get_history_commodity_success(self):
        status, body = self._get('/referee/history?station_id=earth&instrument=FRAG&rounds=5')
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['station_id'], 'earth')
        self.assertEqual(body['instrument'], 'FRAG')
        self.assertGreaterEqual(len(body['history']), 1)

    def test_get_history_stock_success(self):
        status, body = self._get('/referee/history?instrument=EQ_AMOS&rounds=5')
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['station_id'], 'ceres')
        self.assertEqual(body['instrument'], 'EQ_AMOS')
        self.assertGreaterEqual(len(body['history']), 1)

    def test_get_history_missing_instrument(self):
        status, body = self._get('/referee/history?station_id=earth')
        self.assertEqual(status, 400)
        self.assertEqual(body['payload']['reason'], 'missing_parameter')

    def test_get_history_missing_station_for_commodity(self):
        status, body = self._get('/referee/history?instrument=FRAG')
        self.assertEqual(status, 400)
        self.assertEqual(body['payload']['reason'], 'missing_parameter')

    def test_get_history_invalid_station(self):
        status, body = self._get('/referee/history?station_id=atlantis&instrument=FRAG')
        self.assertEqual(status, 400)
        self.assertEqual(body['payload']['reason'], 'invalid_station')


if __name__ == '__main__':
    unittest.main()
