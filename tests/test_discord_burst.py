import unittest

from tools.agora_announcer import (
    parse_discord_burst_cmd,
    format_final_standings,
    BURST_TRIGGER_PATTERN,
    BURST_CANCEL_PATTERN,
    BURST_STATUS_PATTERN
)


class TestDiscordBurst(unittest.TestCase):
    def test_regex_matching(self):
        # Trigger pattern
        m = BURST_TRIGGER_PATTERN.search("!burst 5")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "5")
        self.assertIsNone(m.group(2))

        m = BURST_TRIGGER_PATTERN.search("!burst 8 45")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "8")
        self.assertEqual(m.group(2), "45")

        m = BURST_TRIGGER_PATTERN.search("@referee burst 10 30")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "10")
        self.assertEqual(m.group(2), "30")

        m = BURST_TRIGGER_PATTERN.search("BURST START 12")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "12")

        # Cancel pattern
        self.assertIsNotNone(BURST_CANCEL_PATTERN.search("!burst cancel"))
        self.assertIsNotNone(BURST_CANCEL_PATTERN.search("!burst stop"))
        self.assertIsNotNone(BURST_CANCEL_PATTERN.search("@referee burst cancel"))
        self.assertIsNotNone(BURST_CANCEL_PATTERN.search("BURST HALT"))

        # Status pattern
        self.assertIsNotNone(BURST_STATUS_PATTERN.search("!burst status"))
        self.assertIsNotNone(BURST_STATUS_PATTERN.search("@referee burst status"))

    def test_parse_discord_burst_cmd(self):
        cmd = parse_discord_burst_cmd("!burst 5")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "start")
        self.assertEqual(cmd["rounds"], 5)
        self.assertEqual(cmd["interval_sec"], 180.0)

        cmd = parse_discord_burst_cmd("!burst 8 60")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "start")
        self.assertEqual(cmd["rounds"], 8)
        self.assertEqual(cmd["interval_sec"], 60.0)

        cmd = parse_discord_burst_cmd("!burst cancel")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "cancel")

        cmd = parse_discord_burst_cmd("!burst status")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "status")

        # Non-burst content returns None
        self.assertIsNone(parse_discord_burst_cmd("BUY 50 FOOD @ 32 AT CERES"))
        self.assertIsNone(parse_discord_burst_cmd("MOVE TO MARS WITH 100 FOOD"))

    def test_format_final_standings(self):
        sample_lb = {
            "leaderboard": [
                {
                    "agent_id": "zero",
                    "balance": {"CR": 120000, "FRAG": 500, "FUEL": 350, "FOOD": 200, "ORE": 100},
                    "mtm_net_worth": 165400
                },
                {
                    "agent_id": "amos",
                    "balance": {"CR": 95000, "FRAG": 200, "FUEL": 400, "FOOD": 150, "ORE": 50},
                    "mtm_net_worth": 138200
                }
            ]
        }
        res = format_final_standings(sample_lb)
        self.assertIn("FINAL STANDINGS", res)
        self.assertIn("Apex Vector Arbitrage [AVA]", res)
        self.assertIn("165,400 CR", res)
        self.assertIn("Atlantean Paperclip Manufacturing [APM]", res)
        self.assertIn("138,200 CR", res)


if __name__ == "__main__":
    unittest.main()
