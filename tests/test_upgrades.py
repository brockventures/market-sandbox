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
        to_round(ref, 175)  # every shielding tier on sale (#181)
        ref.upgrades.buy('amos', 'shielding')
        ref.upgrades.buy('amos', 'shielding')
        self.assertEqual(ref.upgrades.tier('amos', 'shielding'), 3)
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
                to_round(ref, 75)  # shielding tier 2 on sale (#181)
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
        to_round(ref, 40)  # engines tier 1 on sale (#181)
        self.assertEqual(ref.upgrades.buy('amos', 'engines')['kind'], 'upgrade_ok')
        p = ref.initiate_transit('amos', best[1])['payload']
        self.assertEqual(p['arrival_round'] - p['departure_round'], best[0] - 1)

    def test_briefing_section(self):
        from agora.briefing import build_briefing
        self.assertIn('Ship upgrades', build_briefing(game()))


class TestBalance162(unittest.TestCase):
    """#162, from the #154 dominance harness (PR #167): armor tier 1 bought
    in round 1 won 76% of seeds, so it costs 7,500."""

    def test_armor_tier_1_costs_7500(self):
        self.assertEqual(CATALOG['armor']['prices'][0], 7_500)
        ref = game()
        cr = ref.get_balance('amos', 'CR')
        self.assertEqual(ref.upgrades.buy('amos', 'armor')['kind'], 'upgrade_ok')
        self.assertEqual(ref.get_balance('amos', 'CR'), cr - 7_500)


def fund(ref, agent, cr):
    with ref.conn:
        for acct, d in ((agent, cr), ('SYSTEM', -cr)):
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (d, acct))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('t-fund', 0, ?, 'CR', ?)",
                             (acct, d))


def to_round(ref, n):
    while ref.current_round < n:
        ref.step_round()


class TestStagedTiers181(unittest.TestCase):
    """#181 (Ryan 2026-09-23): three tiers (engines two) on one staggered
    shipyard schedule, each unlock after round 0 announced on GalNet."""

    SCHEDULE = {0: [('armor', 1), ('shielding', 1)], 10: [('boarding_pods', 1)],
                15: [('telemetry', 1)], 20: [('algo_desk', 1)], 25: [('priority_slips', 1)],
                30: [('hardened_comm', 1)], 35: [('bulk_storage', 1)],
                40: [('engines', 1)], 50: [('hold', 1)], 60: [('ecm_jammers', 1)],
                75: [('shielding', 2)], 80: [('refinery_loop', 1)], 90: [('stealth_drives', 1)],
                100: [('armor', 2)], 125: [('hold', 2)], 175: [('shielding', 3)],
                200: [('armor', 3)], 225: [('hold', 3)], 250: [('engines', 2)]}

    def test_catalog_values(self):
        self.assertEqual(CATALOG['shielding']['prices'], [3_000, 7_000, 14_000])
        self.assertEqual(CATALOG['shielding']['factors'], [0.85, 0.6, 0.35])
        self.assertEqual(CATALOG['hold']['prices'], [4_000, 9_000, 16_000])
        self.assertEqual(CATALOG['hold']['factors'], [0.75, 0.45, 0.25])
        self.assertEqual(CATALOG['armor']['prices'], [7_500, 11_000, 18_000])
        self.assertEqual(CATALOG['armor']['factors'], [0.7, 0.45, 0.25])
        self.assertEqual(CATALOG['engines']['prices'], [12_000, 24_000])

    def test_one_unlock_per_round_on_the_schedule(self):
        got = {}
        for kind, c in CATALOG.items():
            for i, at in enumerate(c['unlocks']):
                got.setdefault(at, []).append((kind, i + 1))
        self.assertEqual({k: sorted(v) for k, v in got.items()}, self.SCHEDULE)
        self.assertTrue(all(len(v) == 1 for at, v in got.items() if at > 0))

    def test_tiers_lock_per_tier_and_buy_in_order(self):
        ref = game()
        # Tier 1 of shielding is on sale at once; tier 2 is not until round 75.
        self.assertEqual(ref.upgrades.buy('amos', 'shielding')['kind'], 'upgrade_ok')
        r = ref.upgrades.buy('amos', 'shielding')
        self.assertEqual(r['payload']['reason'], 'upgrade_locked')
        self.assertIn('tier 2', r['payload']['detail'])
        self.assertIn('round 75', r['payload']['detail'])
        self.assertEqual(ref.upgrades.tier('amos', 'shielding'), 1)
        # Hold tier 1 is locked until round 50.
        r = ref.upgrades.buy('amos', 'hold')
        self.assertEqual(r['payload']['reason'], 'upgrade_locked')
        self.assertIn('round 50', r['payload']['detail'])
        to_round(ref, 125)
        # In order: two buys at round 125 give hold tiers 1 and 2, never tier 2 first.
        self.assertEqual(ref.upgrades.buy('amos', 'hold')['payload']['tier'], 1)
        self.assertEqual(ref.upgrades.buy('amos', 'hold')['payload']['tier'], 2)
        self.assertEqual(ref.upgrades.buy('amos', 'hold')['payload']['reason'], 'upgrade_locked')  # t3 at 225
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_catalog_shows_each_tiers_lock(self):
        ref = game()
        to_round(ref, 80)
        c = {x['kind']: x for x in ref.upgrades.catalog()}
        self.assertEqual([t['locked'] for t in c['shielding']['tier_detail']], [False, False, True])
        self.assertEqual([t['unlock_round'] for t in c['hold']['tier_detail']], [50, 125, 225])
        self.assertEqual([t['locked'] for t in c['engines']['tier_detail']], [False, True])
        self.assertEqual(c['hold']['unlock_rounds'], [50, 125, 225])

    def test_factors_by_tier(self):
        ref = game()
        to_round(ref, 225)
        fund(ref, 'amos', 200_000)
        for kind, want in (('shielding', [0.85, 0.6, 0.35]), ('hold', [0.75, 0.45, 0.25]),
                           ('armor', [0.7, 0.45, 0.25])):
            self.assertEqual(ref.upgrades.factor('amos', kind), 1.0)
            for f in want:
                self.assertEqual(ref.upgrades.buy('amos', kind)['kind'], 'upgrade_ok')
                self.assertEqual(ref.upgrades.factor('amos', kind), f)
        self.assertEqual(ref.upgrades.buy('amos', 'armor')['payload']['reason'], 'max_tier')

    def test_hold_loss_size_from_tier_2(self):
        ref = game()
        to_round(ref, 125)
        self.assertEqual(ref.upgrades.loss_size_factor('amos'), 1.0)
        ref.upgrades.buy('amos', 'hold')
        self.assertEqual(ref.upgrades.loss_size_factor('amos'), 1.0)  # tier 1 cuts the chance only
        ref.upgrades.buy('amos', 'hold')
        self.assertEqual(ref.upgrades.loss_size_factor('amos'), 0.7)

    def test_engine_cuts_by_tier(self):
        ref = game()
        to_round(ref, 250)
        fund(ref, 'amos', 100_000)
        self.assertEqual([ref.upgrades.engine_cut('amos', n) for n in (2, 3, 5)], [0, 0, 0])
        ref.upgrades.buy('amos', 'engines')
        self.assertEqual([ref.upgrades.engine_cut('amos', n) for n in (2, 3, 4, 5, 6)], [0, 1, 1, 1, 1])
        ref.upgrades.buy('amos', 'engines')
        # Tier 2 cuts fuel, not rounds (#189): the round cut stays tier 1's.
        self.assertEqual([ref.upgrades.engine_cut('amos', n) for n in (2, 3, 4, 5, 6)], [0, 1, 1, 1, 1])

    def test_engines_tier_2_cuts_fuel_40pct(self):
        """#189: engines tier 2 cuts every trip's FUEL burn by 40%; tier 1
        leaves the burn alone. Checked on the live initiate_transit debit."""
        from agora.spatial import get_route
        ref = game()
        to_round(ref, 250)
        fund(ref, 'amos', 100_000)
        self.assertEqual([ref.upgrades.engine_fuel('amos', f) for f in (0, 5, 15, 20, 30)], [0, 5, 15, 20, 30])
        ref.upgrades.buy('amos', 'engines')
        self.assertEqual([ref.upgrades.engine_fuel('amos', f) for f in (0, 5, 15, 20, 30)], [0, 5, 15, 20, 30])
        self.assertEqual(ref.upgrades.buy('amos', 'engines')['kind'], 'upgrade_ok')
        self.assertEqual([ref.upgrades.engine_fuel('amos', f) for f in (0, 1, 5, 15, 20, 30)], [0, 1, 3, 9, 12, 18])
        rounds, d = dest_for(ref, 'amos')
        here = ref.get_vessel_location('amos')['station_id']
        full = get_route(here, d, ref.current_round)['fuel']
        fuel = ref.get_balance('amos', 'FUEL')
        p = ref.initiate_transit('amos', d)['payload']
        self.assertEqual(p['fuel_burned'], round(full * 0.6))
        self.assertEqual(ref.get_balance('amos', 'FUEL'), fuel - round(full * 0.6))
        self.assertEqual(p['arrival_round'] - p['departure_round'] - ((p.get('hazard') or {}).get('delay') or 0),
                         rounds - ref.upgrades.engine_cut('amos', rounds))
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_engines_fuel_cut_off_when_upgrades_off(self):
        ref = game(upgrades=False)
        self.assertEqual(ref.upgrades.engine_fuel('amos', 30), 30)

    def news(self, ref):
        import json
        return [json.loads(r[0]) for r in ref.conn.execute("SELECT payload FROM book_events WHERE kind = 'news'")
                if 'gn-shipyard-' in r[0]]

    def test_one_galnet_story_per_unlock(self):
        ref = game()
        seen = {}
        for _ in range(260):
            ref.step_round()
            for n in self.news(ref):
                seen.setdefault(n['id'], ref.current_round)
        want = {f"gn-shipyard-{k}-t{t}": at for at, kts in self.SCHEDULE.items() if at > 0 for k, t in kts}
        self.assertEqual(seen, want)
        self.assertEqual(len(self.news(ref)), len(want))  # once a game each
        by_id = {n['id']: n for n in self.news(ref)}
        self.assertIn('HARDENED HOLDS TIER 1', by_id['gn-shipyard-hold-t1']['headline'])
        self.assertTrue(any(e.id == 'gn-shipyard-engines-t2' for e in ref.galnet.events))
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_briefing_shows_each_tiers_lock(self):
        from agora.briefing import build_briefing
        ref = game()
        b = build_briefing(ref)
        self.assertIn('t1 4,000 x0.75 (locked until round 50)', b)
        self.assertIn('t1 3,000 x0.85 (on sale)', b)
        to_round(ref, 60)
        b = build_briefing(ref)
        self.assertIn('t1 4,000 x0.75 (on sale since round 50)', b)
        self.assertIn('t2 9,000 x0.45 (locked until round 125)', b)


if __name__ == '__main__':
    unittest.main()
