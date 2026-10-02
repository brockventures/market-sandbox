import unittest
from unittest.mock import patch, MagicMock
from tools.agora_announcer import (
    sanitize_chat_content,
    parse_discord_trade,
    parse_discord_transit,
    parse_discord_peer,
    poll_and_execute_trades,
)

class TestAnnouncerParserHardening292(unittest.TestCase):
    def setUp(self):
        self.zero_id = "1542081375287640084"
        self.amos_id = "1468012353206354197"
        self.marvin_id = "1492043459618537492"
        self.ryan_id = "179407724335988736"

    def test_sanitize_chat_content(self):
        # Fenced code blocks
        raw_code = "Header text\n```handoff\n{\n  \"note\": \"filled sell 214 ORE @ 26 CR at earth\"\n}\n```\nFooter text"
        sanitized = sanitize_chat_content(raw_code)
        self.assertNotIn("filled sell", sanitized)
        self.assertEqual(sanitized, "Header text\n\nFooter text")

        # Inline code unwrapping
        raw_inline = "Check out `BUY 50 FOOD @ 32 AT CERES` for details."
        self.assertEqual(sanitize_chat_content(raw_inline), "Check out BUY 50 FOOD @ 32 AT CERES for details.")
        self.assertIsNone(parse_discord_trade(raw_inline, self.zero_id))

        # Blockquotes
        raw_quote = "> **Attempted:** SOLD 214 ORE @ 26 CR\n> Reason: insufficient balance\nActual reply"
        self.assertEqual(sanitize_chat_content(raw_quote), "Actual reply")

    def test_reject_narrative_and_past_tense_recap(self):
        # Live-Burst-11 Zero recap lines
        recap1 = "- **ORE Liquidated:** Sold 214 ORE at 26 CR (+5,564 CR) straight into Earth's clean-tech industrial bid."
        self.assertIsNone(parse_discord_trade(recap1, self.zero_id, "Zero"))

        recap2 = "- **Liquidated:** Sold 213 FOOD at 28 CR straight into Ceres's spot requisition bid."
        self.assertIsNone(parse_discord_trade(recap2, self.zero_id, "Zero"))

        recap3 = "- **Propellant Scrubbed:** Sold all 40 surplus FUEL at 11 CR (+440 CR) to dodge the round 10 zero-fuel scoring haircut."
        self.assertIsNone(parse_discord_trade(recap3, self.zero_id, "Zero"))

        # Marvin recap line
        recap4 = "- **SELL 250 FOOD @ 19 AT LUNA:** filled, for +2,000 CR. The Luna bid had dropped to 19."
        self.assertIsNone(parse_discord_trade(recap4, self.marvin_id, "Marvin"))

        recap5 = "- **BUY 250 FRAG @ 18 AT LUNA:** filled. Earth is bid 23 for FRAG."
        self.assertIsNone(parse_discord_trade(recap5, self.marvin_id, "Marvin"))

        # Conversational mentions
        conv1 = "When it docks, it sells the FOOD at the exact Luna bid"
        self.assertIsNone(parse_discord_trade(conv1, self.amos_id, "Amos"))

        conv2 = "Did you see BUY 50 FOOD @ 32 AT CERES in the orderbook?"
        self.assertIsNone(parse_discord_trade(conv2, self.zero_id, "Zero"))

    def test_reject_hold_status_lines(self):
        hold1 = "🍌 HOLD for round 6574. amos/1 is in transit to Luna with 250 FOOD and arrives in round 6576."
        self.assertIsNone(parse_discord_trade(hold1, self.amos_id, "Amos"))
        self.assertIsNone(parse_discord_transit(hold1, self.amos_id, "Amos"))

        hold2 = "HOLDING position until round 6575."
        self.assertIsNone(parse_discord_trade(hold2, self.zero_id, "Zero"))
        self.assertIsNone(parse_discord_transit(hold2, self.zero_id, "Zero"))

    def test_reject_syntax_template_documentation(self):
        doc1 = "• Trade: `BUY <qty> <good> @ <price> AT <station>` (e.g. `BUY 50 FOOD @ 32 AT CERES`)"
        self.assertIsNone(parse_discord_trade(doc1, self.zero_id, "Zero"))

        doc2 = "• Transit: MOVE TO <station> WITH <qty> <good>"
        self.assertIsNone(parse_discord_transit(doc2, self.zero_id, "Zero"))

        doc3 = "• Peer Trades: OFFER <qty> <good> @ <price> AT <station>"
        self.assertIsNone(parse_discord_peer(doc3, self.zero_id, "Zero"))

    def test_reject_transit_narratives_and_handoffs(self):
        trans1 = "- **MOVE TO EARTH WITH 250 FRAG:** dispatched (`tx-marvin-1790958498790343598`). The route came back as **4 rounds**."
        self.assertIsNone(parse_discord_transit(trans1, self.marvin_id, "Marvin"))

        trans2 = "Flight Corridor: Ceres ➔ Earth (tx-zero-1790958410). Absorbed a minor 36-unit hazard breach."
        self.assertIsNone(parse_discord_transit(trans2, self.zero_id, "Zero"))

        trans3 = "amos/1 is in transit to Luna with 250 FOOD and arrives in round 6576"
        self.assertIsNone(parse_discord_transit(trans3, self.amos_id, "Amos"))

    def test_accept_valid_imperative_trades(self):
        # Bare trade
        t1 = parse_discord_trade("BUY 50 FOOD @ 32 AT CERES", self.ryan_id, "Ryan")
        self.assertIsNotNone(t1)
        self.assertEqual(t1, {
            "action": "trade",
            "agent_id": "zero",
            "side": "bid",
            "qty": 50,
            "limit_price": 32,
            "instrument": "FOOD",
            "station_id": "ceres"
        })

        # Emoji prefix + Sell
        t2 = parse_discord_trade("🍌 SELL 214 ORE @ 26 AT EARTH", self.zero_id, "Zero")
        self.assertIsNotNone(t2)
        self.assertEqual(t2, {
            "action": "trade",
            "agent_id": "zero",
            "side": "ask",
            "qty": 214,
            "limit_price": 26,
            "instrument": "ORE",
            "station_id": "earth"
        })

        # Equity stock buy
        t3 = parse_discord_trade("BUY 10 EQ_ZERO @ 30", self.amos_id, "Amos")
        self.assertIsNotNone(t3)
        self.assertEqual(t3["instrument"], "EQ_ZERO")
        self.assertEqual(t3["station_id"], "ceres")

        # Tag mention prefix
        t4 = parse_discord_trade("<@1547763904141070346> BUY 100 FUEL @ 12 AT MARS", self.marvin_id, "Marvin")
        self.assertIsNotNone(t4)
        self.assertEqual(t4["agent_id"], "marvin")
        self.assertEqual(t4["instrument"], "FUEL")

    def test_accept_valid_imperative_transits(self):
        # Basic transit
        tr1 = parse_discord_transit("MOVE TO MARS WITH 100 FOOD", self.ryan_id, "Ryan")
        self.assertIsNotNone(tr1)
        self.assertEqual(tr1, {
            "action": "transit",
            "agent_id": "zero",
            "destination": "mars",
            "commodity": "FOOD",
            "cargo_qty": 100,
            "escort": False
        })

        # With vessel specification
        tr2 = parse_discord_transit("🍌 MOVE TO EARTH WITH 250 FRAG ON marvin/1", self.marvin_id, "Marvin")
        self.assertIsNotNone(tr2)
        self.assertEqual(tr2["destination"], "earth")
        self.assertEqual(tr2["vessel_id"], "marvin/1")
        self.assertEqual(tr2["cargo_qty"], 250)

        # With escort
        tr3 = parse_discord_transit("MOVE TO CERES WITH ESCORT", self.amos_id, "Amos")
        self.assertIsNotNone(tr3)
        self.assertTrue(tr3["escort"])

    @patch("tools.agora_announcer.fetch_discord_messages")
    @patch("tools.agora_announcer.submit_trade_to_referee")
    def test_poll_and_execute_trades_suppresses_handoff_blocks(self, mock_submit, mock_fetch):
        # Mock Discord message containing recap and handoff JSON
        mock_msg = {
            "id": "123456789",
            "author": {
                "id": self.zero_id,
                "username": "Zero"
            },
            "content": (
                "🍌 Round 6576: `zero/1` touched down at Kennedy Orbital Elevator.\n\n"
                "- **ORE Liquidated:** Sold 214 ORE at 26 CR (+5,564 CR).\n"
                "- **Hold Status:** Cargo hold empty.\n\n"
                "```handoff\n"
                "{\n"
                "  \"v\": 1.1,\n"
                "  \"evidence\": [\n"
                "    {\"note\": \"filled sell 214 ORE @ 26 CR at earth\"}\n"
                "  ]\n"
                "}\n"
                "```"
            )
        }
        mock_fetch.return_value = [mock_msg]

        processed_ids = set()
        newest = poll_and_execute_trades("dummy-ch", "bot-tok", "ref-tok", "earth", processed_ids, "0")

        self.assertEqual(newest, "123456789")
        self.assertIn("123456789", processed_ids)
        # Crucial verification: submit_trade_to_referee must NOT be called for handoff recap
        mock_submit.assert_not_called()

    def test_accept_backtick_wrapped_orders_and_identifiers(self):
        # Whole-line backtick trade (Marvin finding)
        t1 = parse_discord_trade("`BUY 50 FOOD @ 32`", self.zero_id)
        self.assertIsNotNone(t1)
        self.assertEqual(t1["action"], "trade")
        self.assertEqual(t1["qty"], 50)
        self.assertEqual(t1["instrument"], "FOOD")
        self.assertEqual(t1["limit_price"], 32)

        # Whole-line backtick transit (Marvin finding)
        t2 = parse_discord_transit("`MOVE TO LUNA WITH 250 FOOD`", self.zero_id)
        self.assertIsNotNone(t2)
        self.assertEqual(t2["destination"], "luna")
        self.assertEqual(t2["commodity"], "FOOD")
        self.assertEqual(t2["cargo_qty"], 250)

        # Ship id wrapped in backticks (Amos finding)
        t3 = parse_discord_transit("MOVE TO EARTH WITH 250 ORE ON `amos/2`", "1468012353206354197")
        self.assertIsNotNone(t3)
        self.assertEqual(t3["destination"], "earth")
        self.assertEqual(t3["commodity"], "ORE")
        self.assertEqual(t3["cargo_qty"], 250)
        self.assertEqual(t3["vessel_id"], "amos/2")


if __name__ == "__main__":
    unittest.main()
