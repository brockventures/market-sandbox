"""
tests/test_economy_sim.py - tools/economy_sim.py runs end to end, in-process,
fast enough for CI, and its scripted fleets actually trade.
"""

import unittest

from tools.economy_sim import run


class TestEconomySim(unittest.TestCase):
    def test_short_mixed_run(self):
        r = run("mixed", "flat", seed=2, rounds=30, mode="tolerant", check_every=10)
        self.assertEqual(r["rounds"], 30)
        self.assertGreater(r["transits"], 0)
        self.assertGreater(r["fills"]["fleet_vs_depot"], 0)
        self.assertEqual(r["fleets"]["aerial"]["pnl"], 0)  # idler never trades
        self.assertIsNone(r["first_invariant_failure"])

    def test_reactive_depots_run_clean(self):
        r = run("haulers4", "flat", seed=1, rounds=40, mode="tolerant", check_every=10,
                depot_model="reactive", band_pct=0.25)
        self.assertEqual(r["depot_model"], "reactive")
        self.assertGreater(r["transits"], 0)
        self.assertIsNone(r["first_negative_depot_round"])
        self.assertIsNone(r["first_invariant_failure"])

    def test_contracts_are_delivered_and_balanced(self):
        r = run("market", "flat", seed=1, rounds=60, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, contracts=True)
        self.assertGreater(r["contracts"]["posted"], 0)
        self.assertGreater(r["contracts"]["units_delivered"], 0)
        self.assertIsNone(r["first_invariant_failure"])

    def test_novices_trade_badly_but_keep_the_ledger_clean(self):
        r = run("novice4", "flat", seed=1, rounds=60, check_every=20,
                depot_model="reactive", band_pct=0.25)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["bad_orders"], 0)
        self.assertGreater(r["transits"], 0)
        self.assertTrue(any(f["pnl"] > 0 for f in r["fleets"].values()))

    def test_owned_contracts_change_hands_and_balance(self):
        r = run("haulers4", "flat", seed=1, rounds=60, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, owned_contracts=True, fog=(3, 0.15))
        self.assertIsNone(r["first_invariant_failure"])
        self.assertTrue(r["contracts"]["owned"])
        self.assertGreater(r["contracts"]["transfers"], 0)

    def test_peer_desk_keeps_ledger_balanced(self):
        r = run("novice_vs_haulers", "flat", seed=1, rounds=80, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, peer=True, fog=(3, 0.15))
        self.assertIsNone(r["first_invariant_failure"])
        self.assertIn("trades", r["peer"])

    def test_corporate_risk_runs_clean_and_is_reproducible(self):
        a = run("novice_vs_haulers", "flat", seed=3, rounds=60, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, corporate=True)
        b = run("novice_vs_haulers", "flat", seed=3, rounds=60, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, corporate=True)
        self.assertIsNone(a["first_invariant_failure"])
        self.assertGreater(a["corporate"]["claims"], 0)
        self.assertEqual({k: v["leaderboard_nw"] for k, v in a["fleets"].items()},
                         {k: v["leaderboard_nw"] for k, v in b["fleets"].items()})

    def test_corporate_non_deliverers_never_take_contracts(self):
        # An idler or maker never delivers, so it must never claim or buy a
        # contract: before this fix both did, ate the penalties and were
        # bankrupted or taken over in almost every mixed run.
        r = run("mixed", "flat", seed=1, rounds=150, mode="tolerant", check_every=50,
                depot_model="reactive", band_pct=0.25, corporate=True)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["corporate"]["claims"], 0)
        self.assertNotIn("marvin", r["corporate"]["debt_end"])
        self.assertNotIn("aerial", r["corporate"]["debt_end"])
        self.assertEqual([b for b in r["corporate"]["bankruptcies"] if b["fleet"] in ("marvin", "aerial")], [])
        self.assertEqual(r["fleets"]["aerial"]["pnl"], 0)

    def test_peer_desk_respects_goods_committed_to_resting_asks(self):
        # All features on, seed 5: the peer desk used to sell ORE that a
        # resting distress ask had committed, and the seller went to -200 ORE.
        r = run("novice_vs_haulers", "flat", seed=5, rounds=160, mode="tolerant", check_every=4,
                depot_model="reactive", band_pct=0.25, peer=True, fog=(3, 0.15), corporate=True)
        self.assertIsNone(r["first_invariant_failure"])

    def test_stock_trader_trades_against_stand_in_liquidity(self):
        r = run("stocks", "flat", seed=1, rounds=120, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, corporate=True, equity_mm=(0.05, 20))
        self.assertIsNone(r["first_invariant_failure"])
        st = r["stocks"]["fleets"]["marvin"]
        self.assertNotEqual(st["stock_cash"], 0)  # it traded
        self.assertEqual(r["corporate"]["claims"] > 0, True)
        self.assertNotIn("marvin", r["corporate"]["debt_end"])  # never takes contracts

    def test_stock_trader_against_referee_exchange(self):
        r = run("stocks", "flat", seed=2, rounds=120, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, corporate=True, exchange=(100, 0.03))
        self.assertIsNone(r["first_invariant_failure"])
        self.assertNotEqual(r["stocks"]["fleets"]["marvin"]["stock_cash"], 0)
        self.assertGreater(r["stocks"]["exchange"]["cr"], 0)

    def test_no_stock_trader_leaves_results_unchanged(self):
        r = run("mixed", "flat", seed=1, rounds=30, mode="tolerant")
        self.assertNotIn("stocks", r)

    def test_daytraders_trade_and_spread_scale_restores(self):
        import agora.spatial as sp
        before = sp.BASE_PRICES["ceres"]["ORE"]
        r = run("daytrade_vs_haulers", "flat", seed=1, rounds=80, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, vol=2.4, spread_scale=0.5)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["day_trades"], 0)
        self.assertEqual(sp.BASE_PRICES["ceres"]["ORE"], before)

    def test_claim_bond_and_hazards_keep_ledger_balanced_and_repeat(self):
        kw = dict(mode="tolerant", check_every=10, depot_model="reactive", band_pct=0.25,
                  corporate=True, bond=0.25, hazards=(0.2, 0.1))
        a = run("novice_vs_haulers", "flat", seed=2, rounds=120, **kw)
        b = run("novice_vs_haulers", "flat", seed=2, rounds=120, **kw)
        self.assertIsNone(a["first_invariant_failure"])
        self.assertGreater(a["hazards"]["delays"] + a["hazards"]["losses"], 0)
        self.assertGreater(a["corporate"].get("bonds_cr", 0), 0)
        self.assertEqual({k: v["pnl"] for k, v in a["fleets"].items()}, {k: v["pnl"] for k, v in b["fleets"].items()})

    def test_piracy_and_privateers_keep_ledger_balanced(self):
        r = run("privateer_vs_haulers", "flat", seed=3, rounds=150, mode="tolerant", check_every=10,
                depot_model="reactive", band_pct=0.25, corporate=True, bond=0.25, piracy=(0.3, 0.1))
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["piracy"]["raids"], 0)
        self.assertGreater(r["piracy"]["privateer_contracts"], 0)

    def test_dock_fee_charges_idlers(self):
        r = run("idle4", "flat", seed=1, rounds=20, dock_fee=10)
        for f in r["fleets"].values():
            self.assertEqual(f["pnl"], -200)
        self.assertEqual(r["dock_fees_collected"], 800)
        self.assertIsNone(r["first_invariant_failure"])

    def test_planet_genesis_preserves_value(self):
        r = run("idle4", "planet", seed=1, rounds=1)
        for f in r["fleets"].values():
            self.assertAlmostEqual(f["start"], 33000, delta=20)


if __name__ == "__main__":
    unittest.main()
