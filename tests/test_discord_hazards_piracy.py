import unittest
from tools.agora_announcer import (
    PIRACY_RESPOND_PATTERN,
    PRIVATEER_PATTERN,
    PIRACY_STATUS_PATTERN,
    HAZARDS_STATUS_PATTERN,
    parse_discord_piracy_respond_cmd,
    parse_discord_privateer_cmd,
    parse_discord_piracy_status_cmd,
    parse_discord_hazards_cmd,
    parse_discord_transit,
    format_piracy_status,
    format_hazards_status
)
from agora.referee import AgoraReferee


class TestDiscordHazardsPiracy(unittest.TestCase):

    def test_regex_patterns(self):
        # Piracy response
        self.assertIsNotNone(PIRACY_RESPOND_PATTERN.search("!respond tx-123 pay"))
        self.assertIsNotNone(PIRACY_RESPOND_PATTERN.search("!ransom tx-456 surrender"))
        self.assertIsNotNone(PIRACY_RESPOND_PATTERN.search("RESPOND tx-789 FIGHT"))

        # Privateers
        self.assertIsNotNone(PRIVATEER_PATTERN.search("!privateer amos"))
        self.assertIsNotNone(PRIVATEER_PATTERN.search("!privateers marvin 15"))
        self.assertIsNotNone(PRIVATEER_PATTERN.search("PRIVATEER zero 5"))

        # Piracy status
        self.assertIsNotNone(PIRACY_STATUS_PATTERN.search("!piracy"))
        self.assertIsNotNone(PIRACY_STATUS_PATTERN.search("!raids"))
        self.assertIsNotNone(PIRACY_STATUS_PATTERN.search("!underworld"))

        # Hazards status
        self.assertIsNotNone(HAZARDS_STATUS_PATTERN.search("!hazards"))
        self.assertIsNotNone(HAZARDS_STATUS_PATTERN.search("!weather"))
        self.assertIsNotNone(HAZARDS_STATUS_PATTERN.search("!solar"))

    def test_parse_discord_piracy_respond_cmd(self):
        # Basic pay
        cmd1 = parse_discord_piracy_respond_cmd("!respond tx-abc-123 pay", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd1)
        self.assertEqual(cmd1["action"], "piracy_respond")
        self.assertEqual(cmd1["transit_id"], "tx-abc-123")
        self.assertEqual(cmd1["choice"], "pay")
        self.assertEqual(cmd1["agent_id"], "zero")

        # Surrender with ransom alias and agent override
        cmd2 = parse_discord_piracy_respond_cmd("!ransom tx-xyz-999 surrender as amos", "123", "User")
        self.assertIsNotNone(cmd2)
        self.assertEqual(cmd2["transit_id"], "tx-xyz-999")
        self.assertEqual(cmd2["choice"], "surrender")
        self.assertEqual(cmd2["agent_id"], "amos")

        # Fight
        cmd3 = parse_discord_piracy_respond_cmd("!respond tx-battle fight", "1542035925603713086", "Aerial")
        self.assertIsNotNone(cmd3)
        self.assertEqual(cmd3["choice"], "fight")
        self.assertEqual(cmd3["agent_id"], "aerial")

    def test_parse_discord_privateer_cmd(self):
        # Default duration (10)
        cmd1 = parse_discord_privateer_cmd("!privateer amos", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd1)
        self.assertEqual(cmd1["action"], "privateer")
        self.assertEqual(cmd1["sponsor"], "zero")
        self.assertEqual(cmd1["target"], "amos")
        self.assertEqual(cmd1["duration"], 10)

        # Custom duration and sponsor override
        cmd2 = parse_discord_privateer_cmd("!privateers marvin 20 as aerial", "123", "User")
        self.assertIsNotNone(cmd2)
        self.assertEqual(cmd2["sponsor"], "aerial")
        self.assertEqual(cmd2["target"], "marvin")
        self.assertEqual(cmd2["duration"], 20)

    def test_parse_discord_piracy_status_cmd(self):
        cmd = parse_discord_piracy_status_cmd("!piracy", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "piracy_status")
        self.assertEqual(cmd["agent_id"], "zero")

        cmd_raids = parse_discord_piracy_status_cmd("!raids", "1468012353206354197", "Amos")
        self.assertIsNotNone(cmd_raids)
        self.assertEqual(cmd_raids["action"], "piracy_status")
        self.assertEqual(cmd_raids["agent_id"], "amos")

    def test_parse_discord_hazards_cmd(self):
        cmd = parse_discord_hazards_cmd("!hazards", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "hazards_status")

        cmd_weather = parse_discord_hazards_cmd("!weather", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd_weather)
        self.assertEqual(cmd_weather["action"], "hazards_status")

    def test_parse_discord_transit_with_escort(self):
        # Transit with escort and cargo
        t1 = parse_discord_transit("!transit CERES with 50 FRAG escort", "1542081375287640084", "Zero")
        self.assertIsNotNone(t1)
        self.assertEqual(t1["destination"], "ceres")
        self.assertEqual(t1["cargo_qty"], 50)
        self.assertEqual(t1["commodity"], "FRAG")
        self.assertTrue(t1["escort"])

        # Transit with escort on secondary ship
        t2 = parse_discord_transit("!transit CERES 2 with 100 FUEL with escort", "1542081375287640084", "Zero")
        self.assertIsNotNone(t2)
        self.assertEqual(t2["destination"], "ceres")
        self.assertEqual(t2["vessel_id"], "2")
        self.assertEqual(t2["cargo_qty"], 100)
        self.assertEqual(t2["commodity"], "FUEL")
        self.assertTrue(t2["escort"])

        # Transit without escort
        t3 = parse_discord_transit("!transit MARS with 30 ORE", "1542081375287640084", "Zero")
        self.assertIsNotNone(t3)
        self.assertFalse(t3["escort"])

    def test_format_piracy_status(self):
        sample_data = {
            "status": "ok",
            "hot_station": "ceres",
            "hot_until": 20,
            "odds": [0.15, 0.04],
            "active_contracts": [
                {"sponsor": "zero", "target": "amos", "expires_round": 15}
            ],
            "recent_raids": [
                {"agent_id": "amos", "origin": "earth", "destination": "ceres", "round": 8}
            ]
        }
        res = format_piracy_status(sample_data)
        self.assertIn("Space-Lane Security & Piracy Briefing", res)
        self.assertIn("Ceres", res)
        self.assertIn("15%", res)
        self.assertIn("4%", res)
        self.assertIn("1 privateer contract(s) active", res)
        self.assertIn("raided near Ceres", res)
        self.assertLess(len(res), 2000)

    def test_format_hazards_status(self):
        sample_hazards = {
            "status": "ok",
            "enabled": True,
            "round": 12,
            "odds": {"delay": 0.20, "loss": 0.25},
            "recent": [
                {"agent_id": "zero", "round": 10, "delay": 2, "lost_qty": 15, "note": "storm on the route: arrival 2 rounds late"}
            ]
        }
        res = format_hazards_status(sample_hazards)
        self.assertIn("Space Weather & Corridor Hazards Briefing", res)
        self.assertIn("Round #12", res)
        self.assertIn("Delay Chance: **20%**", res)
        self.assertIn("Hull Loss: **25%**", res)
        self.assertIn("storm on the route", res)
        self.assertLess(len(res), 2000)

    def test_referee_transit_with_escort_and_hazards(self):
        ref = AgoraReferee()
        ref.new_game(seed=42, warmup_rounds=2, piracy="0.15,0.04", hazards="0.2,0.25")

        # Fund zero with cash and fuel and goods
        with ref.lock, ref.conn:
            ref.fleet._move("test-fund", (
                ('zero', 'CR', 100000),
                ('SYSTEM', 'CR', -100000),
                ('zero/1', 'FUEL', 1000),
                ('SYSTEM', 'FUEL', -1000),
                ('zero/1', 'FRAG', 500),
                ('SYSTEM', 'FRAG', -500),
            ))

        # Fly zero/1 from ceres to earth with escort
        res = ref.initiate_transit("zero", "earth", commodity="FRAG", cargo_qty=50, escort=True, vessel_id="zero/1")
        self.assertEqual(res["kind"], "status")
        payload = res["payload"]
        self.assertEqual(payload["destination"], "earth")
        self.assertIsNotNone(payload.get("piracy"))
        self.assertTrue(payload["piracy"]["escort"])
        self.assertGreater(payload["piracy"]["escort_fee"], 0)

        # Verify ledger invariants stay mathematically balanced
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)


if __name__ == "__main__":
    unittest.main()
