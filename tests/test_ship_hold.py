"""
tests/test_ship_hold.py - per-ship cargo hold size (#95 follow-up, agora/fleet.py).

Every ship's hold carries at most ref.ship_hold cargo units (SHIP_HOLD = 250
live). FUEL rides in the tank up to FUEL_TANK and only FUEL above that is
cargo. Covers the default and the env switch, genesis goods beyond one hold,
bids refused at placement (resting bids keep their room), depot fills,
ship-to-ship and station-hold transfers, peer collection, piracy and
sabotage loot, salvage bounties, takeovers, the backstops on NPC order flow and a
database from before the limit, and the API and briefing fields.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

from agora import covert as covert_mod
from agora import fleet as F
from agora import order_flow as OF
from agora import piracy as P
from agora.briefing import build_briefing, build_state
from agora.referee import AgoraReferee
from agora.server import build_referee_from_env, make_handler
from agora.spatial import STATIONS

HOLD = F.SHIP_HOLD


def game(hold=HOLD, **kw):
    kw.setdefault('depots', True)
    ref = AgoraReferee(ship_hold=hold, **kw)
    ref.new_game(seed=4, warmup_rounds=2, **{k: v for k, v in kw.items() if k != 'db_path'})
    return ref


def give(ref, acct, inst, qty):
    """Mint qty of inst to acct from SYSTEM, balanced (no hold check: a test fixture)."""
    with ref.lock, ref.conn:
        ref.fleet._move(f"test-give-{acct}-{inst}-{ref.current_seq}", ((acct, inst, qty), ('SYSTEM', inst, -qty)))


def order(ref, agent, side, qty, px, inst, st, vessel=None):
    p = {'order_id': f"t-{agent}-{side}-{inst}-{ref.current_seq}-{qty}", 'agent_id': agent, 'side': side,
         'qty': qty, 'limit_price': px, 'instrument': inst, 'station_id': st, 'seq_seen': ref.current_seq}
    if vessel is not None:
        p['vessel_id'] = vessel
    return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': p})


def reason(r):
    return (r.get('payload') or {}).get('reason') or r.get('reason')


def clean(t, ref):
    ok, errs = ref.verify_ledger_invariants()
    t.assertTrue(ok, errs)


def home(ref, agent='amos'):
    return ref.get_vessel_location(agent)['station_id']


def unload(ref, agent, inst, qty, vessel=None):
    """Move qty of inst off a ship into the corp's hold at its station."""
    vessel = vessel or f"{agent}/1"
    st = ref.fleet.station_of(vessel)
    r = ref.fleet.transfer(agent, vessel, f"@{st}", inst, qty)
    assert r['kind'] == 'transfer_ok', r
    return st


def depot_ask(ref, st, inst):
    return ref.get_depot_summary()['stations'][st][inst]['best_ask']


class TestDefaults(unittest.TestCase):
    def test_live_factory_defaults_to_the_constant_and_env_can_turn_it_off(self):
        self.assertEqual(F.SHIP_HOLD, 250)
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGORA_')}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(build_referee_from_env(':memory:').ship_hold, F.SHIP_HOLD)
        with mock.patch.dict(os.environ, dict(env, AGORA_SHIP_HOLD='0'), clear=True):
            ref = build_referee_from_env(':memory:')
        self.assertEqual(ref.ship_hold, 0)
        self.assertIsNone(ref.fleet.capacity('amos/1'))

    def test_bare_referee_has_no_limit(self):
        ref = AgoraReferee(depots=True)
        ref.new_game(seed=4, warmup_rounds=2, depots=True)
        self.assertEqual(ref.get_balance('amos/1', 'FRAG'), 1000)
        self.assertIsNone(ref.fleet.hold_status('amos/1')['hold_capacity'])

    def test_station_holds_have_no_limit(self):
        ref = game()
        self.assertEqual(ref.fleet.capacity('amos/1'), HOLD)
        self.assertIsNone(ref.fleet.capacity(F.hold_account('amos', home(ref))))

    def test_load_counts_fuel_only_above_the_tank(self):
        self.assertEqual(F.FleetDesk.load_of({'FRAG': 100, 'ORE': 20, 'FUEL': F.FUEL_TANK}), 120)
        self.assertEqual(F.FleetDesk.load_of({'FUEL': F.FUEL_TANK + 30}), 30)


class TestGenesis(unittest.TestCase):
    def test_genesis_goods_beyond_one_hold_wait_in_the_home_station_hold(self):
        ref = game()
        st = home(ref)
        self.assertEqual(ref.get_balance('amos/1', 'FRAG'), HOLD)
        self.assertEqual(ref.get_balance(F.hold_account('amos', st), 'FRAG'), 1000 - HOLD)
        self.assertEqual(ref.get_balance('amos', 'FRAG'), 1000)  # the corp total is unchanged
        self.assertEqual(ref.get_balance('amos/1', 'FUEL'), 500)  # the tank, not cargo
        h = ref.fleet.hold_status('amos/1')
        self.assertEqual((h['hold_used'], h['hold_capacity'], h['hold_free']), (HOLD, HOLD, 0))
        clean(self, ref)

    def test_an_old_database_is_unloaded_when_reopened_with_a_limit(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'agora.db')
            ref = AgoraReferee(db_path=db, depots=True)
            ref.new_game(seed=4, warmup_rounds=2, depots=True)
            st = home(ref)
            self.assertEqual(ref.get_balance('amos/1', 'FRAG'), 1000)
            ref.conn.close()
            ref = AgoraReferee(db_path=db, depots=True, ship_hold=HOLD)
            self.assertEqual(ref.get_balance('amos/1', 'FRAG'), HOLD)
            self.assertEqual(ref.get_balance(F.hold_account('amos', st), 'FRAG'), 1000 - HOLD)
            clean(self, ref)
            ref.conn.close()

    def test_the_invariant_catches_a_docked_ship_over_capacity(self):
        ref = game()
        give(ref, 'amos/1', 'ORE', 1)
        ok, errs = ref.verify_ledger_invariants()
        self.assertFalse(ok)
        self.assertTrue(any('Hold breach: amos/1' in e for e in errs), errs)


class TestBids(unittest.TestCase):
    def setUp(self):
        self.ref = game()
        self.st = home(self.ref)

    def test_a_bid_for_more_than_the_free_room_is_refused(self):
        ref, st = self.ref, self.st
        r = order(ref, 'amos', 'bid', 1, 1, 'ORE', st)
        self.assertEqual(r['kind'], 'reject', r)
        self.assertEqual(reason(r), 'hold_full')
        unload(ref, 'amos', 'FRAG', 100)
        self.assertEqual(reason(order(ref, 'amos', 'bid', 101, 1, 'ORE', st)), 'hold_full')
        self.assertNotEqual(order(ref, 'amos', 'bid', 100, 1, 'ORE', st)['kind'], 'reject')
        clean(self, ref)

    def test_resting_bids_keep_their_room(self):
        ref, st = self.ref, self.st
        unload(ref, 'amos', 'FRAG', 100)
        r = order(ref, 'amos', 'bid', 60, 1, 'ORE', st)  # far below the depot: rests
        self.assertEqual(r['payload'].get('order_status'), 'resting', r)
        h = ref.fleet.hold_status('amos/1')
        self.assertEqual((h['hold_used'], h['hold_reserved'], h['hold_free']), (HOLD - 100, 60, 40))
        self.assertEqual(reason(order(ref, 'amos', 'bid', 41, 1, 'FOOD', st)), 'hold_full')
        self.assertNotEqual(order(ref, 'amos', 'bid', 40, 1, 'FOOD', st)['kind'], 'reject')
        # Room kept for bids is not free for a transfer either.
        self.assertEqual(reason(ref.fleet.transfer('amos', f'@{st}', 'amos/1', 'FRAG', 1)), 'hold_full')
        clean(self, ref)

    def test_fuel_fills_the_tank_first(self):
        ref, st = self.ref, self.st
        unload(ref, 'amos', 'FRAG', 50)
        # The tank is full (500), so FUEL bought is cargo: 50 units of room.
        self.assertEqual(ref.fleet.room('amos/1', 'FUEL'), 50)
        self.assertEqual(reason(order(ref, 'amos', 'bid', 51, 1, 'FUEL', st)), 'hold_full')
        # Burn/sell FUEL below the tank and the tank takes it without hold room.
        unload(ref, 'amos', 'FUEL', 200)
        self.assertEqual(ref.fleet.room('amos/1', 'FUEL'), 250)
        self.assertEqual(ref.fleet.room('amos/1', 'ORE'), 50)

    def test_a_depot_fill_lands_exactly_the_room(self):
        ref, st = self.ref, self.st
        unload(ref, 'amos', 'FRAG', 80)
        px = depot_ask(ref, st, 'ORE')
        r = order(ref, 'amos', 'bid', 80, px, 'ORE', st)
        self.assertNotEqual(r['kind'], 'reject', r)
        self.assertEqual(ref.get_balance('amos/1', 'ORE'), 80)
        self.assertEqual(ref.fleet.hold_status('amos/1')['hold_used'], HOLD)
        self.assertEqual(reason(order(ref, 'amos', 'bid', 1, px, 'ORE', st)), 'hold_full')
        clean(self, ref)

    def test_asks_are_not_limited(self):
        ref, st = self.ref, self.st
        r = order(ref, 'amos', 'ask', HOLD, 9999, 'FRAG', st)
        self.assertNotEqual(r['kind'], 'reject', r)


class TestTransfers(unittest.TestCase):
    def setUp(self):
        self.ref = game()
        give(self.ref, 'amos', 'CR', 100_000)
        self.st = home(self.ref)
        self.assertEqual(self.ref.fleet.buy('amos')['kind'], 'ship_bought')

    def test_onto_an_empty_ship_up_to_its_capacity(self):
        ref, st = self.ref, self.st
        hold = f'@{st}'
        r = ref.fleet.transfer('amos', hold, 'amos/2', 'FRAG', HOLD + 1)
        self.assertEqual(reason(r), 'hold_full')
        self.assertIn('capacity 250', r['payload']['detail'])
        self.assertEqual(ref.fleet.transfer('amos', hold, 'amos/2', 'FRAG', HOLD)['kind'], 'transfer_ok')
        self.assertEqual(ref.get_balance('amos/2', 'FRAG'), HOLD)
        clean(self, ref)

    def test_onto_a_full_ship_is_refused_and_off_it_is_free(self):
        ref = self.ref
        self.assertEqual(reason(ref.fleet.transfer('amos', 'amos/2', 'amos/1', 'FRAG', 1)), 'insufficient_balance')
        give(ref, F.hold_account('amos', self.st), 'ORE', 10)
        self.assertEqual(reason(ref.fleet.transfer('amos', f'@{self.st}', 'amos/1', 'ORE', 1)), 'hold_full')
        self.assertEqual(ref.fleet.transfer('amos', 'amos/1', 'amos/2', 'FRAG', 100)['kind'], 'transfer_ok')
        self.assertEqual(ref.fleet.transfer('amos', f'@{self.st}', 'amos/1', 'ORE', 10)['kind'], 'transfer_ok')
        clean(self, ref)

    def test_fuel_moves_into_an_empty_tank_beyond_the_hold(self):
        ref = self.ref
        # amos/2 has an empty tank: 500 FUEL fits without touching its 250 hold.
        self.assertEqual(ref.fleet.transfer('amos', 'amos/1', 'amos/2', 'FUEL', 400)['kind'], 'transfer_ok')
        self.assertEqual(ref.fleet.hold_status('amos/2')['hold_used'], 0)


class TestUnsizedDeliveries(unittest.TestCase):
    """Loot, salvage and peer pickups fill the ship; the rest waits in the
    corp's station hold (FleetDesk.stow_locked). Nothing is destroyed."""

    def test_stow_splits_at_the_free_room(self):
        ref = game()
        st = home(ref)
        unload(ref, 'amos', 'FRAG', 30)
        with ref.lock:
            self.assertEqual(ref.fleet.stow_locked('amos/1', 'ORE', 100),
                             [('amos/1', 30), (F.hold_account('amos', st), 70)])
            self.assertEqual(ref.fleet.stow_locked('amos/1', 'ORE', 20), [('amos/1', 20)])

    def test_peer_collection_fills_the_ship_and_stows_the_rest(self):
        ref = game(peer_trades=True)
        st = home(ref)
        # zero offers 100 ORE at amos's station; amos (full) accepts while docked there.
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE vessels SET station_id = ?, status = 'docked' WHERE vessel_id = 'zero/1'", (st,))
        unload(ref, 'zero', 'FRAG', 100)
        give(ref, 'zero/1', 'ORE', 100)
        eid = ref.peer.offer('zero', st, 'ORE', 100, 5)['payload']['escrow_id']
        unload(ref, 'amos', 'FRAG', 40)
        r = ref.peer.accept('amos', eid)
        self.assertEqual(r['payload']['status'], 'collected', r)
        self.assertEqual(ref.get_balance('amos/1', 'ORE'), 40)
        self.assertEqual(ref.get_balance(F.hold_account('amos', st), 'ORE'), 60)
        self.assertEqual(ref.fleet.hold_status('amos/1')['hold_used'], HOLD)
        clean(self, ref)

    def test_piracy_sponsor_cut_overflows_to_the_station_hold(self):
        ref = game(piracy=(0.0001, 0.0001))
        zst = home(ref, 'zero')
        self.assertEqual(ref.piracy.hire('zero', 'amos')['kind'], 'privateer_hire_ok')
        ref.piracy.bags.force('raid', True)
        ref.piracy.bags.force('trace', False)
        tid = ref.initiate_transit('amos', next(s for s in STATIONS if s != home(ref)), 'FRAG', HOLD)['payload']['transit_id']
        zhold = F.hold_account('zero', zst)
        before = ref.get_balance(zhold, 'FRAG')
        ref.piracy.respond('amos', tid, 'surrender')
        cut = int(int(HOLD * P.SURRENDER_PCT) * P.PRIV_SHARE)
        self.assertGreater(cut, 0)
        # zero/1 is full of genesis FRAG: the whole cut waits at its station.
        self.assertEqual(ref.get_balance('zero/1', 'FRAG'), HOLD)
        self.assertEqual(ref.get_balance(zhold, 'FRAG'), before + cut)
        clean(self, ref)

    def test_sabotage_loot_overflows_to_the_station_hold(self):
        ref = game(events=True, corporate=True, piracy=(0.3, 0.1))
        ref.covert.bags.force('sabotage_trace', False)
        ref.initiate_transit('amos', next(s for s in STATIONS if s != home(ref)), 'FRAG', 200)
        zst = home(ref, 'zero')
        zhold = F.hold_account('zero', zst)
        before = ref.get_balance(zhold, 'FRAG')
        res = ref.covert.execute_sabotage('zero', 'amos', mode='transit')
        self.assertEqual(res['kind'], 'sabotage_ok', res)
        cut = int(int(200 * covert_mod.TRANSIT_SIPHON) * covert_mod.SABOTAGE_LOOT_SHARE)
        loot = res['payload']['loot']
        self.assertEqual(loot['qty'], cut)
        self.assertEqual(loot['stowed'], {zhold: cut})
        self.assertEqual(ref.get_balance('zero/1', 'FRAG'), HOLD)
        self.assertEqual(ref.get_balance(zhold, 'FRAG'), before + cut)
        clean(self, ref)

    def test_salvage_bounty_fills_the_salvager_and_stows_the_rest(self):
        ref = game()
        mst = home(ref, 'marvin')
        unload(ref, 'marvin', 'FRAG', 30)
        b = ref.broadcast_distress(agent_id='zero', location='ceres_mars', cargo_bounty={'FRAG': 100}, fuel_needed=30)
        r = ref.claim_salvage(salvager_id='marvin', beacon_id=b['beacon_id'])
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['cargo_claimed'], {'FRAG': 100})
        mhold = F.hold_account('marvin', mst)
        self.assertEqual(r['stowed'], {mhold: {'FRAG': 70}})
        self.assertEqual(ref.get_balance('marvin/1', 'FRAG'), HOLD)
        self.assertEqual(ref.get_balance(mhold, 'FRAG'), 1000 - HOLD + 30 + 70)
        clean(self, ref)


class TestTakeover(unittest.TestCase):
    def test_absorbed_ships_keep_their_cargo_and_station_holds_merge(self):
        # A takeover (#164) renames each ship 1:1 with its hold, so no ship
        # ends over capacity; station holds and the corp sweep (CR, shares)
        # have no size limit.
        ref = game(corporate=True, rival_shares=100, standing=True)
        m_st, z_st = home(ref, 'marvin'), home(ref, 'zero')
        sym = ref.corporate._sym('marvin')
        need = ref.corporate.takeover_threshold('marvin') - ref.get_balance('zero', sym)
        with ref.lock, ref.conn:
            ref.corporate._move('test-buyup', (('marvin', sym, -need), ('zero', sym, need)))
        ref.step_round()
        self.assertEqual(ref.corporate.status('marvin'), 'absorbed')
        self.assertEqual(ref.get_balance('zero/2', 'FRAG'), HOLD)  # ex-marvin/1, full as it was
        self.assertEqual(ref.get_balance('zero/1', 'FRAG'), HOLD)
        for vid in ('zero/1', 'zero/2'):
            self.assertLessEqual(ref.fleet.hold_status(vid)['hold_used'], HOLD)
        merged = (1000 - HOLD) * (2 if m_st == z_st else 1)
        self.assertEqual(ref.get_balance(F.hold_account('zero', m_st), 'FRAG'), merged)
        clean(self, ref)


class TestBackstops(unittest.TestCase):
    def test_npc_order_flow_clips_a_fill_to_the_room_left(self):
        # Placement keeps room for every resting bid, so this clip is a
        # backstop that play never reaches; a fixture takes the room away.
        with mock.patch.multiple(OF, FLOW_SCALE=1.0, FLOW_MAIN_SCALE=1.0, FLOW_JITTER=(1.0, 1.0)):
            ref = AgoraReferee(depots=True, asymmetric=True, depot_model='reactive', order_flow=True, ship_hold=HOLD)
            ref.new_game(seed=1, warmup_rounds=3)
            st = home(ref)
            unload(ref, 'amos', 'FRAG', ref.fleet.hold_status('amos/1')['hold_used'])
            q = ref.get_depot_summary()['stations'][st]['ORE']
            r = order(ref, 'amos', 'bid', 200, q['best_bid'] + 1, 'ORE', st)
            self.assertEqual(r['payload'].get('order_status'), 'resting', r)
            # Something outside the order fills the reserved room (a test
            # fixture: no real path can), then the station's sellers arrive.
            give(ref, 'amos/1', 'FOOD', 190)
            for _ in range(3):
                ref.step_round()
                self.assertLessEqual(ref.fleet.hold_status('amos/1')['hold_used'], HOLD)
            # 100 sellers a round arrive; only the 60 units of room fill.
            self.assertEqual(ref.order_flow.expected(st, 'ORE')['sell'], 100.0)
            self.assertEqual(ref.get_balance('amos/1', 'ORE'), HOLD - 190)
            clean(self, ref)

    def test_a_ship_landing_over_capacity_unloads_at_the_destination(self):
        ref = game()
        dest = next(s for s in STATIONS if s != home(ref))
        unload(ref, 'amos', 'FRAG', HOLD)
        tid = ref.initiate_transit('amos', dest)['payload']['transit_id']
        give(ref, 'amos/1', 'ORE', HOLD + 40)  # as if it took off before the limit
        for _ in range(40):
            if ref.get_vessel_location('amos').get('station_id') == dest:
                break
            ref.step_round()
        self.assertEqual(ref.fleet.station_of('amos/1'), dest, tid)
        self.assertEqual(ref.get_balance('amos/1', 'ORE'), HOLD)
        self.assertEqual(ref.get_balance(F.hold_account('amos', dest), 'ORE'), 40)
        clean(self, ref)


class TestAPIAndBriefing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = game()
        tokens = {'amos': 'ta', 'zero': 'tz', 'admin': 'tadm'}
        cls.server = HTTPServer(('127.0.0.1', 0), make_handler(cls.ref, auth_tokens=tokens))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path, token=None):
        req = urllib.request.Request(self.base + path, headers={'Authorization': f'Bearer {token}'} if token else {})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read()
            try:
                return json.loads(body)
            except ValueError:
                return body.decode()

    def test_vessels_list_and_fleet_carry_hold_fields(self):
        d = self._get('/referee/vessels?agent_id=amos')
        v = next(x for x in d['vessels'] if x['vessel_id'] == 'amos/1')
        self.assertEqual((v['hold_used'], v['hold_capacity']), (HOLD, HOLD))
        fleet = d['fleet']
        self.assertEqual((fleet['hold_per_ship'], fleet['fuel_tank']), (HOLD, F.FUEL_TANK))
        s = next(x for x in fleet['ships'] if x['vessel_id'] == 'amos/1')
        for k in ('hold_capacity', 'hold_used', 'hold_reserved', 'hold_free', 'fuel_tank', 'fuel'):
            self.assertIn(k, s)
        self.assertEqual((s['hold_capacity'], s['hold_used'], s['hold_free'], s['fuel']), (HOLD, HOLD, 0, 500))
        d = self._get('/referee/vessels')
        self.assertTrue(all('hold_used' in x and 'hold_capacity' in x for x in d['vessels']))

    def test_briefing_explains_the_limit_and_shows_the_viewers_holds(self):
        text = build_briefing(self.ref, viewer='amos')
        self.assertIn('Hold size.', text)
        self.assertIn(f'at most {HOLD} cargo units', text)
        self.assertIn('hold_full', text)
        self.assertIn(f'amos/1 {HOLD}/{HOLD} used', text)
        self.assertIn('Your ship starts with its hold full', text)
        self.assertNotIn('Your holds now', build_briefing(self.ref))
        ships = next(f for f in build_state(self.ref)['fleets'] if f['agent_id'] == 'amos')['ships']
        self.assertEqual((ships[0]['hold_used'], ships[0]['hold_capacity']), (HOLD, HOLD))

    def test_no_limit_briefing_says_nothing_about_hold_size(self):
        ref = AgoraReferee(depots=True)
        ref.new_game(seed=4, warmup_rounds=2, depots=True)
        text = build_briefing(ref, viewer='amos')
        self.assertNotIn('Hold size.', text)
        self.assertNotIn('Your ship starts with its hold full', text)


if __name__ == '__main__':
    unittest.main()
