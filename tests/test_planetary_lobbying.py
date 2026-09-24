"""
tests/test_planetary_lobbying.py - Planetary Council Lobbying & Regulatory Capture (#134).

Tests buying planetary council influence tokens, enacting deregulation packages
(circuit breaker suspension, targeted docking tariffs, idle fee exemptions),
and stock appreciation shocks.
"""

import unittest
from agora.referee import AgoraReferee
from agora.exchange import shock_for


def make_referee(**kwargs):
    kwargs.setdefault('db_path', ':memory:')
    kwargs.setdefault('events', True)
    ref = AgoraReferee(**kwargs)
    ref.new_game(seed=42, warmup_rounds=0)
    return ref


class TestPlanetaryLobbying(unittest.TestCase):
    def test_buy_influence_tokens(self):
        """Buying influence tokens deducts 500 CR per token and credits council influence."""
        ref = make_referee()
        agent = 'zero'
        st = 'ceres'

        initial_cr = ref.get_balance(agent, 'CR')
        res = ref.lobbying.buy_influence(agent, st, tokens=3)
        self.assertEqual(res['kind'], 'influence_buy_ok')
        self.assertEqual(res['payload']['tokens_bought'], 3)
        self.assertEqual(res['payload']['cost_cr'], 1500)
        self.assertEqual(ref.get_balance(agent, 'CR'), initial_cr - 1500)

        # Check status query
        status = ref.lobbying.get_influence(agent, st)
        self.assertEqual(status['tokens'], 3)
        self.assertEqual(status['total_spent_cr'], 1500)

    def test_buy_influence_rejects_insufficient_credits(self):
        """Cannot buy more influence tokens than available CR."""
        ref = make_referee()
        res = ref.lobbying.buy_influence('amos', 'mars', tokens=99999)
        self.assertEqual(res['kind'], 'reject')
        self.assertEqual(res['payload']['reason'], 'insufficient_credits')

    def test_circuit_breaker_suspension(self):
        """Enacting circuit breaker suspension disables volatility halts at the station."""
        ref = make_referee()
        agent = 'zero'
        st = 'mars'

        # Buy 3 tokens for suspension
        ref.lobbying.buy_influence(agent, st, tokens=3)
        res = ref.lobbying.enact_action(agent, 'circuit_breaker_suspension', st, rounds=5)
        self.assertEqual(res['kind'], 'lobbying_action_ok')
        self.assertEqual(res['payload']['tokens_spent'], 3)
        self.assertTrue(ref.lobbying.is_circuit_breaker_suspended(st))

        # Attempt to trigger halt on suspended station is bypassed
        halt_res = ref.circuit_breaker.trigger_halt(st, 'ORE', 50.0, 'test_spike', ref.current_round)
        self.assertFalse(halt_res['halted'])
        self.assertEqual(halt_res['reason'], 'circuit_breaker_suspended_by_council')
        self.assertFalse(ref.circuit_breaker.is_halted(st, 'ORE'))

    def test_targeted_docking_tariff(self):
        """Targeted docking tariff imposes a CR surcharge when rival vessel arrives at the station."""
        ref = make_referee()
        demander = 'zero'
        target = 'amos'
        st = 'earth'
        tariff_cr = 150

        # Buy 2 tokens
        ref.lobbying.buy_influence(demander, st, tokens=2)
        res = ref.lobbying.enact_action(demander, 'tariff', st, target=target, param_value=tariff_cr, rounds=10)
        self.assertEqual(res['kind'], 'lobbying_action_ok')
        self.assertEqual(ref.lobbying.get_docking_tariff(target, st), tariff_cr)

        # Setup transit arriving at earth for target amos
        amos_cr_before = ref.get_balance(target, 'CR')
        with ref.lock, ref.conn:
            ref.conn.execute(
                """INSERT INTO transits (transit_id, vessel_id, agent_id, origin, destination, commodity, cargo_qty, departure_round, arrival_round, status)
                   VALUES ('t-tariff-test', 'amos/1', 'amos', 'ceres', 'earth', 'ORE', 50, 0, 1, 'in_transit')"""
            )

        # Advance round to settle arrival
        ref.step_round()
        amos_cr_after = ref.get_balance(target, 'CR')
        self.assertEqual(amos_cr_after, amos_cr_before - tariff_cr)

    def test_idle_fee_exemption(self):
        """Idle fee exemption spares docked fleets from paying the 10 CR idle fee."""
        ref = make_referee(idle_fee=10)
        agent = 'zero'
        st = 'ceres'

        # Buy tokens and enact exemption
        ref.lobbying.buy_influence(agent, st, tokens=2)
        res = ref.lobbying.enact_action(agent, 'idle_fee_exemption', st, rounds=5)
        self.assertEqual(res['kind'], 'lobbying_action_ok')
        self.assertTrue(ref.lobbying.is_idle_exempt(agent))

        # Check idle fees charged during step_round
        idle_fees = ref._charge_idle_fees_locked(ref.current_round)
        self.assertNotIn(agent, idle_fees)

    def test_deregulation_stock_appreciation_shock(self):
        """Passing council deregulation triggers +3% equity stock shock."""
        ev = {'kind': 'deregulation_enacted', 'actor': 'zero'}
        shock = shock_for(ev)
        self.assertIsNotNone(shock)
        who, pct = shock
        self.assertEqual(who, 'zero')
        self.assertEqual(pct, 0.03)

    def test_actions_query(self):
        """Active lobbying actions can be queried by station."""
        ref = make_referee()
        ref.lobbying.buy_influence('zero', 'mars', tokens=5)
        ref.lobbying.enact_action('zero', 'circuit_breaker_suspension', 'mars', rounds=4)

        actions = ref.lobbying.get_active_actions('mars')
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['action_type'], 'circuit_breaker_suspension')
        self.assertEqual(actions[0]['agent_id'], 'zero')


if __name__ == '__main__':
    unittest.main()
