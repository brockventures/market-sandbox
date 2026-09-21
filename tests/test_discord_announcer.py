"""
tests/test_discord_announcer.py - Unit tests for Discord Burst Trigger & Standings Announcer (Issue #64).
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from tools.discord_burst_announcer import (
    BurstAnnouncerService,
    RefereeClient,
    StandingsFormatter,
    dispatch_webhook,
    parse_trigger_command,
)


class TestDiscordBurstTriggerParsing(unittest.TestCase):

    def test_parse_valid_burst_commands(self):
        cmd1 = parse_trigger_command("@referee burst 5")
        self.assertEqual(cmd1, {"action": "burst", "rounds": 5, "interval_sec": 30.0})

        cmd2 = parse_trigger_command("@referee burst 10 15.5")
        self.assertEqual(cmd2, {"action": "burst", "rounds": 10, "interval_sec": 15.5})

        cmd3 = parse_trigger_command("<@1542035925603713086> burst 3 20")
        self.assertEqual(cmd3, {"action": "burst", "rounds": 3, "interval_sec": 20.0})

        cmd4 = parse_trigger_command("!referee burst 1")
        self.assertEqual(cmd4, {"action": "burst", "rounds": 1, "interval_sec": 30.0})

    def test_parse_control_commands(self):
        self.assertEqual(parse_trigger_command("@referee pause"), {"action": "pause"})
        self.assertEqual(parse_trigger_command("@referee resume"), {"action": "resume"})
        self.assertEqual(parse_trigger_command("@referee status"), {"action": "status"})
        self.assertEqual(parse_trigger_command("@referee help"), {"action": "help"})

    def test_parse_invalid_burst_parameters(self):
        # Missing rounds
        res1 = parse_trigger_command("@referee burst")
        self.assertEqual(res1["action"], "error")
        self.assertIn("Missing rounds", res1["error"])

        # Non-integer rounds
        res2 = parse_trigger_command("@referee burst five")
        self.assertEqual(res2["action"], "error")
        self.assertIn("Must be an integer", res2["error"])

        # Out-of-bounds rounds
        res3 = parse_trigger_command("@referee burst 0")
        self.assertEqual(res3["action"], "error")
        self.assertIn("out of range", res3["error"])

        res4 = parse_trigger_command("@referee burst 51")
        self.assertEqual(res4["action"], "error")
        self.assertIn("out of range", res4["error"])

        # Invalid interval
        res5 = parse_trigger_command("@referee burst 5 abc")
        self.assertEqual(res5["action"], "error")
        self.assertIn("Invalid interval_sec", res5["error"])

        res6 = parse_trigger_command("@referee burst 5 -10")
        self.assertEqual(res6["action"], "error")
        self.assertIn("greater than 0", res6["error"])

    def test_non_trigger_messages(self):
        self.assertIsNone(parse_trigger_command("Hello team, good game!"))
        self.assertIsNone(parse_trigger_command("Let's look at the referee logs"))
        self.assertIsNone(parse_trigger_command(""))
        self.assertIsNone(parse_trigger_command(None))


class TestStandingsFormatter(unittest.TestCase):

    def test_format_burst_start(self):
        payload = StandingsFormatter.format_burst_start(
            burst_id="burst-1001",
            rounds=5,
            interval_sec=15.0,
            start_round=10
        )
        self.assertIn("content", payload)
        self.assertIn("burst-1001", payload["content"])
        self.assertIn("Rounds #11 -> #15", payload["content"])
        self.assertIn("15.0s", payload["content"])
        self.assertEqual(payload["username"], "Agora Trade Terminal")

    def test_format_round_tick(self):
        lb = [
            {"agent_id": "amos", "fleet_name": "Atlantean Paperclip", "net_worth": 26025},
            {"agent_id": "aerial", "fleet_name": "Zenith Drift", "net_worth": 26000}
        ]
        payload = StandingsFormatter.format_round_tick(
            round_num=3,
            seq=42,
            mark_price=16.0,
            halts=["ceres:FRAG"],
            leaderboard=lb
        )
        self.assertIn("Round 3 Strategy Window", payload["content"])
        self.assertIn("SEQ: #42", payload["content"])
        self.assertIn("CERES MARK: 16.0 CR", payload["content"])
        self.assertIn("HALTS: ceres:FRAG", payload["content"])
        self.assertIn("Atlantean Paperclip (26,025 CR)", payload["content"])

    def test_format_burst_concluded(self):
        standings = [
            {"agent_id": "amos", "fleet_name": "Atlantean Paperclip", "net_worth": 26025, "liquid_cr": 10000, "frag_balance": 1000, "fuel_balance": 500},
            {"agent_id": "aerial", "fleet_name": "Zenith Drift", "net_worth": 26000, "liquid_cr": 10000, "frag_balance": 1000, "fuel_balance": 500}
        ]
        payload = StandingsFormatter.format_burst_concluded(
            burst_id="burst-final",
            final_round=5,
            standings=standings
        )
        self.assertIn("Operation Burst Concluded", payload["content"])
        self.assertIn("FINAL ROUND: #5", payload["content"])
        self.assertIn("🥇 **#1 Atlantean Paperclip [AMOS]**: **26,025 CR**", payload["content"])
        self.assertIn("🥈 **#2 Zenith Drift [AERIAL]**: **26,000 CR**", payload["content"])


class TestBurstAnnouncerService(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock(spec=RefereeClient)
        self.service = BurstAnnouncerService(
            referee_client=self.mock_client,
            webhook_url="https://discord.com/api/webhooks/mock"
        )

    def test_handle_burst_command_success(self):
        self.mock_client.trigger_burst.return_value = {
            "status": "burst_started",
            "burst_id": "burst-test-99",
            "rounds": 5,
            "interval_sec": 30.0
        }
        self.mock_client.get_ticker_status.return_value = {
            "current_round": 10,
            "burst_active": True
        }

        with patch("tools.discord_burst_announcer.dispatch_webhook") as mock_webhook:
            mock_webhook.return_value = True
            msg = "@referee burst 5"
            reply = self.service.handle_discord_message(msg, author="ryan")

            self.assertIn("Burst Initiated by @ryan", reply)
            self.assertIn("5 rounds", reply)
            self.assertIn("burst-test-99", reply)
            self.mock_client.trigger_burst.assert_called_once_with(rounds=5, interval_sec=30.0)
            mock_webhook.assert_called_once()

    def test_handle_burst_conflict_error(self):
        self.mock_client.trigger_burst.return_value = {
            "status": 409,
            "error": "burst in progress"
        }
        reply = self.service.handle_discord_message("@referee burst 3", author="alex")
        self.assertIn("Burst Rejected", reply)

    def test_handle_control_commands(self):
        self.mock_client.pause_ticker.return_value = {"status": "paused"}
        reply_pause = self.service.handle_discord_message("@referee pause")
        self.assertIn("Market Ticker Paused", reply_pause)

        self.mock_client.resume_ticker.return_value = {"status": "resumed"}
        reply_resume = self.service.handle_discord_message("@referee resume")
        self.assertIn("Market Ticker Resumed", reply_resume)

        self.mock_client.get_ticker_status.return_value = {
            "running": True,
            "paused": False,
            "current_round": 4,
            "burst_active": False,
            "next_tick_eta_sec": 24.5
        }
        reply_status = self.service.handle_discord_message("@referee status")
        self.assertIn("Station Agora Ticker Status", reply_status)
        self.assertIn("#4", reply_status)

    def test_poll_and_announce_lifecycle(self):
        with patch("tools.discord_burst_announcer.dispatch_webhook") as mock_webhook:
            # 1. Burst starts
            self.mock_client.get_ticker_status.return_value = {
                "burst_active": True,
                "burst_id": "burst-test-1",
                "burst_rounds_total": 3,
                "current_round": 1,
                "interval_sec": 20.0,
                "quiet_round_count": 0
            }
            ev1 = self.service.poll_and_announce_once()
            self.assertEqual(ev1, "burst_started")
            self.assertEqual(mock_webhook.call_count, 1)

            # 2. Round tick advances
            self.mock_client.get_ticker_status.return_value = {
                "burst_active": True,
                "burst_id": "burst-test-1",
                "current_round": 2,
                "quiet_round_count": 0
            }
            self.mock_client.get_leaderboard.return_value = {
                "leaderboard": [{"agent_id": "amos", "fleet_name": "Atlantean", "net_worth": 26000}]
            }
            ev2 = self.service.poll_and_announce_once()
            self.assertEqual(ev2, "round_2_announced")
            self.assertEqual(mock_webhook.call_count, 2)

            # 3. Burst concludes
            self.mock_client.get_ticker_status.return_value = {
                "burst_active": False,
                "current_round": 3,
                "quiet_round_count": 1
            }
            ev3 = self.service.poll_and_announce_once()
            self.assertEqual(ev3, "burst_concluded")
            self.assertEqual(mock_webhook.call_count, 3)


class TestDiscordBurstAnnouncerLiveHttpIntegration(unittest.TestCase):

    def test_live_http_burst_trigger_and_status(self):
        import socket
        import threading
        import time
        from http.server import ThreadingHTTPServer
        from agora.referee import AgoraReferee
        from agora.ticker import TickerEngine
        from agora.server import make_handler

        referee = AgoraReferee()
        ticker = TickerEngine(referee, interval_sec=10.0, inactivity_rounds=50)
        auth_tokens = {"aerial": "token-aerial-123"}
        handler_cls = make_handler(referee, ticker=ticker, auth_tokens=auth_tokens)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = server.server_address[1]

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.05)

        try:
            client = RefereeClient(base_url=f"http://127.0.0.1:{port}", token="token-aerial-123")
            service = BurstAnnouncerService(referee_client=client, webhook_url="")

            # 1. Test status before burst
            status_reply = service.handle_discord_message("@referee status")
            self.assertIn("Station Agora Ticker Status", status_reply)

            # 2. Test trigger burst via Discord message
            burst_reply = service.handle_discord_message("@referee burst 2 0.05", author="ryan")
            self.assertIn("Burst Initiated by @ryan", burst_reply)
            self.assertIn("2 rounds", burst_reply)

            # 3. Wait for burst to execute 2 rounds
            deadline = time.time() + 3.0
            while time.time() < deadline:
                st = client.get_ticker_status()
                if not st.get("burst_active") and referee.current_round >= 2:
                    break
                time.sleep(0.05)

            self.assertGreaterEqual(referee.current_round, 2)

            # 4. Verify pause/resume via Discord message
            pause_reply = service.handle_discord_message("@referee pause")
            self.assertIn("Market Ticker Paused", pause_reply)
            st_paused = client.get_ticker_status()
            self.assertTrue(st_paused.get("paused"))

            resume_reply = service.handle_discord_message("@referee resume")
            self.assertIn("Market Ticker Resumed", resume_reply)
            st_resumed = client.get_ticker_status()
            self.assertFalse(st_resumed.get("paused"))

        finally:
            ticker.stop()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
