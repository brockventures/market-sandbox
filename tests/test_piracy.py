"""Piracy (#145): route risk, extortion choice, escorts, black market, privateers."""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest import mock

from agora.referee import AgoraReferee
from agora import piracy as P
from agora.server import make_handler, build_referee_from_env

CARGO = 1000                                   # genesis FRAG per fleet
VALUE = int(round(CARGO * P.REF_PRICE['FRAG']))  # 15,000 CR


def game(odds=(1, 1), seed=7, depot_model='static'):
    ref = AgoraReferee(depots=True, depot_model=depot_model, piracy=odds)
    ref.new_game(seed=seed, warmup_rounds=2, depots=True, piracy=odds)
    return ref


def clean(tc, ref):
    good, errs = ref.verify_ledger_invariants()
    tc.assertTrue(good, errs)


class Fixed:
    """Stand-in RNG: random() returns the queued values in turn (then the last one)."""
    def __init__(self, *vals, randint=None):
        self.vals, self.ri = list(vals), randint

    def random(self):
        return self.vals.pop(0) if len(self.vals) > 1 else self.vals[0]

    def randint(self, a, b):
        return self.ri if self.ri is not None else a


def move(ref, agent='amos', dest='mars', qty=CARGO, escort=False):
    return ref.initiate_transit(agent, dest, 'FRAG', qty, escort=escort)


class TestPiracy(unittest.TestCase):
    def test_off_by_default(self):
        env = {k: v for k, v in os.environ.items() if k != 'AGORA_PIRACY'}
        with mock.patch.dict(os.environ, env, clear=True):
            ref = AgoraReferee(depots=True)
        ref.new_game(seed=7, warmup_rounds=2, depots=True)
        self.assertFalse(ref.piracy.enabled)
        r = move(ref, escort=True)
        self.assertIsNone(r['payload']['piracy'])
        self.assertEqual(ref.conn.execute("SELECT COUNT(*) FROM piracy_raids").fetchone()[0], 0)
        self.assertEqual(ref.get_balance('amos', 'CR'), 10_000 - r['payload']['toll_paid'])  # no escort fee
        clean(self, ref)

    def test_live_server_default_on(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGORA_')}
        with mock.patch.dict(os.environ, env, clear=True):
            ref = build_referee_from_env(':memory:')
        self.assertEqual(ref.piracy.odds, (0.15, 0.04))
        with mock.patch.dict(os.environ, {'AGORA_PIRACY': '0'}):
            self.assertFalse(build_referee_from_env(':memory:').piracy.enabled)

    def test_certain_raid_creates_demand(self):
        ref = game()
        r = move(ref)
        pz = r['payload']['piracy']
        self.assertTrue(pz['raided'], pz)
        d = pz['demand']
        self.assertEqual(d['status'], 'pending')
        self.assertEqual(d['ransom'], int(VALUE * P.RANSOM_PCT))
        self.assertEqual(d['surrender_qty'], int(CARGO * P.SURRENDER_PCT))
        self.assertIn('/respond', d['respond'])
        clean(self, ref)

    def test_pay(self):
        ref = game()
        tid = move(ref)['payload']['transit_id']
        cr = ref.get_balance('amos', 'CR')
        r = ref.piracy.respond('amos', tid, 'pay')
        self.assertEqual(r['kind'], 'piracy_respond_ok', r)
        self.assertEqual(r['payload']['status'], 'paid')
        self.assertEqual(ref.get_balance('amos', 'CR'), cr - int(VALUE * P.RANSOM_PCT))
        self.assertEqual(ref.piracy.respond('amos', tid, 'pay')['payload']['reason'], 'already_resolved')
        while ref.get_vessel_location('amos')['status'] == 'in_transit':
            ref.step_round()
        self.assertEqual(ref.get_balance('amos', 'FRAG'), CARGO)
        clean(self, ref)

    def test_pay_rejected_when_short(self):
        ref = game()
        tid = move(ref)['payload']['transit_id']
        with ref.lock, ref.conn:
            ref.piracy._move('test-drain', (('amos', 'CR', -ref.get_balance('amos', 'CR') + 5), ('SYSTEM', 'CR', ref.get_balance('amos', 'CR') - 5)))
        r = ref.piracy.respond('amos', tid, 'pay')
        self.assertEqual(r['payload']['reason'], 'insufficient_credits')
        self.assertEqual(ref.piracy.respond('amos', tid, 'surrender')['kind'], 'piracy_respond_ok')
        clean(self, ref)

    def test_only_owner_responds(self):
        ref = game()
        tid = move(ref)['payload']['transit_id']
        self.assertEqual(ref.piracy.respond('zero', tid, 'pay')['payload']['reason'], 'unauthorized')
        self.assertEqual(ref.piracy.respond('amos', tid, 'flee')['payload']['reason'], 'invalid_choice')

    def test_surrender_fences_goods_at_ceres_depot(self):
        ref = game(depot_model='reactive')
        tid = move(ref)['payload']['transit_id']
        depot = ref.get_balance('depot_ceres', 'FRAG')
        shelf = ref._reactive['shelf'][('ceres', 'FRAG')]
        r = ref.piracy.respond('amos', tid, 'surrender')
        taken = int(CARGO * P.SURRENDER_PCT)
        self.assertEqual(r['payload']['qty_taken'], taken)
        self.assertEqual(r['payload']['fenced_at'], 'depot_ceres')
        self.assertEqual(ref.get_balance('depot_ceres', 'FRAG'), depot + taken)
        self.assertEqual(ref._reactive['shelf'][('ceres', 'FRAG')], shelf + taken)
        while ref.get_vessel_location('amos')['status'] == 'in_transit':
            ref.step_round()
        self.assertEqual(ref.get_balance('amos', 'FRAG'), CARGO - taken)
        clean(self, ref)

    def test_fight_escape(self):
        ref = game()
        tid = move(ref)['payload']['transit_id']
        arr = ref.conn.execute("SELECT arrival_round FROM transits WHERE transit_id = ?", (tid,)).fetchone()[0]
        ref.piracy.rng = Fixed(0.0)  # escape roll < 0.5
        r = ref.piracy.respond('amos', tid, 'fight')
        self.assertEqual(r['payload']['status'], 'escaped')
        self.assertEqual(r['payload']['qty_taken'], 0)
        self.assertEqual(ref.conn.execute("SELECT arrival_round FROM transits WHERE transit_id = ?", (tid,)).fetchone()[0], arr)
        clean(self, ref)

    def test_fight_lost(self):
        ref = game()
        tid = move(ref)['payload']['transit_id']
        arr = ref.conn.execute("SELECT arrival_round FROM transits WHERE transit_id = ?", (tid,)).fetchone()[0]
        depot = ref.get_balance('depot_ceres', 'FRAG')
        ref.piracy.rng = Fixed(0.9, randint=2)
        r = ref.piracy.respond('amos', tid, 'fight')
        self.assertEqual(r['payload']['status'], 'lost')
        self.assertEqual(r['payload']['qty_taken'], int(CARGO * P.FIGHT_LOSS))
        self.assertEqual(r['payload']['delay'], 2)
        self.assertEqual(ref.conn.execute("SELECT arrival_round FROM transits WHERE transit_id = ?", (tid,)).fetchone()[0], arr + 2)
        self.assertEqual(ref.get_balance('depot_ceres', 'FRAG'), depot + int(CARGO * P.FIGHT_LOSS))
        clean(self, ref)

    def test_no_answer_counts_as_fight(self):
        ref = game()
        tid = move(ref)['payload']['transit_id']
        ref.piracy.rng = Fixed(0.9, randint=1)
        rep = ref.step_round()
        row = ref.piracy._row(tid)
        self.assertEqual(row['status'], 'lost')
        self.assertEqual(row['choice'], 'fight')
        self.assertEqual(row['timed_out'], 1)
        self.assertEqual(rep['piracy']['timed_out'][0]['transit_id'], tid)
        self.assertEqual(ref.piracy.respond('amos', tid, 'pay')['payload']['reason'], 'already_resolved')
        clean(self, ref)

    def test_one_round_trip_fight_applies_before_arrival(self):
        ref = game()
        # Earth-Luna is a 1-round inner route.
        with ref.conn:
            ref.conn.execute("UPDATE vessel_locations SET station_id = 'earth' WHERE agent_id = 'amos'")
        tid = ref.initiate_transit('amos', 'luna', 'FRAG', CARGO)['payload']['transit_id']
        ref.piracy.rng = Fixed(0.9, randint=1)
        ref.step_round()
        self.assertEqual(ref.get_vessel_location('amos')['status'], 'in_transit')  # delayed a round
        ref.step_round()
        self.assertEqual(ref.get_balance('amos', 'FRAG'), CARGO - int(CARGO * P.FIGHT_LOSS))
        self.assertEqual(ref.piracy._row(tid)['status'], 'lost')
        clean(self, ref)

    def test_escort_cost_and_odds_cut(self):
        ref = game(odds=(0.1, 0.1))
        a = ref.piracy.chance('amos', 'ceres', 'mars', True, 'FRAG', CARGO, False, 1)
        b = ref.piracy.chance('amos', 'ceres', 'mars', True, 'FRAG', CARGO, True, 1)
        self.assertAlmostEqual(b['odds'], round(a['odds'] * (1 - P.ESCORT_CUT), 4), places=4)
        cr = ref.get_balance('amos', 'CR')
        r = move(ref, escort=True)
        fee = int(VALUE * P.ESCORT_PCT)
        self.assertEqual(r['payload']['piracy']['escort_fee'], fee)
        self.assertEqual(ref.get_balance('amos', 'CR'), cr - fee - r['payload']['toll_paid'])
        clean(self, ref)

    def test_escort_rejected_when_unaffordable(self):
        ref = game()
        with ref.lock, ref.conn:
            ref.piracy._move('test-drain', (('amos', 'CR', -(10_000 - 100)), ('SYSTEM', 'CR', 10_000 - 100)))
        r = move(ref, escort=True)
        self.assertEqual(r['kind'], 'reject')
        self.assertEqual(r['payload']['reason'], 'insufficient_credits_for_escort')
        self.assertEqual(ref.get_vessel_location('amos')['status'], 'docked')
        self.assertEqual(ref.get_balance('amos', 'CR'), 100)
        clean(self, ref)

    def test_value_and_route_scaling(self):
        ref = game(odds=(0.1, 0.02))
        rnd = 1
        hot = ref.piracy.hot_station(rnd)
        cold = [s for s in ('earth', 'luna', 'mars') if s != hot]
        small = ref.piracy.chance('amos', cold[0], cold[1], False, 'FRAG', 10, False, rnd)
        self.assertEqual(small['value_mult'], P.VALUE_MULT[0])
        self.assertAlmostEqual(small['odds'], 0.01)
        big = ref.piracy.chance('amos', cold[0], cold[1], True, 'FRAG', 100_000, False, rnd)
        self.assertAlmostEqual(big['odds'], 0.2)
        hot_trip = ref.piracy.chance('amos', hot, cold[0], True, 'FRAG', 100_000, False, rnd)
        self.assertTrue(hot_trip['hot'])
        self.assertAlmostEqual(hot_trip['odds'], 0.4)

    def test_hot_station_rotates_and_is_announced(self):
        ref = game(odds=(0.1, 0.1))
        seen = set()
        for _ in range(P.HOT_EVERY * 3):
            ref.step_round()
            seen.add((ref.current_round // P.HOT_EVERY, ref.piracy.hot_station(ref.current_round)))
        self.assertEqual(len(seen), 4)  # epochs 0..3 (round 1 through 60)
        news = [e for e in ref.galnet.events if e.id.startswith('gn-piracy-')]
        self.assertEqual(len(news), 4)
        self.assertIn(ref.piracy.hot_station(ref.current_round).upper(), news[-1].headline)

    def test_privateer_contract_sponsor_cut_and_privacy(self):
        ref = game(odds=(0.0001, 0.0001))
        self.assertEqual(ref.piracy.hire('zero', 'zero')['payload']['reason'], 'invalid_target')
        cr = ref.get_balance('zero', 'CR')
        r = ref.piracy.hire('zero', 'amos')
        self.assertEqual(r['kind'], 'privateer_hire_ok', r)
        self.assertEqual(ref.get_balance('zero', 'CR'), cr - P.PRIV_COST)
        self.assertEqual(ref.piracy.hire('zero', 'marvin')['payload']['reason'], 'contract_active')
        self.assertEqual(ref.piracy.hire('marvin', 'amos')['payload']['reason'], 'target_taken')
        c = ref.piracy.chance('amos', 'ceres', 'mars', True, 'FRAG', CARGO, False, ref.current_round)
        self.assertTrue(c['privateers'])
        self.assertGreaterEqual(c['odds'], P.PRIV_ADD)
        ref.piracy.rng = Fixed(0.0, 0.9)  # raid hits, not traced
        tid = move(ref)['payload']['transit_id']
        pub = ref.piracy.status()
        self.assertIsNone(pub['recent_raids'][0]['sponsor'])
        self.assertTrue(pub['recent_raids'][0]['sponsored'])
        self.assertIsNone(pub['privateer_contracts'][0]['sponsor'])
        self.assertEqual(ref.piracy.status('zero')['privateer_contracts'][0]['sponsor'], 'zero')
        depot = ref.get_balance('depot_ceres', 'FRAG')
        zf = ref.get_balance('zero', 'FRAG')
        ref.piracy.respond('amos', tid, 'surrender')
        taken = int(CARGO * P.SURRENDER_PCT)
        cut = int(taken * P.PRIV_SHARE)
        self.assertEqual(ref.get_balance('zero', 'FRAG'), zf + cut)
        self.assertEqual(ref.get_balance('depot_ceres', 'FRAG'), depot + taken - cut)
        from agora.briefing import build_briefing
        self.assertNotIn('zero sponsored', build_briefing(ref))
        clean(self, ref)

    def test_ransom_split_with_sponsor(self):
        ref = game(odds=(0.0001, 0.0001))
        ref.piracy.hire('zero', 'amos')
        ref.piracy.rng = Fixed(0.0, 0.9)
        tid = move(ref)['payload']['transit_id']
        zc = ref.get_balance('zero', 'CR')
        ref.piracy.respond('amos', tid, 'pay')
        self.assertEqual(ref.get_balance('zero', 'CR'), zc + int(int(VALUE * P.RANSOM_PCT) * P.PRIV_SHARE))
        clean(self, ref)

    def test_trace_fines_and_names_sponsor(self):
        ref = game(odds=(0.0001, 0.0001))
        ref.piracy.hire('zero', 'amos')
        zc = ref.get_balance('zero', 'CR')
        ref.piracy.rng = Fixed(0.0, 0.0)  # raid hits, traced
        tid = move(ref)['payload']['transit_id']
        fine = min(P.PRIV_COST * P.PRIV_FINE, zc)
        self.assertEqual(ref.get_balance('zero', 'CR'), zc - fine)
        self.assertEqual(ref.piracy.status()['recent_raids'][0]['sponsor'], 'zero')
        self.assertEqual(ref.piracy.status()['privateer_contracts'][0]['sponsor'], 'zero')
        from agora.briefing import build_briefing
        self.assertIn('zero sponsored the raid on amos', build_briefing(ref))
        self.assertEqual(ref.piracy._row(tid)['fine'], fine)
        clean(self, ref)

    def test_trace_fine_capped_at_balance(self):
        ref = game(odds=(0.0001, 0.0001))
        ref.piracy.hire('zero', 'amos')
        left = 1_000
        with ref.lock, ref.conn:
            z = ref.get_balance('zero', 'CR')
            ref.piracy._move('test-drain', (('zero', 'CR', -(z - left)), ('SYSTEM', 'CR', z - left)))
        ref.piracy.rng = Fixed(0.0, 0.0)
        move(ref)
        self.assertEqual(ref.get_balance('zero', 'CR'), 0)
        clean(self, ref)

    def test_contract_expires(self):
        ref = game(odds=(0.1, 0.1))
        ref.piracy.hire('zero', 'amos')
        for _ in range(P.PRIV_ROUNDS):
            ref.step_round()
        self.assertEqual(ref.piracy.active_contracts(), [])
        self.assertEqual(ref.piracy.hire('zero', 'amos')['kind'], 'privateer_hire_ok')

    def test_seeded_reproducibility(self):
        def run(seed):
            ref = game(odds=(0.4, 0.4), seed=seed)
            for rnd in range(30):
                for a in ('amos', 'zero', 'marvin', 'aerial'):
                    loc = ref.get_vessel_location(a)
                    if loc['status'] == 'docked':
                        dest = 'mars' if loc['station_id'] != 'mars' else 'earth'
                        ref.initiate_transit(a, dest, 'FRAG', min(200, ref.get_balance(a, 'FRAG')))
                ref.step_round()
            clean(self, ref)
            return [tuple(r[k] for k in ('agent_id', 'round', 'status', 'qty_taken', 'delay'))
                    for r in ref.conn.execute("SELECT * FROM piracy_raids ORDER BY round, agent_id")]
        a, b = run(11), run(11)
        self.assertTrue(a)
        self.assertEqual(a, b)
        self.assertNotEqual(a, run(12))

    def test_reset_wipes_tables(self):
        ref = game()
        move(ref)
        ref.piracy.hire('zero', 'marvin')
        ref.new_game(seed=8, warmup_rounds=1)
        self.assertEqual(ref.conn.execute("SELECT COUNT(*) FROM piracy_raids").fetchone()[0], 0)
        self.assertEqual(ref.conn.execute("SELECT COUNT(*) FROM piracy_privateers").fetchone()[0], 0)
        self.assertTrue(ref.piracy.enabled)  # setting kept across new_game

    def test_briefing_section(self):
        from agora.briefing import build_briefing
        ref = game(odds=(0.15, 0.04))
        text = build_briefing(ref)
        self.assertIn('## Piracy', text)
        self.assertIn(f"Hot station now: {ref.piracy.hot_station(0).capitalize()}", text)
        self.assertIn('/referee/privateers', text)

    def test_sibling_hooks(self):
        ref = game(odds=(0.1, 0.1))
        base = ref.piracy.chance('amos', 'ceres', 'mars', True, 'FRAG', CARGO, False, 1)['odds']
        ref.upgrades = mock.Mock()
        ref.upgrades.factor.return_value = 0.4
        self.assertAlmostEqual(ref.piracy.chance('amos', 'ceres', 'mars', True, 'FRAG', CARGO, False, 1)['odds'],
                               round(base * 0.4, 4), places=4)
        ref.fleet_out = lambda a: 'bankrupt' if a == 'zero' else None
        self.assertEqual(ref.piracy.hire('zero', 'amos')['payload']['reason'], 'fleet_out')
        self.assertEqual(ref.piracy.hire('amos', 'zero')['payload']['reason'], 'invalid_target')


class TestPiracyHTTP(unittest.TestCase):
    def setUp(self):
        self.ref = game()
        self.server = HTTPServer(('127.0.0.1', 0), make_handler(
            self.ref, auth_tokens={'amos': 'ta', 'zero': 'tz', 'combine': 'tc'}))
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, body, tok):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {tok}'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path, tok=None):
        req = urllib.request.Request(self.base + path, headers={'Authorization': f'Bearer {tok}'} if tok else {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_move_respond_privateers_over_http(self):
        code, r = self._post('/referee/privateers', {'target': 'amos'}, 'tz')
        self.assertEqual(code, 200, r)
        code, r = self._post('/referee/privateers', {'target': 'amos'}, 'tz')
        self.assertEqual(code, 400)
        self.ref.piracy.rng = Fixed(0.0, 0.9)  # raid, not traced
        code, r = self._post('/stations/transit', {'destination': 'mars', 'commodity': 'FRAG',
                                                   'cargo_qty': CARGO, 'escort': True}, 'ta')
        self.assertEqual(code, 200, r)
        pz = r['payload']['piracy']
        self.assertTrue(pz['escort'])
        self.assertTrue(pz['raided'])
        self.assertIsNone(pz['demand']['sponsor'])
        tid = r['payload']['transit_id']
        pub = self._get('/referee/piracy')
        self.assertTrue(pub['enabled'])
        self.assertIn(pub['hot_station'], ('earth', 'luna', 'mars', 'ceres'))
        self.assertIsNone(pub['privateer_contracts'][0]['sponsor'])
        self.assertNotIn('"zero"', json.dumps(pub['recent_raids']))
        self.assertEqual(self._get('/referee/piracy', 'tz')['privateer_contracts'][0]['sponsor'], 'zero')
        code, r = self._post(f'/referee/piracy/{tid}/respond', {'choice': 'pay'}, 'tz')
        self.assertEqual(code, 400)
        self.assertEqual(r['payload']['reason'], 'unauthorized')
        code, r = self._post(f'/referee/piracy/{tid}/respond', {'choice': 'surrender', 'agent_id': 'amos'}, 'tc')
        self.assertEqual(code, 200, r)
        self.assertEqual(r['payload']['status'], 'surrendered')
        with urllib.request.urlopen(self.base + '/referee/briefing', timeout=5) as resp:
            self.assertIn('## Piracy', resp.read().decode())
        clean(self, self.ref)


if __name__ == '__main__':
    unittest.main()
