"""
tests/test_planetary_lobbying.py - Planetary Council Lobbying & Regulatory Capture (#134).

Tests buying planetary council influence tokens, enacting deregulation packages
(circuit breaker suspension, targeted docking tariffs, idle fee exemptions),
bounds clamping, duplicate action rejection, shock cooldowns, and stock appreciation shocks.
"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
from typing import Optional

import agora.referee
from agora.referee import AgoraReferee
from agora.exchange import shock_for
from agora.lobbying import ACTIONS, DEREGULATION_SHOCK_COOLDOWN_ROUNDS
from agora.server import make_handler


def make_referee(**kwargs):
    kwargs.setdefault('db_path', ':memory:')
    kwargs.setdefault('events', True)
    ref = AgoraReferee(**kwargs)
    ref.new_game(seed=42, warmup_rounds=0)
    return ref


class TestPlanetaryLobbying(unittest.TestCase):
    def test_referee_docstring_preserved(self):
        """Module docstring on agora.referee is preserved as __doc__."""
        self.assertIsNotNone(agora.referee.__doc__)
        self.assertIn("Central referee for order validation", agora.referee.__doc__)

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

    def test_bounds_clamping_tariff_and_rounds(self):
        """Unbounded tariff param_value and duration rounds are clamped server-side."""
        ref = make_referee()
        agent = 'zero'
        st = 'ceres'
        target = 'amos'

        # Buy influence
        ref.lobbying.buy_influence(agent, st, tokens=10)

        # 1. Enact tariff with extreme 1e9 param_value and 100000 rounds
        res = ref.lobbying.enact_action(agent, 'tariff', st, target=target, param_value=1_000_000_000, rounds=100_000)
        self.assertEqual(res['kind'], 'lobbying_action_ok')
        self.assertEqual(res['payload']['param_value'], ACTIONS['tariff']['max_param'])  # 500
        self.assertEqual(res['payload']['duration_rounds'], ACTIONS['tariff']['max_rounds'])  # 20
        self.assertEqual(ref.lobbying.get_docking_tariff(target, st), 500)

        # 2. Enact circuit_breaker_suspension with 1000 rounds clamped to max_rounds (10)
        res_cb = ref.lobbying.enact_action(agent, 'circuit_breaker_suspension', st, rounds=1000)
        self.assertEqual(res_cb['kind'], 'lobbying_action_ok')
        self.assertEqual(res_cb['payload']['duration_rounds'], ACTIONS['circuit_breaker_suspension']['max_rounds'])  # 10

        # 3. Low / negative tariff param_value is clamped to min_param (10)
        ref.lobbying.buy_influence(agent, 'earth', tokens=2)
        res_low = ref.lobbying.enact_action(agent, 'tariff', 'earth', target=target, param_value=-50)
        self.assertEqual(res_low['kind'], 'lobbying_action_ok')
        self.assertEqual(res_low['payload']['param_value'], ACTIONS['tariff']['min_param'])  # 10

    def test_duplicate_active_action_rejected(self):
        """Cannot enact the same action while an identical one is currently active."""
        ref = make_referee()
        agent = 'zero'
        st = 'mars'

        ref.lobbying.buy_influence(agent, st, tokens=10)
        res1 = ref.lobbying.enact_action(agent, 'circuit_breaker_suspension', st, rounds=5)
        self.assertEqual(res1['kind'], 'lobbying_action_ok')

        # Second identical action while active is cleanly rejected
        res2 = ref.lobbying.enact_action(agent, 'circuit_breaker_suspension', st, rounds=5)
        self.assertEqual(res2['kind'], 'reject')
        self.assertEqual(res2['payload']['reason'], 'action_already_active')

        # Tariff on same target is rejected, but tariff on different target succeeds
        res_t1 = ref.lobbying.enact_action(agent, 'tariff', st, target='amos', rounds=5)
        self.assertEqual(res_t1['kind'], 'lobbying_action_ok')

        res_t1_dup = ref.lobbying.enact_action(agent, 'tariff', st, target='amos', rounds=5)
        self.assertEqual(res_t1_dup['kind'], 'reject')
        self.assertEqual(res_t1_dup['payload']['reason'], 'action_already_active')

        # Different target succeeds and gets a unique action_id
        res_t2 = ref.lobbying.enact_action(agent, 'tariff', st, target='marvin', rounds=5)
        self.assertEqual(res_t2['kind'], 'lobbying_action_ok')
        self.assertNotEqual(res_t1['payload']['action_id'], res_t2['payload']['action_id'])

    def test_shock_cooldown(self):
        """Deregulation stock appreciation shock has a cooldown to prevent paid stock pumping."""
        ref = make_referee()
        agent = 'zero'

        ref.lobbying.buy_influence(agent, 'ceres', tokens=3)
        ref.lobbying.buy_influence(agent, 'earth', tokens=3)

        # First enactment fires shock
        res1 = ref.lobbying.enact_action(agent, 'circuit_breaker_suspension', 'ceres', rounds=5)
        self.assertEqual(res1['kind'], 'lobbying_action_ok')
        self.assertTrue(res1['payload']['shock_fired'])

        # Immediate second enactment on earth does NOT fire shock due to cooldown
        res2 = ref.lobbying.enact_action(agent, 'circuit_breaker_suspension', 'earth', rounds=5)
        self.assertEqual(res2['kind'], 'lobbying_action_ok')
        self.assertFalse(res2['payload']['shock_fired'])

        # Advance rounds past cooldown
        for _ in range(DEREGULATION_SHOCK_COOLDOWN_ROUNDS):
            ref.step_round()

        # Enacting another action now fires shock again
        ref.lobbying.buy_influence(agent, 'mars', tokens=3)
        res3 = ref.lobbying.enact_action(agent, 'circuit_breaker_suspension', 'mars', rounds=5)
        self.assertEqual(res3['kind'], 'lobbying_action_ok')
        self.assertTrue(res3['payload']['shock_fired'])


class TestPlanetaryLobbyingServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.referee = AgoraReferee(db_path=':memory:', events=True)
        cls.referee.new_game(seed=42, warmup_rounds=0)
        cls.auth_tokens = {'zero': 'tok-zero', 'amos': 'tok-amos', 'admin': 'tok-admin'}
        handler_class = make_handler(cls.referee, auth_tokens=cls.auth_tokens)
        cls.server = HTTPServer(('127.0.0.1', 0), handler_class)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path: str, payload: dict, token: Optional[str] = 'tok-zero'):
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(url, data=data_bytes, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def test_http_rejects_non_integer_tokens(self):
        """POST /referee/lobbying/influence rejects non-integer tokens with 400."""
        status, data = self._post('/referee/lobbying/influence', {'station_id': 'ceres', 'tokens': 'not-an-int'})
        self.assertEqual(status, 400)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'invalid_format')

    def test_http_rejects_non_integer_rounds_and_param(self):
        """POST /referee/lobbying/action rejects non-integer rounds or param_value with 400."""
        status, data = self._post('/referee/lobbying/action', {
            'action_type': 'circuit_breaker_suspension',
            'station_id': 'ceres',
            'rounds': 'bad_round'
        })
        self.assertEqual(status, 400)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'invalid_format')

        status, data = self._post('/referee/lobbying/action', {
            'action_type': 'tariff',
            'station_id': 'ceres',
            'target': 'amos',
            'param_value': 'bad_param'
        })
        self.assertEqual(status, 400)
        self.assertEqual(data['kind'], 'reject')
        self.assertEqual(data['payload']['reason'], 'invalid_format')


if __name__ == '__main__':
    unittest.main()
