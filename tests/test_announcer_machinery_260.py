import unittest
from agora.spatial import COMMODITIES, COMMODITY_ALIASES, normalize_commodity
from tools.agora_announcer import (
    TRADE_PATTERN,
    TRANSFER_PATTERN,
    parse_discord_trade,
    parse_discord_transfer_cmd,
    format_final_standings,
    build_burst_kickoff,
    build_announcement,
)


class TestAnnouncerMachinery(unittest.TestCase):
    def test_trade_pattern_parses_all_commodities_and_aliases(self):
        """Verify that every entry in COMMODITIES and COMMODITY_ALIASES matches TRADE_PATTERN."""
        all_terms = sorted(set(COMMODITIES) | set(COMMODITY_ALIASES.keys()))
        for term in all_terms:
            cmd = f"BUY 50 {term} @ 20 AT MARS"
            m = TRADE_PATTERN.search(cmd)
            self.assertIsNotNone(m, f"TRADE_PATTERN failed to match '{cmd}'")
            side, qty, commodity, price, station = m.groups()
            self.assertEqual(side.upper(), "BUY")
            self.assertEqual(qty, "50")
            self.assertEqual(commodity.upper(), term.upper())
            self.assertEqual(price, "20")
            self.assertEqual(station.upper(), "MARS")

            # Test sell without station
            cmd_sell = f"SELL 10 {term} @ 30"
            m_sell = TRADE_PATTERN.search(cmd_sell)
            self.assertIsNotNone(m_sell, f"TRADE_PATTERN failed to match '{cmd_sell}'")
            side_s, qty_s, comm_s, price_s, station_s = m_sell.groups()
            self.assertEqual(side_s.upper(), "SELL")
            self.assertEqual(qty_s, "10")
            self.assertEqual(comm_s.upper(), term.upper())
            self.assertEqual(price_s, "30")

    def test_transfer_pattern_parses_all_commodities_and_aliases(self):
        """Verify that every entry in COMMODITIES and COMMODITY_ALIASES matches TRANSFER_PATTERN."""
        all_terms = sorted(set(COMMODITIES) | set(COMMODITY_ALIASES.keys()))
        for term in all_terms:
            cmd = f"!transfer 10 {term} from alpha to beta"
            m = TRANSFER_PATTERN.search(cmd)
            self.assertIsNotNone(m, f"TRANSFER_PATTERN failed to match '{cmd}'")
            g = m.groups()
            self.assertEqual(g[0], "10")
            self.assertEqual(g[1].upper(), term.upper())
            self.assertEqual(g[2], "alpha")
            self.assertEqual(g[3], "beta")

    def test_parse_discord_trade_machinery_and_aliases(self):
        """Verify parse_discord_trade normalizes machinery and its aliases."""
        res_mach = parse_discord_trade("BUY 50 MACHINERY @ 20 AT MARS", author_id="179407724335988736", author_name="Ryan")
        self.assertIsNotNone(res_mach)
        self.assertEqual(res_mach["instrument"], "MACHINERY")
        self.assertEqual(res_mach["qty"], 50)
        self.assertEqual(res_mach["limit_price"], 20)
        self.assertEqual(res_mach["station_id"], "mars")

        res_parts = parse_discord_trade("SELL 15 PARTS @ 35 AT CERES", author_id="179407724335988736", author_name="Ryan")
        self.assertIsNotNone(res_parts)
        self.assertEqual(res_parts["instrument"], "MACHINERY")
        self.assertEqual(res_parts["qty"], 15)
        self.assertEqual(res_parts["limit_price"], 35)
        self.assertEqual(res_parts["station_id"], "ceres")

        res_tech = parse_discord_trade("BUY 5 TECH @ 40 AT EARTH", author_id="179407724335988736", author_name="Ryan")
        self.assertIsNotNone(res_tech)
        self.assertEqual(res_tech["instrument"], "MACHINERY")

    def test_parse_discord_transfer_machinery(self):
        """Verify parse_discord_transfer_cmd normalizes machinery."""
        res = parse_discord_transfer_cmd("!transfer 25 PARTS from hold_a to hold_b", author_id="179407724335988736", author_name="Ryan")
        self.assertIsNotNone(res)
        self.assertEqual(res["instrument"], "MACHINERY")
        self.assertEqual(res["qty"], 25)
        self.assertEqual(res["from"], "hold_a")
        self.assertEqual(res["to"], "hold_b")

    def test_holdings_and_help_text_contain_machinery(self):
        """Verify standings and help texts include MACHINERY."""
        leaderboard_data = {
            "leaderboard": [{
                "agent_id": "zero",
                "balance": {"CR": 1000, "FRAG": 5, "FUEL": 10, "FOOD": 20, "ORE": 30, "MACHINERY": 40},
                "mtm_net_worth": 5000,
            }]
        }
        standings = format_final_standings(leaderboard_data)
        self.assertIn("40 MACHINERY", standings)

        kickoff = build_burst_kickoff("burst-123", rounds=8, interval_sec=180.0, start_round=0)
        self.assertIn("MACHINERY", kickoff)


if __name__ == "__main__":
    unittest.main()
