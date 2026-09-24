import json
import pytest
from agora.referee import AgoraReferee
from agora.history import PriceHistoryEngine
from agora.briefing import build_briefing
from agora.spatial import STATIONS, COMMODITIES


def test_genesis_price_history():
    ref = AgoraReferee(depots=True, exchange_shares=100)
    # Check that price history has round 0 entries for earth
    hist = ref.get_price_history(station_id="earth", instrument="FRAG", rounds=5)
    assert len(hist) >= 1
    candle = hist[0]
    assert candle["round"] == 0
    assert candle["open"] > 0
    assert candle["high"] == candle["open"]
    assert candle["low"] == candle["open"]
    assert candle["close"] == candle["open"]
    assert candle["volume"] == 0


def test_stock_price_history():
    ref = AgoraReferee(depots=True, exchange_shares=100)
    # Stocks are on Ceres exchange
    hist = ref.get_price_history(station_id="ceres", instrument="EQ_AMOS", rounds=5)
    assert len(hist) >= 1
    candle = hist[0]
    assert candle["round"] == 0
    assert candle["open"] > 0
    assert candle["volume"] == 0


def test_trade_updates_ohlcv():
    ref = AgoraReferee(depots=True)
    # Initial candle at round 0
    ref.history_engine.record_trade("earth", "FRAG", 20.0, 50, round_num=0)
    ref.history_engine.record_trade("earth", "FRAG", 25.0, 30, round_num=0)
    ref.history_engine.record_trade("earth", "FRAG", 18.0, 20, round_num=0)

    hist = ref.get_price_history(station_id="earth", instrument="FRAG", rounds=5)
    candle = [h for h in hist if h["round"] == 0][0]
    assert candle["high"] >= 25.0
    assert candle["low"] <= 18.0
    assert candle["close"] == 18.0
    assert candle["volume"] >= 100


def test_round_stepping_candles():
    ref = AgoraReferee(depots=True)
    ref.step_round()  # Round 1
    ref.step_round()  # Round 2

    hist = ref.get_price_history(station_id="mars", instrument="ORE", rounds=10)
    rounds = [h["round"] for h in hist]
    assert 0 in rounds
    assert 1 in rounds
    assert 2 in rounds


def test_fog_of_war_price_history():
    ref = AgoraReferee(depots=True, fog=True, asymmetric=True)
    # amos is docked at mars, zero is docked at earth
    # Step a few rounds so lag window has data
    for _ in range(5):
        ref.step_round()

    # Exact for docked station
    hist_docked = ref.get_price_history(station_id="earth", instrument="FRAG", rounds=10, viewer="zero")
    assert any(h["round"] == ref.current_round for h in hist_docked)
    assert hist_docked[-1]["volume"] is not None

    # Remote station is lagged and has hidden volume (None)
    hist_remote = ref.get_price_history(station_id="mars", instrument="ORE", rounds=10, viewer="zero")
    # Latest round in remote view cannot exceed current_round - lag
    assert all(h["round"] <= ref.current_round - ref.fog.lag for h in hist_remote)
    assert all(h["volume"] is None for h in hist_remote)

    # Admin sees exact everywhere
    hist_admin = ref.get_price_history(station_id="mars", instrument="ORE", rounds=10, viewer="admin")
    assert any(h["round"] == ref.current_round for h in hist_admin)
    assert hist_admin[-1]["volume"] is not None

    # Stock is public exchange, exact everywhere
    hist_stock = ref.get_price_history(station_id="ceres", instrument="EQ_AMOS", rounds=10, viewer="zero")
    assert any(h["round"] == ref.current_round for h in hist_stock)


def test_briefing_table_integration():
    ref = AgoraReferee(depots=True)
    ref.step_round()
    briefing = build_briefing(ref, viewer="zero")
    assert "## Recent price history" in briefing
    assert "Full OHLCV history: `GET /referee/history" in briefing


import threading
import urllib.request
import urllib.error
from http.server import HTTPServer
from agora.server import make_handler

class TestHistoryServerEndpoints:
    @classmethod
    def setup_class(cls):
        cls.ref = AgoraReferee(depots=True, exchange_shares=100)
        cls.auth_tokens = {'zero': 'tok-zero', 'admin': 'tok-admin'}
        handler = make_handler(cls.ref, auth_tokens=cls.auth_tokens)
        cls.server = HTTPServer(('127.0.0.1', 0), handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def teardown_class(cls):
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
        assert status == 200
        assert body['status'] == 'ok'
        assert body['station_id'] == 'earth'
        assert body['instrument'] == 'FRAG'
        assert len(body['history']) >= 1

    def test_get_history_stock_success(self):
        status, body = self._get('/referee/history?instrument=EQ_AMOS&rounds=5')
        assert status == 200
        assert body['status'] == 'ok'
        assert body['station_id'] == 'ceres'
        assert body['instrument'] == 'EQ_AMOS'
        assert len(body['history']) >= 1

    def test_get_history_missing_instrument(self):
        status, body = self._get('/referee/history?station_id=earth')
        assert status == 400
        assert body['payload']['reason'] == 'missing_parameter'

    def test_get_history_missing_station_for_commodity(self):
        status, body = self._get('/referee/history?instrument=FRAG')
        assert status == 400
        assert body['payload']['reason'] == 'missing_parameter'

    def test_get_history_invalid_station(self):
        status, body = self._get('/referee/history?station_id=atlantis&instrument=FRAG')
        assert status == 400
        assert body['payload']['reason'] == 'invalid_station'
