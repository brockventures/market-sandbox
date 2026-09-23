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
