import unittest

from agora.referee import AgoraReferee
from tools.agora_announcer import (
    FLEET_PATTERN,
    SHIP_BUY_PATTERN,
    TRANSFER_PATTERN,
    parse_discord_fleet_cmd,
    parse_discord_ship_buy_cmd,
    parse_discord_transfer_cmd,
    parse_discord_transit,
    format_fleet_roster
)


class TestDiscordFleet(unittest.TestCase):
    def test_regex_matching(self):
        # !fleet / !vessels
        self.assertIsNotNone(FLEET_PATTERN.search("!fleet"))
        self.assertIsNotNone(FLEET_PATTERN.search("!vessels"))
        self.assertIsNotNone(FLEET_PATTERN.search("!fleet as amos"))
        self.assertIsNotNone(FLEET_PATTERN.search("!vessels zero"))

        # !ship buy / !buy ship
        self.assertIsNotNone(SHIP_BUY_PATTERN.search("!ship buy"))
        self.assertIsNotNone(SHIP_BUY_PATTERN.search("!buy ship"))
        self.assertIsNotNone(SHIP_BUY_PATTERN.search("!ship buy at zero/1"))
        self.assertIsNotNone(SHIP_BUY_PATTERN.search("!ship buy as marvin"))

        # !transfer
        self.assertIsNotNone(TRANSFER_PATTERN.search("!transfer 50 FRAG from 1 to 2"))
        self.assertIsNotNone(TRANSFER_PATTERN.search("!transfer 50 FRAG 1 2"))
        self.assertIsNotNone(TRANSFER_PATTERN.search("!transfer 1 to 2 50 FRAG"))
        self.assertIsNotNone(TRANSFER_PATTERN.search("!transfer 1 2 50 FRAG"))
        self.assertIsNotNone(TRANSFER_PATTERN.search("!transfer zero/1 to zero/2 50 FRAG"))
        self.assertIsNotNone(TRANSFER_PATTERN.search("!transfer 100 FUEL from zero/1 to zero/2"))
        self.assertIsNotNone(TRANSFER_PATTERN.search("TRANSFER 25 FOOD @ceres 1"))

    def test_parse_discord_fleet_cmd(self):
        cmd = parse_discord_fleet_cmd("!fleet", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "fleet")
        self.assertEqual(cmd["agent_id"], "zero")

        cmd_override = parse_discord_fleet_cmd("!fleet as amos", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd_override)
        self.assertEqual(cmd_override["agent_id"], "amos")

    def test_parse_discord_ship_buy_cmd(self):
        cmd = parse_discord_ship_buy_cmd("!ship buy", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "buy_ship")
        self.assertEqual(cmd["agent_id"], "zero")

        cmd_vessel = parse_discord_ship_buy_cmd("!ship buy at zero/1 as amos", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd_vessel)
        self.assertEqual(cmd_vessel["agent_id"], "amos")
        self.assertEqual(cmd_vessel["at_vessel"], "zero/1")

    def test_parse_discord_transfer_cmd(self):
        # Format 1: !transfer 50 FRAG from 1 to 2
        cmd1 = parse_discord_transfer_cmd("!transfer 50 FRAG from 1 to 2", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd1)
        self.assertEqual(cmd1["action"], "transfer")
        self.assertEqual(cmd1["agent_id"], "zero")
        self.assertEqual(cmd1["from"], "1")
        self.assertEqual(cmd1["to"], "2")
        self.assertEqual(cmd1["instrument"], "FRAG")
        self.assertEqual(cmd1["qty"], 50)

        # Format 2: !transfer zero/1 to zero/2 100 FUEL
        cmd2 = parse_discord_transfer_cmd("!transfer zero/1 to zero/2 100 FUEL", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd2)
        self.assertEqual(cmd2["from"], "zero/1")
        self.assertEqual(cmd2["to"], "zero/2")
        self.assertEqual(cmd2["instrument"], "FUEL")
        self.assertEqual(cmd2["qty"], 100)

        # Format 3: !transfer @ceres 1 25 FOOD
        cmd3 = parse_discord_transfer_cmd("!transfer @ceres 1 25 FOOD as amos", "123", "User")
        self.assertIsNotNone(cmd3)
        self.assertEqual(cmd3["agent_id"], "amos")
        self.assertEqual(cmd3["from"], "@ceres")
        self.assertEqual(cmd3["to"], "1")
        self.assertEqual(cmd3["instrument"], "FOOD")
        self.assertEqual(cmd3["qty"], 25)

    def test_parse_discord_transit_multiship(self):
        # Default single-ship fallback
        t1 = parse_discord_transit("!transit CERES", "1542081375287640084", "Zero")
        self.assertIsNotNone(t1)
        self.assertEqual(t1["destination"], "ceres")
        self.assertIsNone(t1.get("vessel_id"))

        # Explicit vessel number
        t2 = parse_discord_transit("!transit CERES 2", "1542081375287640084", "Zero")
        self.assertIsNotNone(t2)
        self.assertEqual(t2["destination"], "ceres")
        self.assertEqual(t2.get("vessel_id"), "2")

        # Full vessel tag
        t3 = parse_discord_transit("!transit CERES zero/2", "1542081375287640084", "Zero")
        self.assertIsNotNone(t3)
        self.assertEqual(t3["destination"], "ceres")
        self.assertEqual(t3.get("vessel_id"), "zero/2")

        # With cargo and vessel shorthand
        t4 = parse_discord_transit("!transit CERES with 50 FRAG 2", "1542081375287640084", "Zero")
        self.assertIsNotNone(t4)
        self.assertEqual(t4["destination"], "ceres")
        self.assertEqual(t4["cargo_qty"], 50)
        self.assertEqual(t4["commodity"], "FRAG")
        self.assertEqual(t4.get("vessel_id"), "2")

        # With cargo and ON vessel syntax
        t5 = parse_discord_transit("!transit CERES on 2 with 50 FRAG", "1542081375287640084", "Zero")
        self.assertIsNotNone(t5)
        self.assertEqual(t5["destination"], "ceres")
        self.assertEqual(t5["cargo_qty"], 50)
        self.assertEqual(t5["commodity"], "FRAG")
        self.assertEqual(t5.get("vessel_id"), "2")

    def test_format_fleet_roster(self):
        sample_fleet = {
            "status": "ok",
            "fleet": {
                "owned": 2,
                "ship_cap": 5,
                "max_ships": 5,
                "upkeep_per_round": 300,
                "book_value": 12500,
                "next_ship": {"hull": 3, "price": 40000},
                "ships": [
                    {
                        "vessel_id": "zero/1",
                        "station_id": "ceres",
                        "status": "docked",
                        "hold_used": 500,
                        "hold_capacity": 1000,
                        "hold": {"FRAG": 500, "FUEL": 300},
                        "fuel": 300
                    },
                    {
                        "vessel_id": "zero/2",
                        "station_id": "luna",
                        "status": "in_transit",
                        "hold_used": 0,
                        "hold_capacity": 1000,
                        "hold": {"FUEL": 150},
                        "fuel": 150,
                        "location": {
                            "transit": {
                                "origin": "ceres",
                                "destination": "luna",
                                "arrival_round": 4
                            }
                        }
                    }
                ]
            }
        }
        res = format_fleet_roster(sample_fleet, "zero")
        self.assertIn("Fleet Roster", res)
        self.assertIn("zero/1", res)
        self.assertIn("zero/2", res)
        self.assertIn("300 CR/round", res)
        self.assertIn("#3 for 40,000 CR", res)
        self.assertLess(len(res), 2000)

    def test_referee_fleet_multiship_lifecycle(self):
        ref = AgoraReferee()
        ref.new_game(seed=42, warmup_rounds=2)

        # Fund zero with cash via double-entry ledger move
        with ref.lock, ref.conn:
            ref.fleet._move("test-fund", (('zero', 'CR', 100000), ('SYSTEM', 'CR', -100000)))

        # 1. Buy Ship 2
        buy_res = ref.fleet.buy("zero")
        self.assertEqual(buy_res["kind"], "ship_bought")
        self.assertEqual(buy_res["payload"]["vessel_id"], "zero/2")
        self.assertEqual(buy_res["payload"]["ships"], 2)
        self.assertEqual(buy_res["payload"]["upkeep_per_round"], 300)

        # 2. Transfer FUEL between zero/1 and zero/2 at Ceres
        trans_res = ref.fleet.transfer("zero", "zero/1", "zero/2", "FUEL", 100)
        self.assertEqual(trans_res["kind"], "transfer_ok")
        self.assertEqual(trans_res["payload"]["from"], "zero/1")
        self.assertEqual(trans_res["payload"]["to"], "zero/2")
        self.assertEqual(trans_res["payload"]["qty"], 100)

        # 3. Fly zero/2 to Luna
        transit_res = ref.initiate_transit("zero", "luna", vessel_id="zero/2")
        self.assertEqual(transit_res["kind"], "status")
        self.assertEqual(transit_res["payload"]["vessel_id"], "zero/2")
        self.assertEqual(transit_res["payload"]["destination"], "luna")

        # 4. Attempt transfer while zero/2 is in transit (should reject)
        rej_transit = ref.fleet.transfer("zero", "zero/1", "zero/2", "FUEL", 50)
        self.assertEqual(rej_transit["kind"], "reject")
        self.assertEqual(rej_transit["payload"]["reason"], "vessel_in_transit")

        # 5. Ledger invariants verify clean
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)


if __name__ == "__main__":
    unittest.main()
