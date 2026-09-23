import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import json

from tools import trader_client


class TestTraderClient(unittest.TestCase):
    @patch("tools.trader_client.request")
    def test_check_health(self, mock_req):
        mock_req.return_value = {"status": "ok", "seq": 42}
        res = trader_client.check_health()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["seq"], 42)
        mock_req.assert_called_with("/referee/health")

    @patch("tools.trader_client.request")
    def test_get_ticker_status(self, mock_req):
        mock_req.return_value = {"status": "ok", "running": True, "current_round": 3}
        res = trader_client.get_ticker_status()
        self.assertEqual(res["current_round"], 3)
        mock_req.assert_called_with("/referee/ticker/status")

    def test_load_strategy_config(self):
        cfg = trader_client.load_strategy_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn("station_id", cfg)
        self.assertIn("target_bid", cfg)
        self.assertIn("target_ask", cfg)
        self.assertIn("clip_size", cfg)

    @patch("tools.trader_client.check_health")
    @patch("tools.trader_client.request")
    def test_submit_order(self, mock_req, mock_health):
        mock_health.return_value = {"status": "ok", "seq": 100}
        mock_req.return_value = {"kind": "order", "status": "accepted"}
        res = trader_client.submit_order("bid", qty=5, limit_price=15, instrument="FRAG")
        self.assertEqual(res["status"], "accepted")
        mock_req.assert_called_once()
        args, kwargs = mock_req.call_args
        self.assertEqual(args[0], "/referee/orders")
        payload = args[1]["payload"]
        self.assertEqual(payload["side"], "bid")
        self.assertEqual(payload["qty"], 5)
        self.assertEqual(payload["limit_price"], 15)
        self.assertEqual(payload["seq_seen"], 100)

    @patch("tools.trader_client.request")
    def test_cancel_all(self, mock_req):
        mock_req.return_value = {"status": "cancelled_all", "payload": {"count": 3}}
        res = trader_client.cancel_all()
        self.assertEqual(res["status"], "cancelled_all")
        mock_req.assert_called_with("/referee/orders/cancel_all", {"agent_id": trader_client.AGENT_ID})

    @patch("tools.trader_client.get_stations_locations")
    @patch("tools.trader_client.cancel_all")
    @patch("tools.trader_client.submit_order")
    @patch("tools.trader_client.get_accounts")
    @patch("tools.trader_client.get_book")
    @patch("tools.trader_client.get_ticker_status")
    @patch("tools.trader_client.check_health")
    @patch("tools.trader_client.time.sleep")
    def test_poll_round_loop_single_round(
        self, mock_sleep, mock_health, mock_ticker, mock_book, mock_accs, mock_order, mock_cancel, mock_locs
    ):
        mock_locs.return_value = {"locations": [{"agent_id": "zero", "status": "docked", "station_id": "ceres"}]}
        mock_health.return_value = {"status": "ok", "seq": 20, "floor": "open"}
        mock_ticker.return_value = {
            "status": "ok",
            "running": True,
            "paused": False,
            "current_round": 1,
        }
        mock_book.return_value = {
            "book": {
                "bids": [{"limit_price": 14, "qty": 10}],
                "asks": [{"limit_price": 16, "qty": 10}],
            }
        }
        mock_accs.return_value = [
            {"agent_id": "zero", "liquid": 10000, "frags": 1000}
        ]
        mock_order.return_value = {"status": "accepted"}
        mock_cancel.return_value = {"status": "cancelled_all", "payload": {"count": 1}}

        # Run for exactly 1 round
        trader_client.poll_round_loop(poll_interval=0.01, max_rounds=1)

        self.assertEqual(mock_order.call_count, 2)  # 1 bid, 1 ask
        mock_cancel.assert_called()

    @patch("tools.trader_client.post_transit")
    @patch("tools.trader_client.get_stations_routes")
    @patch("tools.trader_client.get_stations_prices")
    @patch("tools.trader_client.get_stations_locations")
    @patch("tools.trader_client.cancel_all")
    @patch("tools.trader_client.submit_order")
    @patch("tools.trader_client.get_accounts")
    @patch("tools.trader_client.get_book")
    @patch("tools.trader_client.get_ticker_status")
    @patch("tools.trader_client.check_health")
    @patch("tools.trader_client.time.sleep")
    def test_poll_round_loop_with_spatial_transit(
        self, mock_sleep, mock_health, mock_ticker, mock_book, mock_accs, mock_order, mock_cancel, mock_locs, mock_prices, mock_routes, mock_transit
    ):
        mock_locs.return_value = {"locations": [{"agent_id": "zero", "status": "docked", "station_id": "ceres"}]}
        mock_health.return_value = {"status": "ok", "seq": 20, "floor": "open"}
        mock_ticker.return_value = {
            "status": "ok",
            "running": True,
            "paused": False,
            "current_round": 1,
        }
        mock_book.return_value = {
            "book": {
                "bids": [{"limit_price": 14, "qty": 10}],
                "asks": [{"limit_price": 16, "qty": 10}],
            }
        }
        mock_accs.return_value = [
            {"agent_id": "zero", "liquid": 10000, "frags": 1000, "fuel": 500}
        ]
        mock_prices.return_value = {
            "data": {
                "prices": {
                    "ceres": {"FRAG": 15.0},
                    "earth": {"FRAG": 25.0},
                }
            }
        }
        mock_routes.return_value = {
            "routes": [
                {"destination": "earth", "fuel": 10, "toll": 50}
            ]
        }
        mock_transit.return_value = {"status": "en_route"}
        mock_order.return_value = {"status": "accepted"}
        mock_cancel.return_value = {"status": "cancelled_all", "payload": {"count": 1}}

        trader_client.poll_round_loop(poll_interval=0.01, max_rounds=1)
        mock_transit.assert_called_once_with(destination="earth", commodity="FRAG", cargo_qty=50)


if __name__ == "__main__":
    unittest.main()
