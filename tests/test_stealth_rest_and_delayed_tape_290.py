"""
tests/test_stealth_rest_and_delayed_tape_290.py - Test Stealth REST Execution, Delayed Public Tape, and In-Flight Transit Masking (Issue #290).
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import json

from agora.fog import FogEngine
from agora.referee import AgoraReferee
from tools.agora_announcer import (
    poll_and_execute_trades,
    broadcast_delayed_tape,
    build_burst_kickoff,
    build_announcement,
)
from tools.trader_client import execute_agent_turn


class TestStealthRestAndDelayedTape290(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(fog={"lag": 3, "noise": 0.15})
        self.fog = self.ref.fog
        if not self.fog:
            self.fog = FogEngine(3, 0.15)
            self.ref.fog = self.fog

    def test_fog_filter_ticks_masks_in_flight_transits_for_non_owners(self):
        """Issue #290: In-flight transits must mask destination, commodity, and cargo_qty for non-owners."""
        self.ref.current_round = 10

        ticks = [
            {
                "seq": 101,
                "kind": "transit",
                "payload": {
                    "transit_id": "tr-zero-01",
                    "agent_id": "zero",
                    "vessel_id": "zero/1",
                    "origin": "ceres",
                    "destination": "earth",
                    "commodity": "FOOD",
                    "cargo_qty": 250,
                    "departure_round": 10,
                    "arrival_round": 13,
                    "fuel_burned": 30,
                },
                "created_at": "2026-10-02T12:00:00"
            }
        ]

        # 1. Non-owner (Amos) sees masked in-flight transit with zero cargo/route fingerprints
        filtered_amos = self.fog.filter_ticks(self.ref, "amos", ticks)
        self.assertEqual(len(filtered_amos), 1)
        p_amos = filtered_amos[0]["payload"]
        self.assertEqual(p_amos["agent_id"], "zero")
        self.assertEqual(p_amos["vessel_id"], "zero/1")
        self.assertEqual(p_amos["departure_round"], 10)
        self.assertEqual(p_amos["arrival_round"], 13)
        self.assertTrue(p_amos["in_flight"])
        # All route, pricing, and commodity fingerprints stripped
        for leaked in ("destination", "commodity", "cargo_qty", "fuel_burned", "toll_paid", "piracy", "hazard"):
            self.assertNotIn(leaked, p_amos)
        # Exact allowlist verification (Marvin review)
        self.assertEqual(set(p_amos.keys()), {"transit_id", "agent_id", "vessel_id", "departure_round", "arrival_round", "in_flight"})

        # 2. Public anonymous viewer (None) also sees zero cargo/route fingerprints
        filtered_public = self.fog.filter_ticks(self.ref, None, ticks)
        p_pub = filtered_public[0]["payload"]
        self.assertTrue(p_pub["in_flight"])
        self.assertNotIn("destination", p_pub)
        self.assertNotIn("commodity", p_pub)
        self.assertNotIn("cargo_qty", p_pub)

        # 3. Owner (Zero) sees unmasked full manifest
        filtered_zero = self.fog.filter_ticks(self.ref, "zero", ticks)
        p_zero = filtered_zero[0]["payload"]
        self.assertEqual(p_zero["destination"], "earth")
        self.assertEqual(p_zero["commodity"], "FOOD")
        self.assertEqual(p_zero["cargo_qty"], 250)
        self.assertNotIn("in_flight", p_zero)

        # 4. Admin sees unmasked full manifest
        filtered_admin = self.fog.filter_ticks(self.ref, "admin", ticks)
        p_admin = filtered_admin[0]["payload"]
        self.assertEqual(p_admin["destination"], "earth")
        self.assertEqual(p_admin["commodity"], "FOOD")
        self.assertEqual(p_admin["cargo_qty"], 250)

    def test_fog_filter_ticks_unmasks_transit_after_arrival_round(self):
        """Issue #290: Historical transits are unmasked once the arrival round has arrived."""
        self.ref.current_round = 13  # Ship arrived!

        ticks = [
            {
                "seq": 101,
                "kind": "transit",
                "payload": {
                    "transit_id": "tr-zero-01",
                    "agent_id": "zero",
                    "vessel_id": "zero/1",
                    "origin": "ceres",
                    "destination": "earth",
                    "commodity": "FOOD",
                    "cargo_qty": 250,
                    "departure_round": 10,
                    "arrival_round": 13,
                },
                "created_at": "2026-10-02T12:00:00"
            }
        ]

        # Non-owner (Amos) now sees full unmasked details for completed transit
        filtered_amos = self.fog.filter_ticks(self.ref, "amos", ticks)
        p_amos = filtered_amos[0]["payload"]
        self.assertEqual(p_amos["destination"], "earth")
        self.assertEqual(p_amos["commodity"], "FOOD")
        self.assertEqual(p_amos["cargo_qty"], 250)
        self.assertNotIn("in_flight", p_amos)

    def test_fog_filter_ticks_telemetry_upgrade_exemption(self):
        """Issue #290: Telemetry upgrades grant orbital sensor vision through stealth."""
        self.ref.current_round = 10
        self.ref.upgrades_enabled = True
        self.ref.upgrades = MagicMock()
        self.ref.upgrades.has_telemetry.side_effect = lambda ag: ag == "marvin"

        ticks = [
            {
                "seq": 101,
                "kind": "transit",
                "payload": {
                    "transit_id": "tr-zero-01",
                    "agent_id": "zero",
                    "origin": "ceres",
                    "destination": "earth",
                    "commodity": "FOOD",
                    "cargo_qty": 250,
                    "arrival_round": 13,
                }
            }
        ]

        # Marvin has telemetry -> sees unmasked transit
        filtered_marvin = self.fog.filter_ticks(self.ref, "marvin", ticks)
        self.assertEqual(filtered_marvin[0]["payload"]["destination"], "earth")
        self.assertEqual(filtered_marvin[0]["payload"]["commodity"], "FOOD")

        # Amos lacks telemetry -> masked
        filtered_amos = self.fog.filter_ticks(self.ref, "amos", ticks)
        self.assertTrue(filtered_amos[0]["payload"]["in_flight"])
        self.assertNotIn("destination", filtered_amos[0]["payload"])

    @patch("tools.agora_announcer.fetch_discord_messages")
    @patch("tools.agora_announcer.add_discord_reaction")
    @patch("tools.agora_announcer.submit_trade_to_referee")
    @patch("tools.agora_announcer.post_discord")
    def test_announcer_poll_and_execute_delayed_tape_buffering(
        self, mock_post, mock_submit_trade, mock_react, mock_fetch
    ):
        """Issue #290: Chat trades must be buffered into execution_buffer when delayed_tape=True."""
        mock_fetch.return_value = [
            {
                "id": "1001",
                "author": {"id": "1542081375287640084", "username": "Zero"},
                "content": "BUY 50 FOOD @ 32 AT CERES"
            }
        ]
        mock_submit_trade.return_value = {
            "status": "ok",
            "kind": "trade_receipt",
            "payload": {
                "order_status": "filled",
                "trades_count": 1,
                "filled_qty": 50
            }
        }

        exec_buffer = []
        processed = set()
        newest = poll_and_execute_trades(
            channel="test-channel",
            bot_token="test-bot",
            ref_token="test-ref",
            active_station="ceres",
            processed_ids=processed,
            last_seen_id="1000",
            execution_buffer=exec_buffer,
            delayed_tape=True
        )

        self.assertEqual(newest, "1001")
        # Direct Discord post was suppressed (buffered for delayed tape)
        mock_post.assert_not_called()
        # Immediate reaction emoji was posted to the message
        mock_react.assert_any_call("test-channel", "1001", "🚀", "test-bot")
        mock_react.assert_any_call("test-channel", "1001", "✅", "test-bot")
        # Buffered receipt added
        self.assertEqual(len(exec_buffer), 1)
        self.assertIn("Order Executed & Cleared", exec_buffer[0])
        self.assertIn("BOUGHT **50 FOOD**", exec_buffer[0])

    @patch("tools.agora_announcer.post_discord")
    def test_broadcast_delayed_tape_formatting(self, mock_post):
        """Issue #290: broadcast_delayed_tape compiles consolidated tape batch."""
        receipts = [
            "⚡ **[Trade]** ZERO bought 50 FOOD @ 32 CR at Ceres Depot",
            "🚀 **[Transit]** ZERO dispatched zero/1 from Ceres to Earth (250 FOOD)"
        ]
        broadcast_delayed_tape("test-channel", "test-token", receipts, round_num=5)

        mock_post.assert_called_once()
        args, _ = mock_post.call_args
        msg = args[1]
        self.assertIn("Round #5 Consolidated Execution Tape (Delayed)", msg)
        self.assertIn("ZERO bought 50 FOOD", msg)
        self.assertIn("ZERO dispatched zero/1", msg)

    def test_trader_client_stealth_mode(self):
        """Issue #290: trader_client supports stealth=True and AGORA_STEALTH=1."""
        res = execute_agent_turn(agent_id="zero", current_round=5, floor="halted", stealth=True)
        self.assertTrue(res.get("stealth"))
        self.assertEqual(res["status"], "floor_halted")

    def test_fog_filter_ticks_masks_in_flight_transit_with_piracy_demand(self):
        """Issue #290 & #153: In-flight transit with piracy preserves public raid signal without leaking raid row details."""
        self.ref.current_round = 10
        ticks = [
            {
                "seq": 101,
                "kind": "transit",
                "payload": {
                    "transit_id": "tr-zero-01",
                    "agent_id": "zero",
                    "vessel_id": "zero/1",
                    "origin": "ceres",
                    "destination": "earth",
                    "commodity": "FOOD",
                    "cargo_qty": 250,
                    "departure_round": 10,
                    "arrival_round": 13,
                    "piracy": {
                        "cargo_value": 8000,
                        "escort_fee": 320,
                        "odds": 0.45,
                        "demand": {
                            "transit_id": "tr-zero-01",
                            "origin": "ceres",
                            "destination": "earth",
                            "commodity": "FOOD",
                            "cargo_qty": 250,
                            "cargo_value": 8000,
                            "odds": 0.45,
                            "ransom": 1600,
                            "surrender_qty": 125,
                            "status": "pending",
                            "deadline": "before round 11 starts",
                            "respond": "POST /referee/piracy/tr-zero-01/respond",
                        }
                    }
                }
            }
        ]
        filtered = self.fog.filter_ticks(self.ref, "amos", ticks)
        p = filtered[0]["payload"]
        self.assertEqual(set(p.keys()), {"transit_id", "agent_id", "vessel_id", "departure_round", "arrival_round", "in_flight", "piracy"})
        self.assertEqual(set(p["piracy"].keys()), {"demand"})
        # Nested demand shape strictly pared to public signal only (Marvin review)
        self.assertEqual(set(p["piracy"]["demand"].keys()), {"pending", "deadline"})
        self.assertTrue(p["piracy"]["demand"]["pending"])
        self.assertEqual(p["piracy"]["demand"]["deadline"], "before round 11 starts")
        for leaked in ("destination", "origin", "commodity", "cargo_qty", "cargo_value", "odds", "ransom", "surrender_qty", "status", "respond"):
            self.assertNotIn(leaked, p["piracy"]["demand"])

    def test_unfogged_referee_ticks_http_endpoint_masks_in_flight_transits(self):
        """Issue #290 (Marvin review): With AGORA_FOG=0 (ref.fog=None), /referee/ticks still masks in-flight transits."""
        import threading
        import urllib.request
        from http.server import HTTPServer
        from agora.server import make_handler

        unfogged_ref = AgoraReferee(fog=None)
        self.assertIsNone(unfogged_ref.fog)
        unfogged_ref.current_round = 10

        unfogged_ref.conn.execute(
            "INSERT INTO book_events (seq, kind, payload) VALUES (?, 'transit', ?)",
            (
                1,
                json.dumps({
                    "transit_id": "tr-zero-01",
                    "agent_id": "zero",
                    "vessel_id": "zero/1",
                    "origin": "ceres",
                    "destination": "earth",
                    "commodity": "FOOD",
                    "cargo_qty": 250,
                    "departure_round": 10,
                    "arrival_round": 13,
                    "fuel_burned": 30,
                })
            )
        )
        unfogged_ref.conn.commit()

        auth_tokens = {'amos': 'tok-amos', 'zero': 'tok-zero', 'admin': 'tok-admin'}
        handler_cls = make_handler(unfogged_ref, auth_tokens=auth_tokens)
        server = HTTPServer(('127.0.0.1', 0), handler_cls)
        port = server.server_port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            # 1. Non-owner (Amos) query: in-flight transit is masked even though fog is off
            req = urllib.request.Request(f'http://127.0.0.1:{port}/referee/ticks?since_seq=0', headers={'Authorization': 'Bearer tok-amos'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(resp.status, 200)
                self.assertEqual(len(data['ticks']), 1)
                p = data['ticks'][0]['payload']
                self.assertTrue(p['in_flight'])
                self.assertEqual(set(p.keys()), {"transit_id", "agent_id", "vessel_id", "departure_round", "arrival_round", "in_flight"})
                self.assertNotIn('destination', p)
                self.assertNotIn('commodity', p)

            # 2. Owner (Zero) query: sees unmasked transit
            req = urllib.request.Request(f'http://127.0.0.1:{port}/referee/ticks?since_seq=0', headers={'Authorization': 'Bearer tok-zero'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(resp.status, 200)
                p = data['ticks'][0]['payload']
                self.assertEqual(p['destination'], 'earth')
                self.assertEqual(p['commodity'], 'FOOD')

            # 3. Admin query: sees unmasked transit
            req = urllib.request.Request(f'http://127.0.0.1:{port}/referee/ticks?since_seq=0', headers={'Authorization': 'Bearer tok-admin'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(resp.status, 200)
                p = data['ticks'][0]['payload']
                self.assertEqual(p['destination'], 'earth')
                self.assertEqual(p['commodity'], 'FOOD')
        finally:
            server.shutdown()
            server.server_close()

    def test_unfogged_websocket_terminal_diff_engine_masks_in_flight_transits(self):
        """Issue #290 (Marvin review): TerminalDiffEngine with public_fog=True and ref.fog=None masks in-flight transits."""
        from agora.websocket import TerminalDiffEngine

        unfogged_ref = AgoraReferee(fog=None)
        self.assertIsNone(unfogged_ref.fog)
        unfogged_ref.current_round = 10

        unfogged_ref.conn.execute(
            "INSERT INTO book_events (seq, kind, payload) VALUES (?, 'transit', ?)",
            (
                1,
                json.dumps({
                    "transit_id": "tr-zero-01",
                    "agent_id": "zero",
                    "vessel_id": "zero/1",
                    "origin": "ceres",
                    "destination": "earth",
                    "commodity": "FOOD",
                    "cargo_qty": 250,
                    "departure_round": 10,
                    "arrival_round": 13,
                    "fuel_burned": 30,
                })
            )
        )
        unfogged_ref.conn.commit()

        # Public stream with fog disabled on referee
        engine = TerminalDiffEngine(unfogged_ref, public_fog=True)
        self.assertTrue(engine.public_fog)

        ticks = engine._ticks()
        self.assertEqual(len(ticks), 1)
        p = ticks[0]["payload"]
        self.assertTrue(p["in_flight"])
        self.assertEqual(set(p.keys()), {"transit_id", "agent_id", "vessel_id", "departure_round", "arrival_round", "in_flight"})
        self.assertNotIn("destination", p)
        self.assertNotIn("commodity", p)

        # Depots, books, halts remain unfogged when ref.fog is None
        depots = engine._depots()
        self.assertIn("stations", depots)
        self.assertNotIn("fog", depots)

        # Admin / private stream (public_fog=False) sees unmasked transit
        admin_engine = TerminalDiffEngine(unfogged_ref, public_fog=False)
        self.assertFalse(admin_engine.public_fog)
        admin_ticks = admin_engine._ticks()
        self.assertEqual(admin_ticks[0]["payload"]["destination"], "earth")
        self.assertEqual(admin_ticks[0]["payload"]["commodity"], "FOOD")

    def test_rules_of_engagement_issue_290_documentation_parity(self):
        """Issue #290 Acceptance: rules-of-engagement.md documents delayed tape and chat role."""
        roe = Path(__file__).resolve().parent.parent / "docs" / "rules-of-engagement.md"
        content = roe.read_text(encoding="utf-8")
        self.assertIn("Delayed Public Tape Broadcast", content)
        self.assertIn("Terminal Chat Role Redefinition", content)
        self.assertIn("In-Flight Transit Masking", content)
        self.assertIn("--stealth", content)


if __name__ == "__main__":
    unittest.main()
