"""
tests/test_covert_ops.py - Comprehensive test suite for Covert Ops & Rivalry (#133, #135, #152).
"""

import unittest

from agora.referee import AgoraReferee
from agora import covert as covert_mod
from agora.covert import WIRETAP_COST, WIRETAP_ROUNDS, SABOTAGE_COST, SABOTAGE_FINE, RIVALRY_DECAY_ROUNDS
from agora.piracy import cargo_value


class TestCovertOps(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(events=True, corporate=True, piracy='0.3,0.1')
        self.covert = self.ref.covert

    def test_wiretap_flow_and_intel(self):
        # 1. Zero taps Amos
        target = 'amos'
        actor = 'zero'
        start_bal = self.ref.get_balance(actor, 'CR')

        res = self.covert.plant_wiretap(actor, target)
        self.assertEqual(res['kind'], 'wiretap_ok')
        self.assertEqual(res['payload']['target'], target)
        self.assertEqual(self.ref.get_balance(actor, 'CR'), start_bal - WIRETAP_COST)

        # Invariants hold
        valid, errors = self.ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

        # 2. Check wiretap active
        self.assertTrue(self.covert.has_wiretap(actor, target))
        self.assertFalse(self.covert.has_wiretap(target, actor))

        # 3. Duplicate wiretap rejected
        dup = self.covert.plant_wiretap(actor, target)
        self.assertEqual(dup['kind'], 'reject')
        self.assertEqual(dup['payload']['reason'], 'wiretap_active')

        # 4. Self wiretap rejected
        self_tap = self.covert.plant_wiretap(actor, actor)
        self.assertEqual(self_tap['kind'], 'reject')
        self.assertEqual(self_tap['payload']['reason'], 'self_target')

        # 5. Intercept telemetry via get_intel
        intel = self.covert.get_intel(actor, target)
        self.assertEqual(intel['kind'], 'intel_ok')
        self.assertIn('liquid_cr', intel['payload'])
        self.assertIn('cargo', intel['payload'])
        self.assertIn('location', intel['payload'])

        # Without wiretap, intel is rejected
        unauth_intel = self.covert.get_intel('marvin', target)
        self.assertEqual(unauth_intel['kind'], 'reject')
        self.assertEqual(unauth_intel['payload']['reason'], 'no_wiretap')

    def test_wiretap_reveals_secret_events_in_visible_to(self):
        actor = 'zero'
        target = 'amos'
        third_party = 'marvin'

        # Target creates a secret event (e.g. privateer contract or secret op)
        ev_id = self.ref.events.record(
            actor=target, victim=third_party, kind='privateer_contract',
            visibility='secret', round=self.ref.current_round,
            detail=f"{target} hired privateers against {third_party}"
        )

        # Before wiretap: zero cannot see target's secret event
        seen_before = self.ref.events.visible_to(actor)
        self.assertFalse(any(e['id'] == ev_id for e in seen_before))

        # Plant wiretap
        self.covert.plant_wiretap(actor, target)

        # After wiretap: zero CAN see target's secret event!
        seen_after = self.ref.events.visible_to(actor)
        intercepted = [e for e in seen_after if e['id'] == ev_id]
        self.assertEqual(len(intercepted), 1)
        self.assertEqual(intercepted[0]['actor'], target)
        self.assertTrue(intercepted[0].get('wiretapped', False))

        # Third party without tap still cannot see it
        self.assertFalse(any(e['id'] == ev_id for e in self.ref.events.visible_to(third_party)))

    def test_sabotage_docked_cargo(self):
        actor = 'zero'
        target = 'amos'

        target_frag_before = self.ref.get_balance(target, 'FRAG')
        actor_cr_before = self.ref.get_balance(actor, 'CR')

        res = self.covert.execute_sabotage(actor, target, mode='docked')
        self.assertEqual(res['kind'], 'sabotage_ok')
        self.assertEqual(res['payload']['target'], target)

        # Target lost cargo or fuel
        target_frag_after = self.ref.get_balance(target, 'FRAG')
        target_fuel_after = self.ref.get_balance(target, 'FUEL')
        target_fuel_before = self.ref.get_balance(target, 'FUEL')
        self.assertTrue(target_frag_after < target_frag_before or target_fuel_after < target_fuel_before)

        # Actor paid sabotage fee
        actor_cr_after = self.ref.get_balance(actor, 'CR')
        self.assertLessEqual(actor_cr_after, actor_cr_before - SABOTAGE_COST)

        # Invariants hold
        valid, errors = self.ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_sabotage_transit_cargo(self):
        actor = 'zero'
        target = 'amos'

        self.ref.initiate_transit(target, 'luna', 'FRAG', 30)
        loc_before = self.ref.get_vessel_location(target)
        arr_before = loc_before['transit']['arrival_round']
        cargo_before = loc_before['transit']['cargo_qty']

        res = self.covert.execute_sabotage(actor, target, mode='transit')
        self.assertEqual(res['kind'], 'sabotage_ok')
        self.assertIn('delayed +1 round', res['payload']['damage'])

        loc_after = self.ref.get_vessel_location(target)
        arr_after = loc_after['transit']['arrival_round']
        cargo_after = loc_after['transit']['cargo_qty']

        self.assertEqual(arr_after, arr_before + 1)
        self.assertLess(cargo_after, cargo_before)

        valid, errors = self.ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_sabotage_transit_loot_goes_to_the_saboteur(self):
        # #186: the siphoned cargo reaches the saboteur (SABOTAGE_LOOT_SHARE),
        # valued at the reference price piracy uses, and the victim gets the rest.
        actor, target = 'zero', 'amos'
        self.covert.bags.force('sabotage_trace', False)  # untraced
        self.ref.initiate_transit(target, 'luna', 'FRAG', 30)
        frag_before = self.ref.get_balance(actor, 'FRAG')
        res = self.covert.execute_sabotage(actor, target, mode='transit')
        self.assertEqual(res['kind'], 'sabotage_ok')
        taken = int(30 * covert_mod.TRANSIT_SIPHON)
        cut = int(taken * covert_mod.SABOTAGE_LOOT_SHARE)
        self.assertEqual(self.ref.get_balance(actor, 'FRAG'), frag_before + cut)
        self.assertEqual(res['payload']['loss_cr'], cargo_value('FRAG', taken))
        self.assertEqual(res['payload']['loot']['qty'], cut)
        self.assertEqual(self.ref.get_vessel_location(target)['transit']['cargo_qty'], 30 - taken)
        valid, errors = self.ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_sabotage_docked_loot_goes_to_the_saboteur(self):
        actor, target = 'zero', 'amos'
        before = {c: self.ref.get_balance(actor, c) for c in ('FRAG', 'FOOD', 'ORE', 'FUEL')}
        res = self.covert.execute_sabotage(actor, target, mode='docked')
        loot = res['payload']['loot']
        self.assertIsNotNone(loot)
        self.assertEqual(self.ref.get_balance(actor, loot['commodity']), before[loot['commodity']] + loot['qty'])
        valid, errors = self.ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_sabotage_target_on_alert_for_cooldown(self):
        # #186: a paying sabotage cannot be repeated on one target every round.
        actor, target = 'zero', 'amos'
        self.assertEqual(self.covert.execute_sabotage(actor, target)['kind'], 'sabotage_ok')
        cr = self.ref.get_balance('marvin', 'CR')
        again = self.covert.execute_sabotage('marvin', target)
        self.assertEqual(again['kind'], 'reject')
        self.assertEqual(again['payload']['reason'], 'target_alert')
        self.assertEqual(self.ref.get_balance('marvin', 'CR'), cr)  # no fee for a refused strike
        self.ref.current_round += covert_mod.SABOTAGE_COOLDOWN
        self.assertEqual(self.covert.execute_sabotage('marvin', target)['kind'], 'sabotage_ok')

    def test_sabotage_alert_ignores_later_rounds_after_a_round_reset(self):
        # A restart over the same database restarts current_round.
        self.ref.current_round = 50
        self.assertEqual(self.covert.execute_sabotage('zero', 'amos')['kind'], 'sabotage_ok')
        self.ref.current_round = 0
        self.assertEqual(self.covert.execute_sabotage('zero', 'amos')['kind'], 'sabotage_ok')
        self.ref.new_game(seed=3)
        self.assertEqual(self.covert.execute_sabotage('zero', 'amos')['kind'], 'sabotage_ok')

    def test_sabotage_traced_fine_restitution(self):
        actor = 'zero'
        target = 'amos'

        # Conserving topup from SYSTEM to actor
        seq = self.ref._get_next_seq()
        for acct, d in (('SYSTEM', -20000), (actor, 20000)):
            self.ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (d, acct))
            self.ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                                  ('topup-actor', seq, acct, d))

        # Force trace
        self.covert.bags.force('sabotage_trace', True)  # Guaranteed trace (< 0.25)

        target_cr_before = self.ref.get_balance(target, 'CR')
        actor_cr_before = self.ref.get_balance(actor, 'CR')

        res = self.covert.execute_sabotage(actor, target)
        self.assertEqual(res['kind'], 'sabotage_ok')
        self.assertTrue(res['payload']['traced'])
        self.assertEqual(res['payload']['fine'], SABOTAGE_FINE)

        # Treble fine transferred to victim
        self.assertEqual(self.ref.get_balance(target, 'CR'), target_cr_before + SABOTAGE_FINE)
        self.assertEqual(self.ref.get_balance(actor, 'CR'), actor_cr_before - SABOTAGE_COST - SABOTAGE_FINE)

        # Ledger invariant holds
        valid, errors = self.ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_corporate_rivalry_scoreboard(self):
        actor = 'zero'
        target = 'amos'

        # Generate some aggression events
        self.ref.events.record(
            actor=actor, victim=target, kind='sabotage',
            visibility='public', round=self.ref.current_round,
            detail=f"{actor} sabotaged {target}"
        )
        self.ref.events.record(
            actor=actor, victim=target, kind='stake_20',
            visibility='public', round=self.ref.current_round,
            detail=f"{actor} acquired 20% stake in {target}"
        )

        board = self.covert.rivalry_scoreboard()
        self.assertIn('rivalries', board)
        self.assertIn('hostility', board)
        self.assertGreater(len(board['rivalries']), 0)

        top = board['rivalries'][0]
        self.assertEqual(top['aggressor'], actor)
        self.assertEqual(top['victim'], target)
        self.assertGreater(top['rivalry_score'], 0)
        self.assertEqual(top['incidents_count'], 2)

        # Check decay: advancing rounds lowers rivalry score
        initial_score = top['rivalry_score']
        for _ in range(15):
            self.ref.step_round()

        decayed_board = self.covert.rivalry_scoreboard()
        top_decayed = [r for r in decayed_board['rivalries'] if r['aggressor'] == actor and r['victim'] == target][0]
        self.assertLess(top_decayed['rivalry_score'], initial_score)


if __name__ == '__main__':
    unittest.main()
