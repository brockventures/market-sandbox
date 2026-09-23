"""Secrecy and exposure (#153): corp_events visibility, leaks, scandals, privateers, 20% stakes."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import HTTPServer
from unittest import mock

from agora import events as E
from agora import piracy as P
from agora.referee import AgoraReferee
from agora.server import make_handler, build_referee_from_env

CARGO = 1000


class Fixed:
    """Stand-in RNG: random() returns the queued values in turn (then the last one)."""
    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0) if len(self.vals) > 1 else self.vals[0]

    def randint(self, a, b):
        return a


def game(events=True, piracy=(0.0001, 0.0001), seed=7, **kw):
    ref = AgoraReferee(depots=True, depot_model='static', piracy=piracy, events=events, **kw)
    ref.new_game(seed=seed, warmup_rounds=2, depots=True, piracy=piracy, events=events, **kw)
    return ref


def record(ref, *a, **kw):
    with ref.lock, ref.conn:
        return ref.events.record_locked(*a, **kw)


def scandals(ref):
    return [e for e in ref.galnet.events if e.id.startswith('gn-scandal-')]


class TestVisibility(unittest.TestCase):
    def setUp(self):
        self.ref = game(piracy=None)
        r = self.ref
        self.pub = record(r, 'takeover', 'public', actor='zero', victim='amos', detail='zero took over amos')
        self.prv = record(r, 'privateer_raid', 'private', actor='zero', victim='amos',
                          detail='raided by privateers (sponsor unknown)', link='L1')
        self.sec = record(r, 'privateer_contract', 'secret', actor='zero', victim='amos',
                          detail='privateers hired against amos', link='L1')

    def ids(self, viewer):
        return {e['id'] for e in self.ref.events.visible_to(viewer)}

    def test_rules(self):
        self.assertEqual(self.ids(None), {self.pub})
        self.assertEqual(self.ids('marvin'), {self.pub})
        self.assertEqual(self.ids('zero'), {self.pub, self.prv, self.sec})
        self.assertEqual(self.ids('amos'), {self.pub, self.prv})
        self.assertEqual(self.ids('admin'), {self.pub, self.prv, self.sec})

    def test_victim_sees_that_not_who(self):
        ev = {e['id']: e for e in self.ref.events.visible_to('amos')}[self.prv]
        self.assertIsNone(ev['actor'])
        self.assertIsNone(ev['link'])
        self.assertTrue(ev['actor_hidden'])
        self.assertNotIn('zero', json.dumps(ev))
        mine = {e['id']: e for e in self.ref.events.visible_to('zero')}[self.prv]
        self.assertEqual(mine['actor'], 'zero')

    def test_expose_spreads_over_link_once(self):
        with self.ref.lock, self.ref.conn:
            ev = self.ref.events.expose_locked(self.sec, 'trace')
            again = self.ref.events.expose_locked(self.sec, 'trace')
            again_raid = self.ref.events.expose_locked(self.prv, 'leak')
        self.assertEqual(ev['exposed_by'], 'trace')
        self.assertIsNone(again)
        self.assertIsNone(again_raid)
        self.assertEqual(self.ids(None), {self.pub, self.prv, self.sec})
        ev = {e['id']: e for e in self.ref.events.visible_to('amos')}[self.prv]
        self.assertEqual(ev['actor'], 'zero')
        self.assertTrue(ev['exposed'])
        news = scandals(self.ref)
        self.assertEqual(len(news), 1)
        self.assertIn('ZERO FUNDED PRIVATEERS AGAINST AMOS', news[0].headline)
        # A later event on an exposed link is born exposed.
        late = record(self.ref, 'privateer_raid', 'private', actor='zero', victim='amos', detail='x', link='L1')
        self.assertIn(late, self.ids(None))

    def test_public_cannot_be_exposed(self):
        with self.ref.lock, self.ref.conn:
            self.assertIsNone(self.ref.events.expose_locked(self.pub, 'leak'))
        self.assertEqual(scandals(self.ref), [])

    def test_bad_visibility(self):
        with self.assertRaises(ValueError):
            record(self.ref, 'x', 'hidden', actor='zero')

    def test_corporate_summary_shows_only_known(self):
        kinds = [e['kind'] for e in self.ref.corporate.summary()['events']]
        self.assertEqual(kinds, ['takeover'])


class TestLockingWrappers(unittest.TestCase):
    def test_record_and_expose(self):
        ref = game(piracy=None)
        eid = ref.events.record('zero', 'amos', 'sabotage', 'secret', detail='a pump failed')
        self.assertEqual(ref.events.visible_to(None), [])
        self.assertEqual(ref.events.expose(eid, 'leak')['exposed_by'], 'leak')
        self.assertIsNone(ref.events.expose(eid, 'leak'))
        self.assertEqual(ref.events.visible_to(None)[0]['actor'], 'zero')


class TestLeaks(unittest.TestCase):
    def test_leak_roll_exposes_and_posts_scandal(self):
        ref = game(piracy=None)
        sec = record(ref, 'sabotage', 'secret', actor='zero', victim='amos', detail='a pump failed at amos')
        ref.events.rng = Fixed(0.0)
        rep = ref.step_round()
        self.assertEqual(rep['events']['leaked'], [sec])
        ev = ref.events.visible_to(None)[0]
        self.assertEqual((ev['id'], ev['exposed_by'], ev['actor']), (sec, 'leak', 'zero'))
        self.assertIn('ZERO SABOTAGED AMOS', scandals(ref)[0].headline)

    def test_no_leak_on_high_roll_and_window_closes(self):
        ref = game(piracy=None)
        sec = record(ref, 'sabotage', 'secret', actor='zero', victim='amos', detail='x')
        ref.events.rng = Fixed(0.99)
        for _ in range(E.LEAK_ROUNDS + 1):
            ref.step_round()
        ref.events.rng = Fixed(0.0)
        ref.step_round()  # the trail is cold: no more rolls
        self.assertNotIn(sec, {e['id'] for e in ref.events.visible_to(None)})

    def test_off_means_no_rolls(self):
        ref = game(events=False, piracy=None)
        sec = record(ref, 'sabotage', 'secret', actor='zero', victim='amos', detail='x')
        ref.events.rng = Fixed(0.0)
        self.assertIsNone(ref.step_round()['events'])
        self.assertNotIn(sec, {e['id'] for e in ref.events.visible_to(None)})

    def test_seeded_leaks_repeat(self):
        def run(seed):
            ref = game(piracy=None, seed=seed)
            for i in range(30):
                record(ref, 'sabotage', 'secret', actor='zero', victim='amos', detail=str(i))
            for _ in range(25):
                ref.step_round()
            return [(e['id'], e['exposed_round']) for e in ref.events.visible_to(None)]
        self.assertEqual(run(3), run(3))
        self.assertTrue(run(3))


class TestStakes(unittest.TestCase):
    def test_stake_crossing_20pct_is_public_once(self):
        ref = game(piracy=None, rival_shares=100, exchange_shares=100)
        ref.step_round()
        self.assertEqual([e for e in ref.events.visible_to(None) if e['kind'] == 'stake_20'], [])
        with ref.lock, ref.conn:
            ref.piracy._move('test-stake', (('marvin', 'EQ_AMOS', -100), ('zero', 'EQ_AMOS', 100)))
        rep = ref.step_round()
        self.assertEqual(len(rep['events']['stakes']), 1)
        ref.step_round()
        stakes = [e for e in ref.events.visible_to(None) if e['kind'] == 'stake_20']
        self.assertEqual(len(stakes), 1)
        self.assertEqual((stakes[0]['actor'], stakes[0]['victim']), ('zero', 'amos'))
        self.assertTrue(any('ZERO TAKES A 20% STAKE IN AMOS' in e.headline for e in ref.galnet.events))


class TestPrivateerSecrecy(unittest.TestCase):
    def raid(self, ref, trace):
        ref.piracy.rng = Fixed(0.0, 0.0 if trace else 0.9)
        return ref.initiate_transit('amos', 'mars', 'FRAG', CARGO)['payload']

    def test_contract_is_secret_raid_is_private(self):
        ref = game()
        ref.piracy.hire('zero', 'amos')
        self.assertEqual(ref.piracy.active_contracts(), [])
        self.assertEqual(ref.piracy.active_contracts('amos'), [])
        self.assertEqual(ref.piracy.active_contracts('zero')[0]['sponsor'], 'zero')
        self.assertEqual(len(ref.piracy.active_contracts('admin')), 1)
        p = self.raid(ref, trace=False)
        self.assertTrue(p['piracy']['demand']['sponsored'])  # the victim knows it was sponsored
        self.assertIsNone(p['piracy']['demand']['sponsor'])  # ...but not by whom
        pub = ref.piracy.status()['recent_raids'][0]
        self.assertIsNone(pub['sponsored'])
        self.assertIsNone(pub['sponsor'])
        self.assertTrue(ref.piracy.status('amos')['recent_raids'][0]['sponsored'])
        kinds = {e['kind']: e for e in ref.events.visible_to('amos')}
        self.assertIn('privateer_raid', kinds)
        self.assertNotIn('privateer_contract', kinds)
        self.assertIsNone(kinds['privateer_raid']['actor'])
        self.assertEqual(ref.events.visible_to(None), [])
        ticks = json.dumps(ref.get_ticks(0))
        self.assertNotIn('"sponsored": true', ticks)
        self.assertIn('"raided": true', ticks)
        from agora.briefing import build_briefing
        self.assertNotIn('zero', build_briefing(ref).split('## Piracy')[1].split('## ')[0].replace('Zero', ''))

    def test_trace_keeps_fine_and_exposes(self):
        ref = game()
        ref.piracy.hire('zero', 'amos')
        zc = ref.get_balance('zero', 'CR')
        self.raid(ref, trace=True)
        fine = min(P.PRIV_COST * P.PRIV_FINE, zc)
        self.assertEqual(ref.get_balance('zero', 'CR'), zc - fine)
        known = {e['kind']: e for e in ref.events.visible_to(None)}
        self.assertEqual(known['privateer_contract']['actor'], 'zero')
        self.assertEqual(known['privateer_contract']['exposed_by'], 'trace')
        self.assertEqual(known['privateer_raid']['actor'], 'zero')
        self.assertEqual(ref.piracy.active_contracts()[0]['sponsor'], 'zero')
        self.assertEqual(len(scandals(ref)), 1)
        # A second traced raid: fined again, no second scandal.
        for _ in range(4):
            ref.step_round()
        with ref.lock, ref.conn:
            ref.piracy._move('test-topup', (('SYSTEM', 'CR', -20_000), ('zero', 'CR', 20_000)))
        zc = ref.get_balance('zero', 'CR')
        ref.piracy.rng = Fixed(0.0, 0.0)
        self.assertTrue(ref.initiate_transit('amos', 'earth', 'FRAG', 100)['payload']['piracy']['raided'])
        self.assertEqual(ref.conn.execute("SELECT COUNT(*) FROM piracy_raids WHERE traced = 1").fetchone()[0], 2)
        self.assertEqual(ref.get_balance('zero', 'CR'), zc - P.PRIV_COST * P.PRIV_FINE)
        self.assertEqual(len(scandals(ref)), 1)
        good, errs = ref.verify_ledger_invariants()
        self.assertTrue(good, errs)

    def test_leaked_contract_unmasks_sponsor_without_fine(self):
        ref = game()
        ref.piracy.hire('zero', 'amos')
        self.raid(ref, trace=False)
        zc = ref.get_balance('zero', 'CR')
        ref.events.rng = Fixed(0.0)
        ref.step_round()
        self.assertEqual(ref.piracy.active_contracts()[0]['sponsor'], 'zero')
        self.assertEqual(ref.piracy.status()['recent_raids'][0]['sponsor'], 'zero')
        self.assertGreaterEqual(ref.get_balance('zero', 'CR'), zc - 10)  # an idle fee at most, no fine

    def test_events_off_keeps_old_behaviour(self):
        ref = game(events=False)
        ref.piracy.hire('zero', 'amos')
        self.raid(ref, trace=True)
        self.assertEqual(ref.conn.execute("SELECT COUNT(*) FROM corp_events").fetchone()[0], 0)
        self.assertEqual(scandals(ref), [])
        self.assertTrue(ref.piracy.status()['recent_raids'][0]['sponsored'])


class TestMigration(unittest.TestCase):
    def test_old_corp_events_table_gains_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'old.db')
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE corp_events (id INTEGER PRIMARY KEY AUTOINCREMENT, round INTEGER NOT NULL, "
                      "kind TEXT NOT NULL, agent_id TEXT NOT NULL, detail TEXT NOT NULL)")
            c.execute("INSERT INTO corp_events (round, kind, agent_id, detail) VALUES (3, 'debt', 'amos', 'owes 5 CR')")
            c.commit()
            c.close()
            ref = AgoraReferee(db_path=path, events=True)
            cols = {r[1] for r in ref.conn.execute("PRAGMA table_info(corp_events)")}
            self.assertTrue({'actor', 'victim', 'visibility', 'link', 'exposed_round', 'exposed_by'} <= cols)
            self.assertEqual(ref.events.visible_to(None)[0]['detail'], 'owes 5 CR')
            ref.conn.close()


class TestFlagAndHTTP(unittest.TestCase):
    def test_defaults(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGORA_')}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(AgoraReferee().events_enabled)
            ref = build_referee_from_env(':memory:')
        self.assertTrue(ref.events_enabled)
        ref.new_game(seed=1)
        self.assertTrue(ref.events_enabled)
        with mock.patch.dict(os.environ, {'AGORA_EVENTS': '0'}):
            self.assertFalse(build_referee_from_env(':memory:').events_enabled)

    def test_endpoint_filters_by_token(self):
        ref = game()
        ref.piracy.hire('zero', 'amos')
        ref.piracy.rng = Fixed(0.0, 0.9)
        ref.initiate_transit('amos', 'mars', 'FRAG', CARGO)
        server = HTTPServer(('127.0.0.1', 0), make_handler(
            ref, auth_tokens={'amos': 'ta', 'zero': 'tz', 'marvin': 'tm', 'admin': 'tadm', 'combine': 'tc'}))
        base = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def get(tok=None):
                req = urllib.request.Request(base + '/referee/corporate/events',
                                             headers={'Authorization': f'Bearer {tok}'} if tok else {})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.loads(r.read())
            self.assertEqual(get()['events'], [])
            self.assertEqual(get('tc')['events'], [])
            self.assertEqual(get('tm')['events'], [])
            amos = get('ta')['events']
            self.assertEqual([e['kind'] for e in amos], ['privateer_raid'])
            self.assertIsNone(amos[0]['actor'])
            self.assertEqual(sorted(e['kind'] for e in get('tz')['events']), ['privateer_contract', 'privateer_raid'])
            self.assertEqual(len(get('tadm')['events']), 2)
            self.assertTrue(get()['events_enabled'])
        finally:
            server.shutdown()
            server.server_close()

    def test_briefing_line(self):
        from agora.briefing import build_briefing
        ref = game()
        ref.piracy.hire('zero', 'amos')
        self.assertIn('## Secrets and scandals', build_briefing(ref))
        self.assertIn('privateers hired against amos', build_briefing(ref, viewer='zero'))
        self.assertNotIn('privateers hired against amos', build_briefing(ref, viewer='amos'))
        self.assertNotIn('## Secrets and scandals', build_briefing(game(events=False)))


if __name__ == '__main__':
    unittest.main()
