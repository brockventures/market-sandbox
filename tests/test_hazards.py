import unittest

from agora.referee import AgoraReferee
from agora.hazards import parse_hazards


def game(odds, seed=5):
    ref = AgoraReferee(hazards=odds)
    ref.new_game(seed=seed, warmup_rounds=2, hazards=odds)
    return ref


def fly(ref, agent='amos', dest='mars', qty=0):
    loc = ref.get_vessel_location(agent)
    if loc.get('station_id') == dest:
        dest = 'luna'
    return ref.initiate_transit(agent, dest, commodity='FRAG', cargo_qty=qty)


class TestHazards(unittest.TestCase):
    def test_parse(self):
        self.assertIsNone(parse_hazards('0'))
        self.assertIsNone(parse_hazards(None))
        self.assertEqual(parse_hazards('0.2,0.1'), (0.2, 0.1))
        self.assertEqual(parse_hazards([1, 0]), (1.0, 0.0))

    def test_off_by_default(self):
        ref = AgoraReferee()
        ref.new_game(seed=5, warmup_rounds=2)
        self.assertIsNone(ref.hazards.odds)
        r = fly(ref, qty=10)
        self.assertIsNone(r['payload']['hazard'])

    def test_certain_delay_adds_rounds_and_is_reported(self):
        ref = game('1,0')
        r = fly(ref)
        p = r['payload']
        self.assertIsNotNone(p['hazard'], r)
        self.assertGreaterEqual(p['hazard']['delay'], 1)
        self.assertEqual(p['arrival_round'] - p['departure_round'], p['rounds_duration'] + p['hazard']['delay'])
        self.assertTrue(ref.verify_ledger_invariants()[0])

    def test_certain_loss_delivers_less_and_ledger_balances(self):
        ref = game('0,1')
        frag = ref.get_balance('amos', 'FRAG')
        self.assertGreater(frag, 20)
        r = fly(ref, qty=frag)
        lost = r['payload']['hazard']['lost_qty']
        self.assertGreater(lost, 0)
        for _ in range(r['payload']['arrival_round'] - ref.current_round + 1):
            ref.step_round()
        self.assertEqual(ref.get_balance('amos', 'FRAG'), frag - lost)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)
        from agora.briefing import build_briefing
        self.assertIn('Hazards in flight', build_briefing(ref))

    def test_seeded_rolls_repeat(self):
        def run():
            ref = game('0.5,0.5', seed=9)
            return fly(ref, qty=10)['payload']['hazard']
        self.assertEqual(run(), run())


if __name__ == '__main__':
    unittest.main()
