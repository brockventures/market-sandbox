"""Idle fee: charged only to docked fleets that did nothing in a round someone else played."""
import unittest

from agora.referee import AgoraReferee


def cr(ref, a):
    return ref.get_balance(a, 'CR')


class TestIdleFee(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(depots=True, idle_fee=10)
        self.fleets = [r[0] for r in self.ref.conn.execute("SELECT agent_id FROM fleet_roster")]

    def test_no_one_playing_no_fees(self):
        before = {a: cr(self.ref, a) for a in self.fleets}
        for _ in range(5):
            self.ref.step_round()
        self.assertEqual({a: cr(self.ref, a) for a in self.fleets}, before)

    def test_idlers_pay_actors_do_not(self):
        ref = self.ref
        actor, idlers = self.fleets[0], self.fleets[1:]
        before = {a: cr(ref, a) for a in self.fleets}
        ref.cancel_all(actor)  # any action counts
        res = ref.step_round()
        self.assertEqual(cr(ref, actor), before[actor])
        for a in idlers:
            self.assertEqual(cr(ref, a), before[a] - 10)
        self.assertEqual(set(res['idle_fees']), set(idlers))
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_off_by_default_in_library(self):
        self.assertEqual(AgoraReferee().idle_fee, 0)


if __name__ == '__main__':
    unittest.main()
