import unittest

from tools.agora_announcer import (
    parse_discord_ship_buy_cmd,
    parse_discord_transfer_cmd,
    parse_discord_privateer_cmd,
    parse_discord_piracy_respond_cmd,
    parse_discord_upgrade_buy_cmd,
    parse_discord_peer,
    parse_discord_transit,
    parse_discord_trade,
    parse_discord_contract_cmd,
    parse_discord_fleet_cmd,
    parse_discord_upgrades_cmd,
    parse_discord_piracy_status_cmd,
    parse_discord_hazards_cmd,
    AUTHOR_MAP,
)


class TestAnnouncerSecurity259(unittest.TestCase):
    """Regression test suite for Issue #259:
    Ensure mutating Discord commands strictly resolve acting corp from Discord author ID
    and reject unmapped authors, preventing impersonation via 'as <corp>'.
    """

    def setUp(self):
        # Mapped author: Amos
        self.amos_id = "1468012353206354197"
        # Mapped author: Zero
        self.zero_id = "179407724335988736"
        # Mapped author: Aerial
        self.aerial_id = "1542035925603713086"
        # Unmapped author
        self.unmapped_id = "999999999999999999"

    def test_ship_buy_security(self):
        # 1. Unmapped author is rejected
        res_unauth = parse_discord_ship_buy_cmd("!ship buy as amos", self.unmapped_id, "Attacker")
        self.assertIsNotNone(res_unauth)
        self.assertEqual(res_unauth["action"], "unauthorized")

        # 2. Mapped author with 'as <other>' acts strictly as author
        res_mapped = parse_discord_ship_buy_cmd("!ship buy as amos", self.zero_id, "ZeroUser")
        self.assertIsNotNone(res_mapped)
        self.assertEqual(res_mapped["action"], "buy_ship")
        self.assertEqual(res_mapped["agent_id"], "zero")

    def test_transfer_security(self):
        # 1. Unmapped author is rejected
        res_unauth = parse_discord_transfer_cmd("!transfer 50 FRAG from 1 to 2 as zero", self.unmapped_id, "Attacker")
        self.assertIsNotNone(res_unauth)
        self.assertEqual(res_unauth["action"], "unauthorized")

        # 2. Mapped author with 'as <other>' acts strictly as author
        res_mapped = parse_discord_transfer_cmd("!transfer 50 FRAG from 1 to 2 as zero", self.amos_id, "AmosUser")
        self.assertIsNotNone(res_mapped)
        self.assertEqual(res_mapped["action"], "transfer")
        self.assertEqual(res_mapped["agent_id"], "amos")

    def test_privateer_security(self):
        # 1. Unmapped author is rejected
        res_unauth = parse_discord_privateer_cmd("!privateer amos 10 as aerial", self.unmapped_id, "Attacker")
        self.assertIsNotNone(res_unauth)
        self.assertEqual(res_unauth["action"], "unauthorized")

        # 2. Mapped author with 'as <other>' acts strictly as author
        res_mapped = parse_discord_privateer_cmd("!privateer amos 10 as aerial", self.zero_id, "ZeroUser")
        self.assertIsNotNone(res_mapped)
        self.assertEqual(res_mapped["action"], "privateer")
        self.assertEqual(res_mapped["sponsor"], "zero")

    def test_piracy_respond_security(self):
        # 1. Unmapped author is rejected
        res_unauth = parse_discord_piracy_respond_cmd("!respond tx-123 pay as zero", self.unmapped_id, "Attacker")
        self.assertIsNotNone(res_unauth)
        self.assertEqual(res_unauth["action"], "unauthorized")

        # 2. Mapped author with 'as <other>' acts strictly as author
        res_mapped = parse_discord_piracy_respond_cmd("!respond tx-123 pay as zero", self.amos_id, "AmosUser")
        self.assertIsNotNone(res_mapped)
        self.assertEqual(res_mapped["action"], "piracy_respond")
        self.assertEqual(res_mapped["agent_id"], "amos")

    def test_upgrade_buy_security(self):
        # 1. Unmapped author is rejected
        res_unauth = parse_discord_upgrade_buy_cmd("!upgrade buy priority_slips as amos", self.unmapped_id, "Attacker")
        self.assertIsNotNone(res_unauth)
        self.assertEqual(res_unauth["action"], "unauthorized")

        # 2. Mapped author with 'as <other>' acts strictly as author
        res_mapped = parse_discord_upgrade_buy_cmd("!upgrade buy priority_slips as amos", self.zero_id, "ZeroUser")
        self.assertIsNotNone(res_mapped)
        self.assertEqual(res_mapped["action"], "upgrade_buy")
        self.assertEqual(res_mapped["agent_id"], "zero")

    def test_peer_trade_security(self):
        # Offer: unmapped rejected vs mapped author
        off_unauth = parse_discord_peer("!offer 10 FRAG @ 15 at ceres as zero", self.unmapped_id, "Attacker")
        self.assertIsNotNone(off_unauth)
        self.assertEqual(off_unauth["action"], "unauthorized")

        off_mapped = parse_discord_peer("!offer 10 FRAG @ 15 at ceres as zero", self.amos_id, "AmosUser")
        self.assertIsNotNone(off_mapped)
        self.assertEqual(off_mapped["action"], "offer")
        self.assertEqual(off_mapped["agent_id"], "amos")

        # Accept: unmapped rejected vs mapped author
        acc_unauth = parse_discord_peer("!accept esc-123 as aerial", self.unmapped_id, "Attacker")
        self.assertIsNotNone(acc_unauth)
        self.assertEqual(acc_unauth["action"], "unauthorized")

        acc_mapped = parse_discord_peer("!accept esc-123 as aerial", self.zero_id, "ZeroUser")
        self.assertIsNotNone(acc_mapped)
        self.assertEqual(acc_mapped["action"], "accept")
        self.assertEqual(acc_mapped["agent_id"], "zero")

        # Cancel: unmapped rejected vs mapped author
        can_unauth = parse_discord_peer("!cancel esc-123 as amos", self.unmapped_id, "Attacker")
        self.assertIsNotNone(can_unauth)
        self.assertEqual(can_unauth["action"], "unauthorized")

        can_mapped = parse_discord_peer("!cancel esc-123 as amos", self.aerial_id, "AerialUser")
        self.assertIsNotNone(can_mapped)
        self.assertEqual(can_mapped["action"], "cancel")
        self.assertEqual(can_mapped["agent_id"], "aerial")

    def test_transit_security(self):
        # 1. Unmapped author is rejected
        res_unauth = parse_discord_transit("MOVE TO MARS WITH 50 FOOD as zero", self.unmapped_id, "Attacker")
        self.assertIsNotNone(res_unauth)
        self.assertEqual(res_unauth["action"], "unauthorized")

        # 2. Mapped author with 'as <other>' acts strictly as author
        res_mapped = parse_discord_transit("MOVE TO MARS WITH 50 FOOD as zero", self.amos_id, "AmosUser")
        self.assertIsNotNone(res_mapped)
        self.assertEqual(res_mapped["action"], "transit")
        self.assertEqual(res_mapped["agent_id"], "amos")

    def test_trade_security(self):
        # 1. Unmapped author is rejected
        res_unauth = parse_discord_trade("BUY 25 FOOD @ 30 AT CERES as amos", self.unmapped_id, "Attacker")
        self.assertIsNotNone(res_unauth)
        self.assertEqual(res_unauth["action"], "unauthorized")

        # 2. Mapped author with 'as <other>' acts strictly as author
        res_mapped = parse_discord_trade("BUY 25 FOOD @ 30 AT CERES as amos", self.zero_id, "ZeroUser")
        self.assertIsNotNone(res_mapped)
        self.assertEqual(res_mapped["action"], "trade")
        self.assertEqual(res_mapped["agent_id"], "zero")

    def test_contract_escrow_security(self):
        # Claim
        cl_unauth = parse_discord_contract_cmd("!claim ct-ceres-1 as zero", self.unmapped_id, "Attacker")
        self.assertIsNotNone(cl_unauth)
        self.assertEqual(cl_unauth["action"], "unauthorized")

        cl_mapped = parse_discord_contract_cmd("!claim ct-ceres-1 as zero", self.amos_id, "AmosUser")
        self.assertIsNotNone(cl_mapped)
        self.assertEqual(cl_mapped["action"], "claim")
        self.assertEqual(cl_mapped["agent_id"], "amos")

        # Deliver
        dl_unauth = parse_discord_contract_cmd("!deliver ct-ceres-1 50 as zero", self.unmapped_id, "Attacker")
        self.assertIsNotNone(dl_unauth)
        self.assertEqual(dl_unauth["action"], "unauthorized")

        dl_mapped = parse_discord_contract_cmd("!deliver ct-ceres-1 50 as zero", self.amos_id, "AmosUser")
        self.assertIsNotNone(dl_mapped)
        self.assertEqual(dl_mapped["action"], "deliver")
        self.assertEqual(dl_mapped["agent_id"], "amos")

    def test_read_only_commands_retain_target_argument(self):
        # !fleet allows querying another corp
        f_query = parse_discord_fleet_cmd("!fleet as amos", self.unmapped_id, "Anyone")
        self.assertIsNotNone(f_query)
        self.assertEqual(f_query["action"], "fleet")
        self.assertEqual(f_query["agent_id"], "amos")

        # !upgrades allows querying another corp's catalog
        u_query = parse_discord_upgrades_cmd("!upgrades as marvin", self.unmapped_id, "Anyone")
        self.assertIsNotNone(u_query)
        self.assertEqual(u_query["agent_id"], "marvin")

        # !piracy status is read-only
        p_query = parse_discord_piracy_status_cmd("!piracy as zero", self.unmapped_id, "Anyone")
        self.assertIsNotNone(p_query)
        self.assertEqual(p_query["action"], "piracy_status")
        self.assertEqual(p_query["agent_id"], "zero")

        # !hazards status is read-only
        h_query = parse_discord_hazards_cmd("!hazards as aerial", self.unmapped_id, "Anyone")
        self.assertIsNotNone(h_query)
        self.assertEqual(h_query["action"], "hazards_status")
        self.assertEqual(h_query["agent_id"], "aerial")


if __name__ == "__main__":
    unittest.main()
