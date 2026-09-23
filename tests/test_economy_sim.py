"""
tests/test_economy_sim.py - tools/economy_sim.py runs end to end, in-process,
fast enough for CI, on the live referee (#155), and its scripted fleets
actually trade and use the live-only mechanics.
"""

import unittest

from tools.economy_sim import run

KW = dict(mode="tolerant", check_every=10)


class TestEconomySim(unittest.TestCase):
    def test_short_mixed_run(self):
        r = run("mixed", "flat", seed=2, rounds=30, **KW)
        self.assertEqual(r["rounds"], 30)
        self.assertGreater(r["transits"], 0)
        self.assertGreater(r["fills"]["fleet_vs_depot"], 0)
        self.assertEqual(r["depot_model"], "reactive")
        self.assertIsNone(r["first_negative_depot_round"])
        self.assertIsNone(r["first_invariant_failure"])

    def test_idler_pays_the_live_idle_fee(self):
        # Live: 10 CR a round for a docked fleet that did nothing, in any
        # round some other fleet acted. The idler's only loss is the fee.
        r = run("mixed", "flat", seed=1, rounds=40, **KW)
        self.assertEqual(r["idle_fees"]["aerial"], 400)
        self.assertEqual(r["fleets"]["aerial"]["pnl"], -400)
        self.assertEqual(run("idle4", "flat", seed=1, rounds=20)["idle_fees_collected"], 0)  # nobody acted

    def test_contracts_are_claimed_delivered_and_balanced(self):
        r = run("market", "flat", seed=1, rounds=80, **KW)
        self.assertGreater(r["contracts"]["posted"], 0)
        self.assertGreater(r["contracts"]["claims"], 0)
        self.assertGreater(r["contracts"]["units_delivered"], 0)
        self.assertGreater(r["contracts"]["bonds_cr"], 0)
        self.assertIsNone(r["first_invariant_failure"])

    def test_novices_trade_badly_but_keep_the_ledger_clean(self):
        r = run("novice4", "flat", seed=1, rounds=60, check_every=20)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["bad_orders"], 0)
        self.assertGreater(r["transits"], 0)

    def test_non_deliverers_never_take_contracts(self):
        # An idler or maker never delivers, so it must never claim or buy a
        # contract: before this was enforced both did, ate the penalties and
        # were bankrupted or taken over in almost every mixed run.
        r = run("mixed", "flat", seed=1, rounds=150, mode="tolerant", check_every=50)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["corporate"]["claims"], 0)
        self.assertNotIn("marvin", r["corporate"]["debt_end"])
        self.assertNotIn("aerial", r["corporate"]["debt_end"])
        self.assertEqual([b for b in r["corporate"]["bankruptcies"] if b["fleet"] in ("marvin", "aerial")], [])

    def test_runs_repeat_exactly(self):
        a = run("novice_vs_haulers", "flat", seed=3, rounds=80, **KW)
        b = run("novice_vs_haulers", "flat", seed=3, rounds=80, **KW)
        self.assertIsNone(a["first_invariant_failure"])
        self.assertEqual({k: v["leaderboard_nw"] for k, v in a["fleets"].items()},
                         {k: v["leaderboard_nw"] for k, v in b["fleets"].items()})
        self.assertEqual(a["piracy"], b["piracy"])
        self.assertEqual(a["peer"], b["peer"])

    def test_live_mechanics_get_used(self):
        # Upgrades bought, hazards rolled, raids answered, privateers hired,
        # peer trades taken: every live-only mechanic the old sim lacked.
        r = run("privateer_vs_haulers", "flat", seed=3, rounds=150, **KW)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(sum(sum(u.values()) for u in r["upgrades"].values()), 0)
        self.assertGreater(r["hazards"]["delays"] + r["hazards"]["losses"], 0)
        self.assertGreater(r["piracy"]["raids"], 0)
        self.assertGreater(r["piracy"]["privateer_contracts"], 0)
        self.assertEqual(r["piracy"]["timed_out"], 0)  # haulers answer every demand
        self.assertGreater(r["peer"]["trades"], 0)
        # #153/#151: those actions are corp events, and known ones move stocks.
        self.assertGreater(r["events"]["by_kind"].get("upgrade", 0), 0)
        self.assertGreater(r["events"]["by_visibility"].get("secret", 0), 0)  # the privateer contracts
        self.assertGreater(r["events"]["stock_shocks"], 0)

    def test_peer_buyers_fly_to_collect(self):
        # #126: remote buyers used to leave their pickups uncollected.
        r = run("haulers4", "flat", seed=1, rounds=150, **KW)
        self.assertGreater(r["peer"]["trades"], 0)
        self.assertGreater(r["peer"]["remote"], 0)
        self.assertEqual(r["peer"]["expired_units"], 0)

    def test_overrides_turn_live_features_off(self):
        r = run("haulers4", "flat", seed=1, rounds=20, overrides={"piracy": False, "upgrades": False, "fog": False},
                **KW)
        self.assertIsNone(r["piracy"])
        self.assertIsNone(r["upgrades"])
        self.assertIsNone(r["fog"])
        self.assertIsNone(r["first_invariant_failure"])

    def test_constant_overrides_apply_and_restore(self):
        import agora.contracts as c
        before = c.PENALTY
        r = run("haulers4", "flat", seed=1, rounds=20, constants={"contracts.PENALTY": 0.9}, **KW)
        self.assertEqual(r["constants"], {"contracts.PENALTY": 0.9})
        self.assertEqual(c.PENALTY, before)
        with self.assertRaises(ValueError):
            run("idle4", "flat", seed=1, rounds=1, constants={"contracts.NO_SUCH_THING": 1})

    def test_stock_trader_against_the_live_exchange(self):
        r = run("stocks", "flat", seed=2, rounds=120, **KW)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertNotEqual(r["stocks"]["fleets"]["marvin"]["stock_cash"], 0)
        self.assertGreater(r["exchange"]["cr"], 0)
        self.assertNotIn("marvin", r["corporate"]["debt_end"])  # never takes contracts

    def test_stock_trader_with_stand_in_liquidity(self):
        r = run("stocks", "flat", seed=1, rounds=60, equity_mm=(0.05, 20), **KW)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertEqual(r["stocks"]["equity_mm"], [0.05, 20])

    def test_no_stock_trader_leaves_results_unchanged(self):
        r = run("mixed", "flat", seed=1, rounds=30, mode="tolerant")
        self.assertNotIn("stocks", r)

    def test_daytraders_trade_and_spread_scale_restores(self):
        import agora.spatial as sp
        before = sp.BASE_PRICES["ceres"]["ORE"]
        r = run("daytrade_vs_haulers", "flat", seed=1, rounds=80, vol=2.4, spread_scale=0.5, **KW)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["day_trades"], 0)
        self.assertEqual(sp.BASE_PRICES["ceres"]["ORE"], before)

    def test_styles_rotate_through_the_homes(self):
        # #162: one fleet per style, each style at a different home each seed.
        from tools.economy_sim import scenario_kinds, FLEETS
        seen = {k: set() for k in scenario_kinds("styles", 0).values()}
        for seed in range(4):
            kinds = scenario_kinds("styles", seed)
            self.assertEqual(sorted(kinds.values()), ["hauler", "maker", "privateer", "stock_trader"])
            for a in FLEETS:
                seen[kinds[a]].add(a)
        self.assertTrue(all(v == set(FLEETS) for v in seen.values()))

    def test_maker_earns_from_station_order_flow(self):
        # #162: the maker moves to a two-sided station and the station's own
        # buyers and sellers fill its quotes. Seed 2 puts it at Earth, so it
        # flies to Luna first.
        r = run("styles", "flat", seed=2, rounds=60, check_every=20)
        self.assertIsNone(r["first_invariant_failure"])
        maker = next(a for a, f in r["fleets"].items() if f["strategy"] == "maker")
        self.assertGreater(r["order_flow"]["by_fleet"][maker]["units"], 0)
        self.assertGreater(r["fleets"][maker]["pnl"], 0)
        for f in r["fleets"].values():
            self.assertEqual(f["pnl_total"], f["pnl_active"] + f["pnl_passive"])

    def test_planet_genesis_preserves_value(self):
        r = run("idle4", "planet", seed=1, rounds=1)
        for f in r["fleets"].values():
            self.assertAlmostEqual(f["start"], 33000, delta=20)

    def test_covert_scenarios_rotate_through_the_homes(self):
        # #174: covert styles rotate through home stations like base styles
        from tools.economy_sim import scenario_kinds, FLEETS
        for sc, expected in [
            ("styles_saboteur", ["hauler", "maker", "privateer", "saboteur"]),
            ("styles_spy", ["hauler", "maker", "privateer", "spy"]),
            ("styles_covert", ["hauler", "maker", "saboteur", "spy"]),
        ]:
            seen = {k: set() for k in scenario_kinds(sc, 0).values()}
            for seed in range(4):
                kinds = scenario_kinds(sc, seed)
                self.assertEqual(sorted(kinds.values()), expected)
                for a in FLEETS:
                    seen[kinds[a]].add(a)
            self.assertTrue(all(v == set(FLEETS) for v in seen.values()))

    def test_covert_sim_bots_act_and_strike(self):
        # #174: saboteur executes sabotage, spy plants wiretaps and strikes, invariants hold
        r_sab = run("styles_saboteur", "flat", seed=1, rounds=60, check_every=20)
        self.assertIsNone(r_sab["first_invariant_failure"])
        self.assertGreater(r_sab["covert"]["sabotages"], 0)

        r_spy = run("styles_spy", "flat", seed=1, rounds=60, check_every=20)
        self.assertIsNone(r_spy["first_invariant_failure"])
        self.assertGreater(r_spy["covert"]["wiretaps"], 0)


if __name__ == "__main__":
    unittest.main()
