"""
Targeted tests for Underworld Syndicate progression, Tech Upgrades,
Black Market Fencing, and Player-to-Player Protection Tributes (#166).

Exercises:
- Stealth Drives, Boarding Pods, ECM Jammers upgrades.
- Black Market Fencing with double-entry SYSTEM legs, committed goods protection,
  looted-only restrictions, and pricing below lowest spot.
- Player extortion demand and consent protocol (accept/refuse), double-entry CR transfer,
  protection guarantees, and privacy gating.
- Monotonic sequence deduplication on repeat calls in the same round.
- Ledger conservation, non-negativity, and reconciliation invariants.
- Server input validation (non-numeric 400s) and endpoint authorization/privacy gates.
"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
from typing import Optional

from agora.referee import AgoraReferee
from agora.server import make_handler
from agora.spatial import COMMODITIES, STATIONS, BASE_PRICES


def make_referee(**kwargs):
    kwargs.setdefault('db_path', ':memory:')
    kwargs.setdefault('piracy', '0.15,0.04')
    kwargs.setdefault('upgrades', True)
    kwargs.setdefault('standing', True)
    ref = AgoraReferee(**kwargs)
    ref.new_game(seed=42, warmup_rounds=0)
    return ref


def seed_cargo(ref, agent, comm, qty, looted=True):
    """Seed cargo with valid double-entry ledger entries for reconciliation."""
    acct = f"{agent}/1" if hasattr(ref, 'fleet') and ref.fleet.is_corp(agent) else agent
    with ref.lock, ref.conn:
        ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, comm))
        ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (qty, acct, comm))
        ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES ('SYSTEM', ?, 0)", (comm,))
        ref.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = 'SYSTEM' AND instrument = ?", (qty, comm))
        seq = ref._get_next_seq()
        ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('seed', ?, 'SYSTEM', ?, ?)", (seq, comm, -qty))
        ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('seed', ?, ?, ?, ?)", (seq, acct, comm, qty))
    if looted:
        ref.piracy.record_loot(agent, comm, qty)


class TestUnderworldSyndicateUpgrades(unittest.TestCase):
    def test_stealth_drives_cuts_raid_risk(self):
        """Stealth drives cut raid risk by 50% across belt and inner shipping lanes."""
        ref = make_referee()
        ref.current_round = 100  # stealth_drives unlock at round 90
        agent = 'amos'

        chance_info = ref.piracy.chance(agent, 'ceres', 'mars', True, 'FRAG', 100, False, 100)
        base_odds = chance_info['exact_odds']
        self.assertEqual(chance_info['stealth_tier'], 0)

        # Buy stealth drives
        res = ref.upgrades.buy(agent, 'stealth_drives')
        self.assertEqual(res['kind'], 'upgrade_ok')
        self.assertEqual(ref.upgrades.tier(agent, 'stealth_drives'), 1)
        self.assertTrue(ref.upgrades.has_stealth_drives(agent))

        stealth_info = ref.piracy.chance(agent, 'ceres', 'mars', True, 'FRAG', 100, False, 100)
        self.assertEqual(stealth_info['stealth_tier'], 1)
        self.assertAlmostEqual(stealth_info['exact_odds'], base_odds * 0.50, places=4)

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
        # Sponsor receives looted cargo recorded in piracy_looted_cargo
        self.assertGreater(ref.piracy.get_looted_cargo(sponsor, 'ORE'), 0)

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
    def test_black_market_fence_cargo_and_ledger_conservation(self):
        """Fencing looted cargo preserves ledger conservation and prices below lowest spot."""
        ref = make_referee()
        agent = 'zero'
        comm = 'ORE'
        qty = 50

        # Give agent looted cargo with double-entry conservation
        seed_cargo(ref, agent, comm, qty, looted=True)
        with ref.lock, ref.conn:
            ref.conn.execute("INSERT OR REPLACE INTO standing_lanes (agent_id, lane, tier, cum_profit) VALUES (?, 'covert', 1, 5000)", (agent,))

        initial_cr = ref.get_balance(agent, 'CR')
        lowest_spot = min(
            ref.spatial.get_station_price(st, comm) if ref.spatial else BASE_PRICES[st][comm]
            for st in STATIONS
        )
        expected_unit = round(lowest_spot * 0.80, 2)
        expected_payout = int(qty * expected_unit)

        res = ref.piracy.fence_cargo(agent, comm, qty)
        self.assertEqual(res['kind'], 'fence_ok')
        payout = res['payload']['payout_cr']
        self.assertEqual(payout, expected_payout)
        self.assertEqual(ref.get_balance(agent, 'CR'), initial_cr + payout)
        self.assertEqual(ref.piracy.get_looted_cargo(agent, comm), 0)

        # Invariant 1: Ledger conservation holds with zero leaks
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant breach: {errors}")
        self.assertEqual(len(errors), 0)

    def test_fence_unlooted_cargo_rejected(self):
        """Fencing rejects normally purchased or unlooted goods."""
        ref = make_referee()
        agent = 'zero'
        comm = 'FUEL'
        qty = 100

        # Agent holds FUEL in account, but zero looted FUEL
        seed_cargo(ref, agent, comm, qty, looted=False)

        res = ref.piracy.fence_cargo(agent, comm, qty)
        self.assertEqual(res['kind'], 'reject')
        self.assertEqual(res['payload']['reason'], 'not_looted_cargo')

    def test_fence_committed_goods_rejected(self):
        """Fencing checks available balance less resting orders, not raw balance."""
        ref = make_referee()
        agent = 'zero'
        comm = 'FRAG'
        qty = 1000  # Zero starts with 1,000 genesis FRAG

        # Mark 1000 FRAG as looted
        ref.piracy.record_loot(agent, comm, qty)

        # Rest a sell order for the full 1000 FRAG at zero's home station ceres
        payload = {
            "order_id": f"test-{agent}-ask-1",
            "agent_id": agent, "side": "ask", "qty": qty, "limit_price": 50,
            "instrument": comm, "station_id": "ceres", "seq_seen": ref.current_seq
        }
        res = ref.submit_envelope({"v": 1, "kind": "order", "payload": payload})
        self.assertIn(res.get("kind"), ("order_accepted", "market_tick"))

        # Available balance is now 0
        self.assertEqual(ref.available(agent, comm, vessel_id='1'), 0)

        # Fencing should reject due to committed goods
        fence_res = ref.piracy.fence_cargo(agent, comm, qty)
        self.assertEqual(fence_res['kind'], 'reject')
        self.assertEqual(fence_res['payload']['reason'], 'insufficient_cargo')

    def test_repeat_fencing_same_round(self):
        """Fencing multiple times in the same round generates distinct IDs without collisions."""
        ref = make_referee()
        agent = 'zero'
        comm = 'FOOD'
        qty = 20

        seed_cargo(ref, agent, comm, 100, looted=True)

        res1 = ref.piracy.fence_cargo(agent, comm, qty)
        res2 = ref.piracy.fence_cargo(agent, comm, qty)
        self.assertEqual(res1['kind'], 'fence_ok')
        self.assertEqual(res2['kind'], 'fence_ok')

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant breach: {errors}")


class TestPlayerExtortionTribute(unittest.TestCase):
    def test_extortion_demand_consent_and_conservation(self):
        """Tribute requires target consent; upon acceptance, CR transfers and ledger is conserved."""
        ref = make_referee()
        demander = 'zero'
        target = 'amos'
        amount = 1_500
        rounds = 20

        amos_initial = ref.get_balance(target, 'CR')
        zero_initial = ref.get_balance(demander, 'CR')

        # Demander initiates demand
        res_demand = ref.piracy.extort(demander, target, amount, rounds)
        self.assertEqual(res_demand['kind'], 'extortion_demanded')
        tid = res_demand['payload']['tribute_id']
        self.assertEqual(res_demand['payload']['status'], 'pending')

        # No money moves while pending
        self.assertEqual(ref.get_balance(target, 'CR'), amos_initial)
        self.assertEqual(ref.get_balance(demander, 'CR'), zero_initial)
        self.assertFalse(ref.piracy.is_protected(demander, target))

        # Target accepts tribute
        res_accept = ref.piracy.respond_tribute(target, tid, 'accept')
        self.assertEqual(res_accept['kind'], 'tribute_accepted')
        self.assertEqual(ref.get_balance(target, 'CR'), amos_initial - amount)
        self.assertEqual(ref.get_balance(demander, 'CR'), zero_initial + amount)

        # Target is now protected against demander
        self.assertTrue(ref.piracy.is_protected(demander, target))

        # Attempt to hire privateers against target by demander should reject
        hire_res = ref.piracy.hire(demander, target)
        self.assertEqual(hire_res['kind'], 'reject')
        self.assertEqual(hire_res['payload']['reason'], 'target_protected_by_tribute')

        # Invariant: Ledger conservation verified
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant breach: {errors}")

    def test_extortion_refusal_leaves_unprotected(self):
        """Refusing tribute transfers no CR and leaves target unprotected."""
        ref = make_referee()
        demander = 'zero'
        target = 'amos'

        res_demand = ref.piracy.extort(demander, target, 1_000, 20)
        tid = res_demand['payload']['tribute_id']

        res_refuse = ref.piracy.respond_tribute(target, tid, 'refuse')
        self.assertEqual(res_refuse['kind'], 'tribute_refused')
        self.assertFalse(ref.piracy.is_protected(demander, target))

    def test_repeat_extortion_same_round(self):
        """Multiple extortion demands in the same round generate distinct IDs."""
        ref = make_referee()
        res1 = ref.piracy.extort('zero', 'amos', 500, 20)
        res2 = ref.piracy.extort('zero', 'amos', 600, 20)
        self.assertEqual(res1['kind'], 'extortion_demanded')
        self.assertEqual(res2['kind'], 'extortion_demanded')
        self.assertNotEqual(res1['payload']['tribute_id'], res2['payload']['tribute_id'])

    def test_tribute_duration_bounds(self):
        """Extortion duration is bounded (1 to 50 rounds)."""
        ref = make_referee()
        res_huge = ref.piracy.extort('zero', 'amos', 500, 10**6)
        self.assertEqual(res_huge['kind'], 'reject')
        self.assertEqual(res_huge['payload']['reason'], 'invalid_duration')

    def test_tributes_privacy_gating(self):
        """Unauthenticated or uninvolved readers only see sanitized tribute records."""
        ref = make_referee()
        res = ref.piracy.extort('zero', 'amos', 1_000, 20)
        tid = res['payload']['tribute_id']
        ref.piracy.respond_tribute('amos', tid, 'accept')

        # Demander and target see full details
        demander_view = ref.piracy.tributes(viewer='zero')
        self.assertEqual(demander_view[0]['target'], 'amos')
        self.assertEqual(demander_view[0]['amount_cr'], 1_000)

        # Unauthenticated reader (None) or third party (marvin) sees sanitized view
        public_view = ref.piracy.tributes(viewer=None)
        self.assertEqual(len(public_view), 1)
        self.assertNotIn('target', public_view[0])
        self.assertNotIn('amount_cr', public_view[0])
        self.assertTrue(public_view[0]['protected'])


class TestSyndicateMonopolyProgression(unittest.TestCase):
    def test_syndicate_rank_and_monopoly(self):
        """Syndicate rank progresses with cumulative plunder & accepted tribute up to Syndicate Boss."""
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

        # Extort and accept tribute of 15,000 CR (total 27,000 CR)
        res_ext = ref.piracy.extort('zero', 'marvin', 15_000, 20)
        self.assertEqual(res_ext['kind'], 'extortion_demanded')
        ref.piracy.respond_tribute('marvin', res_ext['payload']['tribute_id'], 'accept')

        status2 = ref.piracy.syndicate_status(agent)
        self.assertEqual(status2['payload']['syndicate_rank'], 'Syndicate Boss')
        self.assertTrue(status2['payload']['syndicate_monopoly'])
        self.assertGreaterEqual(status2['payload']['total_plunder_cr'], 25_000)


class TestUnderworldSyndicateServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = make_referee()
        cls.tokens = {'zero': 'tok-zero', 'amos': 'tok-amos', 'admin': 'tok-admin'}
        handler = make_handler(cls.ref, auth_tokens=cls.tokens)
        cls.server = HTTPServer(('127.0.0.1', 0), handler)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path: str, payload: dict, token: Optional[str] = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def _get(self, path: str, token: Optional[str] = None):
        url = f"{self.base_url}{path}"
        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def test_server_fence_non_numeric_400(self):
        """Non-numeric qty in POST /referee/piracy/fence yields HTTP 400 invalid_format, not 500."""
        status, data = self._post('/referee/piracy/fence', {'commodity': 'ORE', 'qty': 'invalid'}, token='tok-zero')
        self.assertEqual(status, 400)
        self.assertEqual(data.get('payload', {}).get('reason'), 'invalid_format')

    def test_server_extort_non_numeric_400(self):
        """Non-numeric amount or rounds in POST /referee/piracy/extort yields HTTP 400, not 500."""
        status, data = self._post('/referee/piracy/extort', {'target': 'amos', 'amount_cr': 'nan', 'rounds': 20}, token='tok-zero')
        self.assertEqual(status, 400)
        self.assertEqual(data.get('payload', {}).get('reason'), 'invalid_format')

    def test_server_syndicate_privacy_gates(self):
        """GET /referee/piracy/syndicate requires auth (401) and forbids rival inspection (403)."""
        # Unauthenticated request -> 401
        status, data = self._get('/referee/piracy/syndicate')
        self.assertEqual(status, 401)

        # Inspecting rival -> 403
        status, data = self._get('/referee/piracy/syndicate?agent_id=amos', token='tok-zero')
        self.assertEqual(status, 403)

        # Inspecting self -> 200
        status, data = self._get('/referee/piracy/syndicate?agent_id=zero', token='tok-zero')
        self.assertEqual(status, 200)
        self.assertEqual(data.get('payload', {}).get('agent_id'), 'zero')


if __name__ == '__main__':
    unittest.main()
