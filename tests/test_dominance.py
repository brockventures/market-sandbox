"""
tests/test_dominance.py - the #154 dominance harness (tools/dominance.py).

Short games only: the harness's value depends on paired games being
identical until the treated fleet's buy, so that is what is tested.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import dominance  # noqa: E402

ROUNDS = 30


def game(item=None, start=None, scenario="haulers4", seed=1):
    r = dominance.play((scenario, "flat", seed, ROUNDS, item, start))
    assert "error" not in r, r.get("error")
    return r


class TestDominanceHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.never = game()

    def test_same_seed_same_game(self):
        self.assertEqual(game()["traj"], self.never["traj"])

    def test_arms_identical_until_the_buy(self):
        # Not "hold t1": hold is locked until round 100 (#162), so it would never buy here.
        for item in ("shielding t1", "escorts", "privateers", "stock buy"):
            late = game(item, 20)
            self.assertEqual(late["traj"][:18], self.never["traj"][:18], item)

    def test_tier_one_buy_goes_through_the_live_desk(self):
        r = game("shielding t1", 1)
        self.assertEqual(r["bought_round"], 1)
        self.assertEqual(r["holdings"], {"shielding": 1})
        self.assertIsNone(r["invariant_failure"])

    def test_a_locked_upgrade_is_not_bought_before_it_unlocks(self):
        # #162: hold tier 1 is not on sale until round 100; the live desk refuses it.
        r = game("hold t1", 1)
        self.assertIsNone(r["bought_round"])
        self.assertEqual(r["holdings"], {})
        self.assertIsNone(r["invariant_failure"])

    def test_tier_two_keeps_its_prerequisite_and_buys_nothing_else(self):
        r = game("armor t2", 1)
        self.assertEqual(r["holdings"], {"armor": 2})
        self.assertEqual(game("armor t2", ROUNDS + 1)["holdings"], {"armor": 1})

    def test_never_arm_buys_nothing(self):
        self.assertIn(self.never["holdings"], ({}, None))
        self.assertIsNone(self.never["bought_round"])

    def test_payback(self):
        self.assertEqual(dominance.payback([1, -1, 2, 3], [0, 0, 0, 0], 1), 2)
        self.assertEqual(dominance.payback([-5, -1, 0, 3], [0, 0, 0, 0], 1), 2)
        self.assertTrue(math.isinf(dominance.payback([1, 1, 1, -1], [0, 0, 0, 0], 1)))
        self.assertTrue(math.isinf(dominance.payback([1, 1], [0, 0], None)))


if __name__ == "__main__":
    unittest.main()
