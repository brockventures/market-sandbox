import os
import unittest
from agora.spatial import normalize_commodity, COMMODITY_ALIASES, COMMODITIES
from agora.referee import AgoraReferee
from agora.upgrades import CATALOG
from tools.agora_announcer import (
    UPGRADE_BUY_PATTERN,
    UPGRADES_STATUS_PATTERN,
    parse_discord_upgrade_buy_cmd,
    parse_discord_upgrades_cmd,
    parse_discord_trade,
    parse_discord_transit,
    parse_discord_peer,
    parse_discord_transfer_cmd,
    format_upgrades_catalog,
)


class TestDiscordUpgradesAndCommodities(unittest.TestCase):

    def test_commodity_normalization(self):
        self.assertEqual(normalize_commodity("ORGANICS"), "FOOD")
        self.assertEqual(normalize_commodity("organics"), "FOOD")
        self.assertEqual(normalize_commodity("bio"), "FOOD")
        self.assertEqual(normalize_commodity("HYDROPONICS"), "FOOD")
        self.assertEqual(normalize_commodity("banana"), "FRAG")
        self.assertEqual(normalize_commodity("BANANA"), "FRAG")
        self.assertEqual(normalize_commodity("FUEL"), "FUEL")
        self.assertEqual(normalize_commodity("ORE"), "ORE")
        self.assertEqual(normalize_commodity("FRAG"), "FRAG")
        self.assertEqual(normalize_commodity("FOOD"), "FOOD")

    def test_discord_announcer_commodity_trade_parsing(self):
        # Multi-commodity order commands (#75)
        trade_org = parse_discord_trade("!buy 10 organics at 15 in ceres", "179407724335988736", "Ryan")
        self.assertIsNotNone(trade_org)
        self.assertEqual(trade_org["instrument"], "FOOD")
        self.assertEqual(trade_org["qty"], 10)
        self.assertEqual(trade_org["limit_price"], 15)
        self.assertEqual(trade_org["station_id"], "ceres")

        trade_fuel = parse_discord_trade("!sell 25 fuel at 20 in earth as amos", "123", "User")
        self.assertIsNotNone(trade_fuel)
        self.assertEqual(trade_fuel["instrument"], "FUEL")
        self.assertEqual(trade_fuel["qty"], 25)
        self.assertEqual(trade_fuel["agent_id"], "amos")

        # Multi-commodity transit
        transit_org = parse_discord_transit("!transit mars with 40 organics as zero", "179407724335988736", "Ryan")
        self.assertIsNotNone(transit_org)
        self.assertEqual(transit_org["commodity"], "FOOD")
        self.assertEqual(transit_org["cargo_qty"], 40)
        self.assertEqual(transit_org["destination"], "mars")

        # Multi-commodity peer offer
        peer_org = parse_discord_peer("!offer 30 organics @ 22 at ceres as marvin", "123", "User")
        self.assertIsNotNone(peer_org)
        self.assertEqual(peer_org["instrument"], "FOOD")
        self.assertEqual(peer_org["qty"], 30)

        # Multi-commodity transfer
        xfer_org = parse_discord_transfer_cmd("!transfer 15 organics from amos/1 to amos/@ceres", "123", "User")
        self.assertIsNotNone(xfer_org)
        self.assertEqual(xfer_org["instrument"], "FOOD")
        self.assertEqual(xfer_org["qty"], 15)

    def test_discord_announcer_upgrade_command_parsing(self):
        # Buy upgrade
        cmd1 = parse_discord_upgrade_buy_cmd("!upgrade buy priority_slips", "179407724335988736", "Ryan")
        self.assertIsNotNone(cmd1)
        self.assertEqual(cmd1["kind"], "priority_slips")
        self.assertEqual(cmd1["agent_id"], "zero")

        cmd2 = parse_discord_upgrade_buy_cmd("!buy upgrade bulk_storage as amos", "123", "User")
        self.assertIsNotNone(cmd2)
        self.assertEqual(cmd2["kind"], "bulk_storage")
        self.assertEqual(cmd2["agent_id"], "amos")

        cmd3 = parse_discord_upgrade_buy_cmd("!upgrade buy refinery_loop", "93420059858305024", "Mike Carmody")
        self.assertIsNotNone(cmd3)
        self.assertEqual(cmd3["kind"], "refinery_loop")
        self.assertEqual(cmd3["agent_id"], "amos")

        # Query upgrades catalog
        q1 = parse_discord_upgrades_cmd("!upgrades", "179407724335988736", "Ryan")
        self.assertIsNotNone(q1)
        self.assertEqual(q1["agent_id"], "zero")

        q2 = parse_discord_upgrades_cmd("!upgrades as marvin", "123", "User")
        self.assertIsNotNone(q2)
        self.assertEqual(q2["agent_id"], "marvin")

        # Not triggered by other commands
        self.assertIsNone(parse_discord_upgrade_buy_cmd("!fleet"))
        self.assertIsNone(parse_discord_upgrades_cmd("!upgrade buy bulk_storage"))

    def test_format_upgrades_catalog(self):
        sample_data = {
            "catalog": [
                {
                    "kind": "priority_slips",
                    "what": "waives all docked idle fees",
                    "tier_detail": [{"tier": 1, "price": 8000, "unlock_round": 25, "locked": False}]
                },
                {
                    "kind": "bulk_storage",
                    "what": "increases ship hold capacity by +500 cargo units",
                    "tier_detail": [{"tier": 1, "price": 10000, "unlock_round": 50, "locked": True}]
                }
            ],
            "holdings": {"zero": {"priority_slips": 1}}
        }
        text = format_upgrades_catalog(sample_data, "zero")
        self.assertIn("PRIORITY_SLIPS", text)
        self.assertIn("T1: ✅ Fitted", text)
        self.assertIn("BULK_STORAGE", text)
        self.assertIn("T1: 🔒 R#50", text)

    def test_referee_infrastructure_upgrades_mechanics(self):
        # Initialize referee with upgrades enabled
        os.environ["AGORA_UPGRADES"] = "1"
        ref = AgoraReferee(db_path=":memory:")
        ref.reset_to_genesis(depots=True)
        ref.upgrades_enabled = True

        # Verify new upgrades exist in catalog
        self.assertIn("priority_slips", CATALOG)
        self.assertIn("bulk_storage", CATALOG)
        self.assertIn("refinery_loop", CATALOG)

        # Check catalog attributes
        self.assertEqual(CATALOG["priority_slips"]["prices"], [8_000])
        self.assertEqual(CATALOG["priority_slips"]["unlocks"], [25])
        self.assertEqual(CATALOG["bulk_storage"]["prices"], [10_000])
        self.assertEqual(CATALOG["bulk_storage"]["unlocks"], [50])
        self.assertEqual(CATALOG["refinery_loop"]["prices"], [12_000])
        self.assertEqual(CATALOG["refinery_loop"]["unlocks"], [80])

        # Test purchase unlocking
        ref.current_round = 10
        res_early = ref.upgrades.buy("zero", "priority_slips")
        self.assertEqual(res_early.get("kind"), "reject")
        self.assertEqual(res_early["payload"]["reason"], "upgrade_locked")

        # Advance to round 25 to unlock priority_slips
        ref.current_round = 25
        # Give zero cash
        ref.conn.execute("UPDATE accounts SET balance = balance + 50000 WHERE agent_id = 'zero' AND instrument = 'CR'")
        res_buy = ref.upgrades.buy("zero", "priority_slips")
        self.assertEqual(res_buy.get("kind"), "upgrade_ok")
        self.assertTrue(ref.upgrades.has_priority_slips("zero"))

        # Test idle fee exemption with priority_slips (#167)
        ref.idle_fee = 100
        ref._active_this_round = set()
        # Mark another fleet active so idle fee evaluates
        ref.conn.execute("INSERT OR IGNORE INTO fleet_roster (agent_id, home_station, genesis_cr, genesis_frag, genesis_fuel) VALUES ('other_agent', 'ceres', 1000, 0, 0)")
        ref._active_this_round.add("other_agent")
        # Ensure zero is docked and has cash
        ref.conn.execute("UPDATE accounts SET balance = 1000 WHERE agent_id = 'zero' AND instrument = 'CR'")
        charged = ref._charge_idle_fees_locked(26)
        self.assertNotIn("zero", charged)

        # Test bulk_storage hold capacity bonus (#167)
        ref.ship_hold = 1000
        base_cap = ref.fleet.capacity("zero/1")
        self.assertEqual(base_cap, 1000)

        ref.current_round = 50
        ref.conn.execute("UPDATE accounts SET balance = balance + 50000 WHERE agent_id = 'zero' AND instrument = 'CR'")
        res_bulk = ref.upgrades.buy("zero", "bulk_storage")
        self.assertEqual(res_bulk.get("kind"), "upgrade_ok")
        self.assertEqual(ref.upgrades.bulk_storage_bonus("zero"), 500)
        boosted_cap = ref.fleet.capacity("zero/1")
        self.assertEqual(boosted_cap, 1500)

        # Test refinery_loop propellant reduction (#167)
        ref.current_round = 80
        burn_normal = ref.upgrades.engine_fuel("zero", 20)
        self.assertEqual(burn_normal, 20)

        res_refinery = ref.upgrades.buy("zero", "refinery_loop")
        self.assertEqual(res_refinery.get("kind"), "upgrade_ok")
        self.assertTrue(ref.upgrades.has_refinery_loop("zero"))
        burn_refinery = ref.upgrades.engine_fuel("zero", 20)
        # 20 * 0.80 = 16
        self.assertEqual(burn_refinery, 16)

    def test_referee_multi_commodity_order_normalization(self):
        # Order submission with ORGANICS maps to FOOD book (#75)
        ref = AgoraReferee(db_path=":memory:")
        ref.reset_to_genesis(depots=True)

        ref.conn.execute("UPDATE accounts SET balance = 50000 WHERE agent_id = 'zero' AND instrument = 'CR'")
        ref.conn.execute("UPDATE accounts SET balance = 500 WHERE agent_id = 'zero' AND instrument = 'FOOD'")

        order_env = {
            "v": 1,
            "kind": "order",
            "payload": {
                "order_id": "ord-test-org-1",
                "agent_id": "zero",
                "side": "bid",
                "qty": 5,
                "limit_price": 10,
                "instrument": "ORGANICS",
                "station_id": "ceres",
                "vessel_id": "zero/1"
            }
        }
        res = ref.submit_envelope(order_env)
        self.assertEqual(res.get("kind"), "market_tick")
        self.assertEqual(res["payload"]["instrument"], "FOOD")
        self.assertEqual(res["payload"]["order_id"], "ord-test-org-1")


if __name__ == "__main__":
    unittest.main()
