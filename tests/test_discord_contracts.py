import unittest

from agora.referee import AgoraReferee
from tools.agora_announcer import (
    parse_discord_contract_cmd,
    format_contracts_list,
    CONTRACTS_PATTERN,
    CONTRACT_CLAIM_PATTERN,
    CONTRACT_DELIVER_PATTERN
)


class TestDiscordContracts(unittest.TestCase):
    def test_regex_matching(self):
        # !contracts commands
        m = CONTRACTS_PATTERN.search("!contracts")
        self.assertIsNotNone(m)
        self.assertIsNone(m.group(1))

        m = CONTRACTS_PATTERN.search("!contracts ceres")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).lower(), "ceres")

        m = CONTRACTS_PATTERN.search("!contracts my")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).lower(), "my")

        m = CONTRACTS_PATTERN.search("!mycontracts")
        self.assertIsNotNone(m)

        # !claim commands
        m = CONTRACT_CLAIM_PATTERN.search("!claim ct-mars-4-1")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "ct-mars-4-1")

        m = CONTRACT_CLAIM_PATTERN.search("CLAIM CONTRACT ct-earth-8-2")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "ct-earth-8-2")

        # !deliver commands
        m = CONTRACT_DELIVER_PATTERN.search("!deliver ct-mars-4-1")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "ct-mars-4-1")
        self.assertIsNone(m.group(2))

        m = CONTRACT_DELIVER_PATTERN.search("!deliver ct-mars-4-1 250")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "ct-mars-4-1")
        self.assertEqual(m.group(2), "250")

        m = CONTRACT_DELIVER_PATTERN.search("!deliver ct-mars-4-1 200 vessel amos/1")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "ct-mars-4-1")
        self.assertEqual(m.group(2), "200")
        self.assertEqual(m.group(3), "amos/1")

    def test_parse_discord_contract_cmd(self):
        cmd = parse_discord_contract_cmd("!contracts", "1541205716948353074", "Amos")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "list")
        self.assertEqual(cmd["agent_id"], "amos")
        self.assertFalse(cmd["filter_my"])

        cmd = parse_discord_contract_cmd("!contracts my", "1542081375287640084", "Zero")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "list")
        self.assertEqual(cmd["agent_id"], "zero")
        self.assertTrue(cmd["filter_my"])

        cmd = parse_discord_contract_cmd("!claim ct-mars-4-1 as marvin", "123", "User")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "claim")
        self.assertEqual(cmd["agent_id"], "marvin")
        self.assertEqual(cmd["contract_id"], "ct-mars-4-1")

        cmd = parse_discord_contract_cmd("!deliver ct-mars-4-1 150 vessel amos/2", "1541205716948353074", "Amos")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "deliver")
        self.assertEqual(cmd["agent_id"], "amos")
        self.assertEqual(cmd["contract_id"], "ct-mars-4-1")
        self.assertEqual(cmd["qty"], 150)
        self.assertEqual(cmd["vessel_id"], "amos/2")

    def test_format_contracts_list(self):
        # Disabled contracts
        res = format_contracts_list({"contracts_enabled": False})
        self.assertIn("contracts are currently disabled", res)

        # Empty list
        res = format_contracts_list({"contracts_enabled": True, "contracts": []})
        self.assertIn("No active contracts", res)

        # Active contracts
        sample_data = {
            "contracts_enabled": True,
            "contracts": [
                {
                    "contract_id": "ct-mars-4-1",
                    "station_id": "mars",
                    "instrument": "FOOD",
                    "qty_total": 500,
                    "qty_remaining": 500,
                    "price": 28,
                    "posted_round": 4,
                    "deadline": 10,
                    "owner": None,
                    "bond": 3500
                },
                {
                    "contract_id": "ct-ceres-4-2",
                    "station_id": "ceres",
                    "instrument": "ORE",
                    "qty_total": 400,
                    "qty_remaining": 400,
                    "price": 35,
                    "posted_round": 4,
                    "deadline": 12,
                    "owner": "amos",
                    "bond": 3500
                }
            ]
        }
        res = format_contracts_list(sample_data)
        self.assertIn("ct-mars-4-1", res)
        self.assertIn("ct-ceres-4-2", res)
        self.assertIn("Available for Claim", res)
        self.assertIn("Owner: **Atlantean Paperclip Manufacturing [APM]**", res)

        # Filter by agent
        res_amos = format_contracts_list(sample_data, filter_agent="amos")
        self.assertNotIn("ct-mars-4-1", res_amos)
        self.assertIn("ct-ceres-4-2", res_amos)
        self.assertIn("Owned by Atlantean Paperclip Manufacturing [APM]", res_amos)

        # Filter by station
        res_mars = format_contracts_list(sample_data, filter_station="mars")
        self.assertIn("ct-mars-4-1", res_mars)
        self.assertNotIn("ct-ceres-4-2", res_mars)

    def test_referee_contract_claim_and_deliver_lifecycle(self):
        ref = AgoraReferee(contracts=True)
        ref.new_game(seed=42, warmup_rounds=2, contracts=True)

        with ref.lock:
            cid = ref.contract_desk._post_locked(1)

        c = ref.contract_desk.get(cid)
        self.assertIsNotNone(c)
        self.assertEqual(c["status"], "open")
        self.assertIsNone(c["owner"])

        # Give amos cash and dock at contract's station
        st = c["station_id"]
        comm = c["instrument"]
        with ref.lock, ref.conn:
            ref.contract_desk._move("test-cash", (('amos', 'CR', 50000), ('SYSTEM', 'CR', -50000)))
            ref.contract_desk._move("test-goods", (('amos/1', comm, 1000), ('SYSTEM', comm, -1000)))
            ref.conn.execute("UPDATE vessels SET station_id = ? WHERE vessel_id = 'amos/1'", (st,))

        # Claim
        cr_before = ref.get_balance("amos", "CR")
        claim_res = ref.contract_desk.claim("amos", cid)
        self.assertEqual(claim_res["kind"], "contract_claim_ok")
        c_claimed = claim_res["payload"]
        self.assertEqual(c_claimed["owner"], "amos")
        self.assertGreater(c_claimed["bond"], 0)
        self.assertEqual(ref.get_balance("amos", "CR"), cr_before - c_claimed["bond"])

        # Deliver partial (100 units)
        cr_before_deliver = ref.get_balance("amos", "CR")
        deliver_res = ref.contract_desk.deliver("amos", cid, qty=100)
        self.assertEqual(deliver_res["kind"], "contract_deliver_ok")
        deliv_payload = deliver_res["payload"]
        self.assertEqual(deliv_payload["delivered"], 100)
        self.assertGreater(deliv_payload["paid"], 0)
        self.assertGreater(deliv_payload["bond_refund"], 0)
        self.assertEqual(ref.get_balance("amos", "CR"), cr_before_deliver + deliv_payload["paid"] + deliv_payload["bond_refund"])

        # Ledger invariants
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)


if __name__ == "__main__":
    unittest.main()
