import unittest

from agora.referee import AgoraReferee
from agora.contracts import BOND_PCT, PENALTY, POST_EVERY


def game():
    ref = AgoraReferee(depots=True, contracts=True)
    ref.new_game(seed=3, warmup_rounds=2, depots=True, contracts=True)
    for _ in range(POST_EVERY):
        ref.step_round()
    return ref


def ok(ref):
    good, errs = ref.verify_ledger_invariants()
    return good, errs


class TestContracts(unittest.TestCase):
    def test_off_by_default(self):
        ref = AgoraReferee()
        ref.new_game(seed=3, warmup_rounds=2)
        for _ in range(POST_EVERY * 2):
            ref.step_round()
        self.assertEqual(ref.contract_desk.list(), [])

    def test_posts_and_claim_locks_deposit(self):
        ref = game()
        c = ref.contract_desk.list()[0]
        before = ref.get_balance('amos', 'CR')
        r = ref.contract_desk.claim('amos', c['contract_id'])
        self.assertEqual(r['kind'], 'contract_claim_ok', r)
        bond = int(c['price'] * c['qty_remaining'] * BOND_PCT)
        self.assertEqual(ref.get_balance('amos', 'CR'), before - bond)
        self.assertEqual(ref.contract_desk.claim('zero', c['contract_id'])['payload']['reason'], 'already_claimed')
        self.assertTrue(*ok(ref))

    def test_deposit_counts_toward_net_worth(self):
        ref = game()
        nw = lambda: {b['agent_id']: b['net_worth'] for b in ref.get_leaderboard()}['amos']
        before = nw()
        ref.contract_desk.claim('amos', ref.contract_desk.list()[0]['contract_id'])
        self.assertEqual(nw(), before)

    def test_deliver_pays_and_refunds_deposit_pro_rata(self):
        ref = game()
        c = ref.contract_desk.list()[0]
        cid = c['contract_id']
        ref.contract_desk.claim('amos', cid)
        # Put amos at the contract's station with the goods.
        with ref.conn:
            ref.conn.execute("UPDATE vessel_locations SET station_id = ? WHERE agent_id = 'amos'", (c['station_id'],))
        with ref.conn:
            ref.contract_desk._move('test-seed', (('amos', c['instrument'], c['qty_total']),
                                                  ('SYSTEM', c['instrument'], -c['qty_total'])))
        half = c['qty_total'] // 2
        cr = ref.get_balance('amos', 'CR')
        r = ref.contract_desk.deliver('amos', cid, half)
        self.assertEqual(r['kind'], 'contract_deliver_ok', r)
        bond = int(c['price'] * c['qty_total'] * BOND_PCT)
        self.assertEqual(r['payload']['bond_refund'], bond * half // c['qty_total'])
        self.assertEqual(ref.get_balance('amos', 'CR'), cr + half * c['price'] + r['payload']['bond_refund'])
        r = ref.contract_desk.deliver('amos', cid)
        self.assertEqual(r['payload']['status'], 'fulfilled')
        self.assertEqual(r['payload']['bond'], 0)
        self.assertTrue(*ok(ref))

    def test_only_owner_delivers_and_only_when_docked_there(self):
        ref = game()
        c = ref.contract_desk.list()[0]
        ref.contract_desk.claim('amos', c['contract_id'])
        self.assertEqual(ref.contract_desk.deliver('zero', c['contract_id'])['payload']['reason'], 'unauthorized')

    def test_lapse_forfeits_deposit_and_charges_penalty(self):
        ref = game()
        c = ref.contract_desk.list()[0]
        ref.contract_desk.claim('amos', c['contract_id'])
        cr = ref.get_balance('amos', 'CR')
        while ref.current_round <= c['deadline']:
            ref.step_round()
        row = ref.contract_desk.get(c['contract_id'])
        self.assertEqual(row['status'], 'lapsed')
        penalty = int(c['price'] * c['qty_total'] * PENALTY)
        self.assertEqual(row['penalty'], penalty)
        self.assertEqual(ref.get_balance('amos', 'CR'), cr - (penalty - row['shortfall']))
        self.assertTrue(*ok(ref))

    def test_resale_moves_price_and_deposit_to_seller(self):
        ref = game()
        c = ref.contract_desk.list()[0]
        cid = c['contract_id']
        ref.contract_desk.claim('amos', cid)
        self.assertEqual(ref.contract_desk.buy('zero', cid)['payload']['reason'], 'not_listed')
        ref.contract_desk.list_for_sale('amos', cid, 500)
        bond = ref.contract_desk.get(cid)['bond']
        a, z = ref.get_balance('amos', 'CR'), ref.get_balance('zero', 'CR')
        r = ref.contract_desk.buy('zero', cid)
        self.assertEqual(r['kind'], 'contract_buy_ok', r)
        self.assertEqual(r['payload']['owner'], 'zero')
        self.assertEqual(ref.get_balance('amos', 'CR'), a + 500 + bond)
        self.assertEqual(ref.get_balance('zero', 'CR'), z - 500 - bond)
        self.assertTrue(*ok(ref))

    def test_max_two_open(self):
        ref = game()
        while len(ref.contract_desk.list()) < 3:
            ref.step_round()
        ids = [c['contract_id'] for c in ref.contract_desk.list()]
        with ref.conn:
            ref.contract_desk._move('test-cash', (('amos', 'CR', 10_000_000), ('SYSTEM', 'CR', -10_000_000)))
        self.assertEqual(ref.contract_desk.claim('amos', ids[0])['kind'], 'contract_claim_ok')
        self.assertEqual(ref.contract_desk.claim('amos', ids[1])['kind'], 'contract_claim_ok')
        self.assertEqual(ref.contract_desk.claim('amos', ids[2])['payload']['reason'], 'too_many_contracts')

    def test_briefing_lists_contracts(self):
        from agora.briefing import build_briefing
        ref = game()
        text = build_briefing(ref)
        self.assertIn('## Station contracts', text)


if __name__ == '__main__':
    unittest.main()


import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from http.server import HTTPServer  # noqa: E402
from agora.server import make_handler  # noqa: E402


class TestContractEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = game()
        cls.server = HTTPServer(('127.0.0.1', 0), make_handler(cls.ref, auth_tokens={'amos': 'ta', 'zero': 'tz'}))
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

    def test_list_claim_sell_buy_over_http(self):
        with urllib.request.urlopen(self.base + '/referee/contracts', timeout=5) as resp:
            rows = json.loads(resp.read())['contracts']
        cid = rows[0]['contract_id']
        code, r = self._post(f'/referee/contracts/{cid}/claim', {}, 'ta')
        self.assertEqual(code, 200, r)
        code, r = self._post(f'/referee/contracts/{cid}/claim', {}, 'tz')
        self.assertEqual(code, 400)
        code, r = self._post(f'/referee/contracts/{cid}/list', {'price': 100}, 'ta')
        self.assertEqual(code, 200, r)
        code, r = self._post(f'/referee/contracts/{cid}/buy', {}, 'tz')
        self.assertEqual(code, 200, r)
        self.assertEqual(r['payload']['owner'], 'zero')
        with urllib.request.urlopen(self.base + '/referee/briefing', timeout=5) as resp:
            self.assertIn('Station contracts', resp.read().decode())
