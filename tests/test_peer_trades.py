"""Remote fleet-to-fleet goods trades with station escrow (docs/fleet-market-spec.md section 1)."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from agora.peer import PICKUP_ROUNDS
from agora.referee import AgoraReferee
from agora.server import make_handler


def fresh(peer=True):
    ref = AgoraReferee(depots=True, peer_trades=peer)
    return ref


def dock(ref, agent, st):
    with ref.conn:
        # vessel_locations is a view of ship 1 since #175; its INSTEAD OF
        # INSERT trigger upserts, and an UPSERT cannot target a view.
        ref.conn.execute("INSERT INTO vessel_locations (agent_id, station_id, docked_since) VALUES (?, ?, 0)",
                         (agent, st))


def nw(ref):
    return {r['agent_id']: r['net_worth'] for r in ref.get_leaderboard()}


class TestPeerDesk(unittest.TestCase):
    def setUp(self):
        self.ref = fresh()
        dock(self.ref, 'amos', 'earth')
        dock(self.ref, 'zero', 'luna')

    def test_offer_requires_docked_at_station(self):
        r = self.ref.peer.offer('amos', 'mars', 'FUEL', 100, 12)
        self.assertEqual(r['payload']['reason'], 'vessel_not_docked')

    def test_remote_accept_then_collect_on_dock(self):
        ref = self.ref
        fuel0, cr_z0, cr_a0 = ref.get_balance('amos', 'FUEL'), ref.get_balance('zero', 'CR'), ref.get_balance('amos', 'CR')
        zfuel0 = ref.get_balance('zero', 'FUEL')
        worth0 = nw(ref)
        eid = ref.peer.offer('amos', 'earth', 'FUEL', 100, 12)['payload']['escrow_id']
        self.assertEqual(ref.get_balance('amos', 'FUEL'), fuel0 - 100)
        acc = ref.peer.accept('zero', eid)
        self.assertEqual(acc['payload']['status'], 'accepted')  # zero is at Luna: goods wait at Earth
        self.assertEqual(ref.get_balance('zero', 'CR'), cr_z0 - 1200)
        self.assertEqual(ref.get_balance('amos', 'CR'), cr_a0)  # seller paid on collection
        # FUEL scores 0 in net worth, so the sale shows up as CR only, already at acceptance.
        self.assertEqual(nw(ref)['amos'] - worth0['amos'], 1200)
        self.assertEqual(nw(ref)['zero'] - worth0['zero'], -1200)
        ref.step_round()
        self.assertEqual(ref.peer.get(eid)['status'], 'accepted')
        dock(ref, 'zero', 'earth')
        ref.step_round()
        self.assertEqual(ref.peer.get(eid)['status'], 'collected')
        self.assertEqual(ref.get_balance('amos', 'CR'), cr_a0 + 1200)
        self.assertEqual(ref.get_balance('zero', 'FUEL'), zfuel0 + 100)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_accept_while_docked_there_collects_immediately(self):
        dock(self.ref, 'zero', 'earth')
        z_fuel = self.ref.get_balance('zero', 'FUEL')
        eid = self.ref.peer.offer('amos', 'earth', 'FUEL', 50, 10)['payload']['escrow_id']
        self.assertEqual(self.ref.peer.accept('zero', eid)['payload']['status'], 'collected')
        self.assertEqual(self.ref.get_balance('zero', 'FUEL'), z_fuel + 50)

    def test_expiry_refunds_both_sides(self):
        ref = self.ref
        fuel0, cr0 = ref.get_balance('amos', 'FUEL'), ref.get_balance('zero', 'CR')
        eid = ref.peer.offer('amos', 'earth', 'FUEL', 100, 12)['payload']['escrow_id']
        ref.peer.accept('zero', eid)
        for _ in range(PICKUP_ROUNDS + 1):
            ref.step_round()
        self.assertEqual(ref.peer.get(eid)['status'], 'expired')
        self.assertEqual(ref.get_balance('amos', 'FUEL'), fuel0)
        self.assertEqual(ref.get_balance('zero', 'CR'), cr0)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_cancel_and_guards(self):
        ref = self.ref
        fuel0 = ref.get_balance('amos', 'FUEL')
        eid = ref.peer.offer('amos', 'earth', 'FUEL', 100, 12)['payload']['escrow_id']
        self.assertEqual(ref.peer.accept('amos', eid)['payload']['reason'], 'self_trade')
        self.assertEqual(ref.peer.cancel('zero', eid)['payload']['reason'], 'unauthorized')
        self.assertEqual(ref.peer.cancel('amos', eid)['payload']['status'], 'cancelled')
        self.assertEqual(ref.get_balance('amos', 'FUEL'), fuel0)
        self.assertEqual(ref.peer.offer('amos', 'earth', 'FUEL', 10 ** 7, 1)['payload']['reason'], 'insufficient_balance')

    def test_escrow_does_not_move_net_worth(self):
        ref = self.ref
        before = nw(ref)
        eid = ref.peer.offer('amos', 'earth', 'FRAG', 100, 11)['payload']['escrow_id']
        self.assertEqual(nw(ref), before)
        ref.peer.accept('zero', eid)
        after = nw(ref)
        # CR moved seller-ward and FRAG buyer-ward at the agreed price; totals only shift by price vs mark.
        self.assertEqual(sum(after.values()), sum(before.values()))

    def test_same_round_offers_ordered_deterministically(self):
        ref = self.ref
        eids = []
        for i in range(5):
            r = ref.peer.offer('amos', 'earth', 'FUEL', 10 + i, 10 + i)
            eids.append(r['payload']['escrow_id'])
        listed = [o['escrow_id'] for o in ref.peer.list('earth')]
        self.assertEqual(listed, eids)


class TestPeerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = fresh(peer=False)
        cls.server = HTTPServer(('127.0.0.1', 0), make_handler(cls.ref, auth_tokens={'amos': 'ta', 'zero': 'tz', 'combine': 'tc'}))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, body, tok):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {tok}'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_disabled_then_enabled_flow(self):
        code, _ = self._post('/referee/peer/offer', {'station_id': 'earth', 'instrument': 'FUEL', 'qty': 10, 'price': 9}, 'ta')
        self.assertEqual(code, 409)
        self.ref.peer_trades = True
        dock(self.ref, 'amos', 'earth')
        code, r = self._post('/referee/peer/offer', {'station_id': 'earth', 'instrument': 'FUEL', 'qty': 10, 'price': 9}, 'ta')
        self.assertEqual(code, 200, r)
        eid = r['payload']['escrow_id']
        with urllib.request.urlopen(self.base + '/referee/peer/offers', timeout=5) as resp:
            self.assertIn(eid, [o['escrow_id'] for o in json.loads(resp.read())['offers']])
        code, r = self._post('/referee/peer/accept', {'escrow_id': eid, 'agent_id': 'zero'}, 'tc')
        self.assertEqual(code, 200, r)
        self.assertEqual(r['payload']['buyer'], 'zero')
        with urllib.request.urlopen(self.base + '/referee/briefing', timeout=5) as resp:
            self.assertIn('Trades between fleets', resp.read().decode())


if __name__ == '__main__':
    unittest.main()
