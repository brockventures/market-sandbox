"""
Regression for #160: FUEL shipped as cargo and the trip's own fuel burn draw on
the same FUEL balance, so initiate_transit must check them together.
"""

import unittest

from agora.referee import AgoraReferee
from agora.spatial import get_route


class TestFuelCargoPlusBurn(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee()
        self.fuel = self.ref.get_balance('zero', 'FUEL')
        # zero starts docked at ceres (see tests/test_spatial.py).
        self.burn = get_route('ceres', 'earth', self.ref.current_round)['fuel']
        self.assertGreater(self.fuel, self.burn)

    def test_shipping_whole_fuel_balance_is_rejected(self):
        res = self.ref.initiate_transit('zero', 'earth', commodity='FUEL', cargo_qty=self.fuel)
        self.assertEqual(res['kind'], 'reject')
        self.assertEqual(res['payload']['reason'], 'insufficient_fuel')
        self.assertEqual(self.ref.get_balance('zero', 'FUEL'), self.fuel)
        valid, errors = self.ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_one_over_the_combined_limit_is_rejected(self):
        res = self.ref.initiate_transit('zero', 'earth', commodity='FUEL', cargo_qty=self.fuel - self.burn + 1)
        self.assertEqual(res['kind'], 'reject')
        self.assertEqual(res['payload']['reason'], 'insufficient_fuel')

    def test_exactly_burn_plus_cargo_is_allowed(self):
        res = self.ref.initiate_transit('zero', 'earth', commodity='FUEL', cargo_qty=self.fuel - self.burn)
        self.assertEqual(res.get('kind'), 'status', res)
        self.assertEqual(self.ref.get_balance('zero', 'FUEL'), 0)
        valid, errors = self.ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)


if __name__ == '__main__':
    unittest.main()
