"""
tests/test_underworld_syndicate.py - Underworld Syndicate Progression (#166).

Tests syndicate vessel tech upgrades (boarding pods, ECM jammers, stealth drives),
shadow outpost black market fencing, player-to-player extortion tributes, and
syndicate monopoly progression.
"""

import unittest
from agora.referee import AgoraReferee
from agora.piracy import REF_PRICE
from agora.spatial import COMMODITIES


def make_referee(**kwargs):
    kwargs.setdefault('db_path', ':memory:')
    kwargs.setdefault('piracy', '0.15,0.04')
    kwargs.setdefault('upgrades', True)
    kwargs.setdefault('standing', True)
    ref = AgoraReferee(**kwargs)
    ref.new_game(seed=42, warmup_rounds=0)
    return ref


class TestUnderworldSyndicateUpgrades(unittest.TestCase):
    def test_stealth_drives_cuts_raid_risk(self):
        """Stealth drives cut raid risk by 50% across belt and inner shipping lanes."""
        ref = make_referee()
        ref.current_round = 100  # stealth_drives unlock at round 90
        agent = 'amos'

        base_odds = ref.piracy.chance(agent, 'ceres', 'mars', True, 'FRAG', 100, False, 100)['exact_odds']

        # Buy stealth drives
        res = ref.upgrades.buy(agent, 'stealth_drives')
        self.assertEqual(res['kind'], 'upgrade_ok')
        self.assertEqual(ref.upgrades.tier(agent, 'stealth_drives'), 1)
        self.assertTrue(ref.upgrades.has_stealth_drives(agent))

        stealth_odds = ref.piracy.chance(agent, 'ceres', 'mars', True, 'FRAG', 100, False, 100)['exact_odds']
        self.assertAlmostEqual(stealth_odds, base_odds * 0.50, places=4)

    def test_boarding_pods_plunder_yield(self):
        """Boarding pods boost cargo yield during raids (80% on fight lost, 40% on surrender)."""
        ref = make_referee()
        ref.current_round = 20  # boarding pods unlock at round 10
        sponsor = 'zero'
        victim = 'amos'

        # Buy boarding pods for sponsor
        res = ref.upgrades.buy(sponsor, 'boarding_pods')
        self.assertEqual(res['kind'], 'upgrade_ok')
        self.assertTrue(ref.upgrades.has_boarding_pods(sponsor))

        # Setup transit and mock raid with sponsor
        with ref.lock, ref.conn:
            ref.conn.execute(
                "INSERT INTO transits (transit_id, vessel_id, agent_id, origin, destination, commodity, cargo_qty, departure_round, arrival_round, status) "
                "VALUES ('t-test-bp', 'amos/1', 'amos', 'ceres', 'earth', 'ORE', 100, 20, 23, 'in_transit')"
            )
            ref.conn.execute(
                "INSERT INTO piracy_raids (transit_id, agent_id, round, origin, destination, commodity, cargo_qty, cargo_value, odds, escorted, ransom, surrender_qty, status, contract_id, sponsor) "
                "VALUES ('t-test-bp', 'amos', 20, 'ceres', 'earth', 'ORE', 100, 1000, 0.20, 0, 150, 25, 'pending', 'c-1', 'zero')"
            )

        row = ref.piracy._row('t-test-bp')
        # Surrender resolution: with boarding pods, steals 40% instead of 25%
        ref.piracy._resolve_locked(row, 'surrender')
        updated_row = ref.piracy._row('t-test-bp')
        self.assertEqual(updated_row['qty_taken'], 40)

    def test_ecm_jammers_cuts_trace_odds(self):
        """ECM jammers cut the sponsor trace odds in half."""
        ref = make_referee()
        ref.current_round = 70  # ecm_jammers unlock at round 60
        sponsor = 'zero'

        self.assertFalse(ref.upgrades.has_ecm_jammers(sponsor))
        res = ref.upgrades.buy(sponsor, 'ecm_jammers')
        self.assertEqual(res['kind'], 'upgrade_ok')
        self.assertTrue(ref.upgrades.has_ecm_jammers(sponsor))
        self.assertEqual(ref.upgrades.factor(sponsor, 'ecm_jammers'), 0.50)


class TestBlackMarketFencing(unittest.TestCase):
    def test_black_market_fence_cargo(self):
        """Fencing cargo pays out at black market rate and credits covert standing."""
        ref = make_referee()
        agent = 'zero'
        comm = 'ORE'
        qty = 50

        # Give agent cargo
        with ref.lock, ref.conn:
            ref.conn.execute("INSERT OR REPLACE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, ?)", (agent, comm, qty))
            # Also give standing capability shadow_fence
            ref.conn.execute("INSERT OR REPLACE INTO standing_lanes (agent_id, lane, tier, cum_profit) VALUES (?, 'covert', 1, 5000)", (agent,))

        initial_cr = ref.get_balance(agent, 'CR')
        res = ref.piracy.fence_cargo(agent, comm, qty)
        self.assertEqual(res['kind'], 'fence_ok')
        payout = res['payload']['payout_cr']

        expected_unit = round(REF_PRICE[comm] * 0.95, 2)
        expected_payout = int(qty * expected_unit)
        self.assertEqual(payout, expected_payout)
        self.assertEqual(ref.get_balance(agent, comm), 0)
        self.assertEqual(ref.get_balance(agent, 'CR'), initial_cr + payout)

    def test_fence_insufficient_cargo(self):
        """Fencing fails cleanly if agent does not hold enough cargo."""
        ref = make_referee()
        res = ref.piracy.fence_cargo('amos', 'FUEL', 99999)
        self.assertEqual(res['kind'], 'reject')
        self.assertEqual(res['payload']['reason'], 'insufficient_cargo')


class TestPlayerExtortionTribute(unittest.TestCase):
    def test_extortion_tribute_and_protection(self):
        """Tribute payments transfer CR and block privateer hiring against the target."""
        ref = make_referee()
        demander = 'zero'
        target = 'amos'
        amount = 1_500
        rounds = 20

        amos_initial = ref.get_balance(target, 'CR')
        zero_initial = ref.get_balance(demander, 'CR')

        res = ref.piracy.extort(demander, target, amount, rounds)
        self.assertEqual(res['kind'], 'extortion_ok')
        self.assertEqual(ref.get_balance(target, 'CR'), amos_initial - amount)
        self.assertEqual(ref.get_balance(demander, 'CR'), zero_initial + amount)

        # Target is now protected against demander
        self.assertTrue(ref.piracy.is_protected(demander, target))

        # Attempt to hire privateers against target by demander should reject
        hire_res = ref.piracy.hire(demander, target)
        self.assertEqual(hire_res['kind'], 'reject')
        self.assertEqual(hire_res['payload']['reason'], 'target_protected_by_tribute')

        # Tributes query returns active tribute
        tribs = ref.piracy.tributes(viewer=demander)
        self.assertEqual(len(tribs), 1)
        self.assertEqual(tribs[0]['target'], target)
        self.assertEqual(tribs[0]['amount_cr'], amount)


class TestSyndicateMonopolyProgression(unittest.TestCase):
    def test_syndicate_rank_and_monopoly(self):
        """Syndicate rank progresses with cumulative plunder & tribute up to Syndicate Boss monopoly."""
        ref = make_referee()
        agent = 'zero'

        status0 = ref.piracy.syndicate_status(agent)
        self.assertEqual(status0['payload']['syndicate_rank'], 'Street Freelancer')
        self.assertFalse(status0['payload']['syndicate_monopoly'])

        # Record loot in privateers
        with ref.lock, ref.conn:
            ref.conn.execute(
                "INSERT INTO piracy_privateers (contract_id, sponsor, target, start_round, expires_round, fee, loot_cr, loot_qty) "
                "VALUES ('pv-test', 'zero', 'amos', 1, 21, 750, 12000, 200)"
            )

        status1 = ref.piracy.syndicate_status(agent)
        self.assertEqual(status1['payload']['syndicate_rank'], 'Syndicate Enforcer')
        self.assertFalse(status1['payload']['syndicate_monopoly'])

        # Fund marvin so extortion succeeds
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = balance + 20000 WHERE agent_id = 'marvin' AND instrument = 'CR'")

        # Add extortion tribute of 15,000 CR (total 27,000 CR)
        res_ext = ref.piracy.extort('zero', 'marvin', 15_000, 20)
        self.assertEqual(res_ext['kind'], 'extortion_ok')

        status2 = ref.piracy.syndicate_status(agent)
        self.assertEqual(status2['payload']['syndicate_rank'], 'Syndicate Boss')
        self.assertTrue(status2['payload']['syndicate_monopoly'])
        self.assertGreaterEqual(status2['payload']['total_plunder_cr'], 25_000)


if __name__ == '__main__':
    unittest.main()
