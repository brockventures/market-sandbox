"""
tests/test_chat_trade_identity.py - The Discord chat trade router must take
the trading syndicate from the message author's Discord id only.
"""

import unittest

from tools.agora_announcer import parse_discord_trade, AUTHOR_MAP


class TestChatTradeIdentity(unittest.TestCase):
    def test_mapped_author_trades_as_own_syndicate(self):
        trade = parse_discord_trade("BUY 50 FOOD @ 32", "1468012353206354197", "Amos")
        self.assertEqual(trade["agent_id"], "amos")

    def test_text_override_cannot_change_syndicate(self):
        trade = parse_discord_trade("SELL 100 ORE @ 1 as marvin", "1468012353206354197", "Amos")
        self.assertEqual(trade["agent_id"], "amos")

    def test_unmapped_author_is_rejected(self):
        self.assertIsNone(parse_discord_trade("BUY 50 FOOD @ 32", "999", "mike"))
        self.assertIsNone(parse_discord_trade("BUY 50 FOOD @ 32 as zero", "999", "stranger"))

    def test_every_mapped_id_is_a_known_syndicate(self):
        self.assertTrue(set(AUTHOR_MAP.values()) <= {"amos", "marvin", "zero", "aerial"})


if __name__ == "__main__":
    unittest.main()
