import unittest

from agora.referee import AgoraReferee
from agora.upgrades import CATALOG


def game(hazards=None, upgrades=True):
    ref = AgoraReferee(upgrades=upgrades, hazards=hazards)
    ref.new_game(seed=6, warmup_rounds=2, upgrades=upgrades, hazards=hazards)
    with ref.conn:  # enough cash to buy every tier
        ref.conn.execute("UPDATE accounts SET balance = balance + 100000 WHERE agent_id = 'amos' AND instrument = 'CR'")
        ref.conn.execute("UPDATE accounts SET balance = balance - 100000 WHERE agent_id = 'SYSTEM' AND instrument = 'CR'")
        ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('t-cash', 0, 'amos', 'CR', 100000)")
        ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('t-cash', 0, 'SYSTEM', 'CR', -100000)")
    return ref


def dest_for(ref, agent, min_rounds=0):
    from agora.spatial import STATIONS, get_route
    here = ref.get_vessel_location(agent)['station_id']
    best = None
    for d in STATIONS:
        if d == here:
            continue
        r = get_route(here, d, ref.current_round)
        if r and r['rounds'] >= min_rounds and (best is None or r['rounds'] > best[0]):
            best = (r['rounds'], d)
    return best


class TestUpgrades(unittest.TestCase):
    def test_off_means_factor_one_and_http_409(self):
        ref = game(upgrades=False)
        self.assertEqual(ref.upgrades.factor('amos', 'shielding'), 1.0)

    def test_buy_tiers_charges_cr_and_caps(self):
        ref = game()
        cr = ref.get_balance('amos', 'CR')
        r = ref.upgrades.buy('amos', 'shielding')
        self.assertEqual(r['kind'], 'upgrade_ok', r)
        self.assertEqual(ref.get_balance('amos', 'CR'), cr - CATALOG['shielding']['prices'][0])
        self.assertEqual(ref.upgrades.factor('amos', 'shielding'), CATALOG['shielding']['factors'][0])
        ref.upgrades.buy('amos', 'shielding')
        self.assertEqual(ref.upgrades.buy('amos', 'shielding')['payload']['reason'], 'max_tier')
        self.assertEqual(ref.upgrades.buy('amos', 'warp')['payload']['reason'], 'invalid_upgrade')
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_must_be_docked(self):
        ref = game()
        _, d = dest_for(ref, 'zero')
        ref.initiate_transit('zero', d)
        self.assertEqual(ref.upgrades.buy('zero', 'shielding')['payload']['reason'], 'vessel_not_docked')

    def test_shielding_cuts_delays(self):
        def delays(buy):
            ref = game(hazards='0.5,0')
            if buy:
                ref.upgrades.buy('amos', 'shielding'); ref.upgrades.buy('amos', 'shielding')
            n = 0
            for _ in range(40):
                _, d = dest_for(ref, 'amos')
                p = ref.initiate_transit('amos', d)['payload']
                n += bool(p.get('hazard'))
                while ref.get_vessel_location('amos')['status'] == 'in_transit':
                    ref.step_round()
                with ref.conn:
                    for acct, d in (('amos', 50), ('SYSTEM', -50)):
                        ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'FUEL'", (d, acct))
                        ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('t-fuel', 0, ?, 'FUEL', ?)", (acct, d))
            return n
        self.assertLess(delays(True), delays(False))

    def test_engines_shorten_long_trips(self):
        ref = game()
        best = dest_for(ref, 'amos', min_rounds=3)
        if not best:
            self.skipTest('no 3+ round route from the start station')
        self.assertEqual(ref.upgrades.buy('amos', 'engines')['kind'], 'upgrade_ok')
        p = ref.initiate_transit('amos', best[1])['payload']
        self.assertEqual(p['arrival_round'] - p['departure_round'], best[0] - 1)

    def test_briefing_section(self):
        from agora.briefing import build_briefing
        self.assertIn('Ship upgrades', build_briefing(game()))


class TestBalance162(unittest.TestCase):
    """#162, from the #154 dominance harness (PR #167): hold tier 1 bought in
    round 1 won 75% of seeds and armor tier 1 76%."""

    def test_armor_tier_1_costs_7500(self):
        self.assertEqual(CATALOG['armor']['prices'][0], 7_500)
        ref = game()
        cr = ref.get_balance('amos', 'CR')
        self.assertEqual(ref.upgrades.buy('amos', 'armor')['kind'], 'upgrade_ok')
        self.assertEqual(ref.get_balance('amos', 'CR'), cr - 7_500)

    def test_hold_is_locked_until_round_100(self):
        from agora import upgrades as U
        self.assertEqual(U.UNLOCKS['hold'], 100)
        ref = game()
        r = ref.upgrades.buy('amos', 'hold')
        self.assertEqual(r['kind'], 'reject')
        self.assertEqual(r['payload']['reason'], 'upgrade_locked')
        self.assertIn('round 100', r['payload']['detail'])
        self.assertEqual(ref.upgrades.tier('amos', 'hold'), 0)
        hold = next(c for c in ref.upgrades.catalog() if c['kind'] == 'hold')
        self.assertTrue(hold['locked'])
        self.assertEqual(hold['unlock_round'], 100)
        # Other upgrades are on sale from the start.
        self.assertEqual(ref.upgrades.buy('amos', 'shielding')['kind'], 'upgrade_ok')

    def news(self, ref):
        import json
        return [json.loads(r[0]) for r in ref.conn.execute("SELECT payload FROM book_events WHERE kind = 'news'")
                if 'gn-shipyard-' in r[0]]

    def test_round_100_unlocks_it_with_one_galnet_story(self):
        ref = game()
        for _ in range(99):
            ref.step_round()
        self.assertEqual(ref.current_round, 99)
        self.assertEqual(ref.upgrades.buy('amos', 'hold')['payload']['reason'], 'upgrade_locked')
        self.assertEqual(self.news(ref), [])
        ref.step_round()
        stories = self.news(ref)
        self.assertEqual(len(stories), 1)
        self.assertIn('SHIPYARD RETOOLING', stories[0]['headline'])
        self.assertTrue(any(e.id == 'gn-shipyard-hold' for e in ref.galnet.events))
        self.assertEqual(ref.upgrades.buy('amos', 'hold')['kind'], 'upgrade_ok')
        self.assertFalse(next(c for c in ref.upgrades.catalog() if c['kind'] == 'hold')['locked'])
        ref.step_round()
        self.assertEqual(len(self.news(ref)), 1)  # once a game
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_briefing_shows_the_lock(self):
        from agora.briefing import build_briefing
        ref = game()
        self.assertIn('locked until round 100', build_briefing(ref))
        ref.step_round(100)
        self.assertNotIn('locked until round 100', build_briefing(ref))


if __name__ == '__main__':
    unittest.main()
