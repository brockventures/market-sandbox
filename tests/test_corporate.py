import unittest

from agora.referee import AgoraReferee
from agora.corporate import BANKRUPT_ROUNDS, TAKEOVER_SHARES


def game(**kw):
    ref = AgoraReferee(depots=True, rival_shares=100, corporate=True, contracts=True, **kw)
    ref.new_game(seed=4, warmup_rounds=2, depots=True, rival_shares=100, corporate=True, contracts=True)
    return ref


def move(ref, txn, legs):
    with ref.lock, ref.conn:
        ref.corporate._move(txn, legs)


def clean(t, ref):
    ok, errs = ref.verify_ledger_invariants()
    t.assertTrue(ok, errs)


class TestCorporate(unittest.TestCase):
    def test_off_by_default(self):
        ref = AgoraReferee()
        ref.new_game(seed=4, warmup_rounds=2)
        self.assertFalse(ref.corporate_enabled)
        self.assertIsNone(ref.fleet_out('amos'))

    def test_debt_paid_from_cash_first(self):
        ref = game()
        cr = ref.get_balance('amos', 'CR')
        with ref.lock, ref.conn:
            ref.corporate.add_debt('amos', 1000, 'test')
        ref.step_round()
        self.assertEqual(ref.corporate._row('amos')['debt'], 0)
        self.assertLessEqual(ref.get_balance('amos', 'CR'), cr - 1000 + 1000)  # idle fee may apply too
        clean(self, ref)

    def test_debt_beyond_cash_auctions_treasury_to_rivals(self):
        ref = game()
        cr = ref.get_balance('amos', 'CR')
        with ref.lock, ref.conn:
            ref.corporate.add_debt('amos', cr + 500_000, 'test')
        before = ref.get_balance('amos', 'EQ_AMOS')
        ref.step_round()
        self.assertLess(ref.get_balance('amos', 'EQ_AMOS'), before)
        self.assertGreater(sum(ref.get_balance(b, 'EQ_AMOS') for b in ('zero', 'marvin', 'aerial')), 300)
        self.assertLess(ref.corporate._row('amos')['debt'], cr + 500_000)
        clean(self, ref)

    def test_bankrupt_after_rounds_with_no_treasury(self):
        ref = game()
        # Hand amos's treasury away so nothing can be auctioned, then load debt.
        t = ref.get_balance('amos', 'EQ_AMOS')
        move(ref, 'test-strip', (('amos', 'EQ_AMOS', -t), ('SYSTEM', 'EQ_AMOS', t)))
        with ref.lock, ref.conn:
            ref.corporate.add_debt('amos', 10_000_000, 'test')
        for _ in range(BANKRUPT_ROUNDS + 1):
            ref.step_round()
        self.assertEqual(ref.corporate.status('amos'), 'bankrupt')
        self.assertEqual(ref.get_balance('amos', 'CR'), 0)
        r = ref.submit_envelope({"v": 1, "kind": "order", "payload": {
            "order_id": "x1", "agent_id": "amos", "side": "bid", "qty": 1, "limit_price": 5,
            "instrument": "FUEL", "station_id": "earth", "seq_seen": ref.current_seq}})
        self.assertEqual(r['payload']['reason'], 'fleet_out')
        self.assertEqual(ref.initiate_transit('amos', 'mars')['payload']['reason'], 'fleet_out')
        clean(self, ref)

    def test_takeover_at_51_percent_absorbs_everything(self):
        ref = game()
        need = TAKEOVER_SHARES - ref.get_balance('zero', 'EQ_MARV')
        move(ref, 'test-buyup', (('marvin', 'EQ_MARV', -need), ('zero', 'EQ_MARV', need)))
        m_cr = ref.get_balance('marvin', 'CR')
        z_cr = ref.get_balance('zero', 'CR')
        ref.step_round()
        self.assertEqual(ref.corporate.status('marvin'), 'absorbed')
        self.assertEqual(ref.get_balance('marvin', 'CR'), 0)
        self.assertGreaterEqual(ref.get_balance('zero', 'CR'), z_cr + m_cr - 50)  # idle fees
        self.assertTrue([e for e in ref.corporate.summary()['events'] if e['kind'] == 'takeover' and e['agent_id'] == 'zero'])
        self.assertEqual(ref.submit_envelope({"v": 1, "kind": "order", "payload": {
            "order_id": "x2", "agent_id": "marvin", "side": "bid", "qty": 1, "limit_price": 5,
            "instrument": "FUEL", "station_id": "earth", "seq_seen": ref.current_seq}})['payload']['reason'], 'fleet_out')
        clean(self, ref)

    def test_takeover_hands_over_cargo_in_flight(self):
        ref = game()
        frag = ref.get_balance('marvin', 'FRAG')
        self.assertGreater(frag, 0)
        here = ref.get_vessel_location('marvin')['station_id']
        dest = 'mars' if here != 'mars' else 'luna'
        self.assertEqual(ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=frag)['status'], 'in_transit')
        aboard = ref.conn.execute("SELECT cargo_qty FROM transits WHERE vessel_id = 'marvin/1' "
                                  "AND status = 'in_transit'").fetchone()[0]  # net of any hazard loss
        z_frag, z_loc = ref.get_balance('zero', 'FRAG'), ref.get_vessel_location('zero')
        need = TAKEOVER_SHARES - ref.get_balance('zero', 'EQ_MARV')
        move(ref, 'test-buyup2', (('marvin', 'EQ_MARV', -need), ('zero', 'EQ_MARV', need)))
        ref.step_round()
        self.assertEqual(ref.corporate.status('marvin'), 'absorbed')
        # #175: the ship is renamed into zero's fleet and keeps flying; its
        # cargo lands in its own hold on arrival.
        t = ref.conn.execute("SELECT agent_id, vessel_id, status FROM transits WHERE vessel_id = 'zero/2'").fetchone()
        self.assertEqual((t['agent_id'], t['status']), ('zero', 'in_transit'))
        self.assertEqual(ref.get_vessels('marvin'), [])
        for _ in range(12):
            ref.step_round()
        self.assertEqual(ref.get_balance('marvin', 'FRAG'), 0)
        self.assertEqual(ref.get_balance('zero/2', 'FRAG'), aboard)
        self.assertEqual(ref.get_balance('zero', 'FRAG'), z_frag + aboard)
        self.assertEqual(ref.get_vessel_location('zero').get('station_id'), z_loc.get('station_id'))
        v = ref.conn.execute("SELECT station_id, status FROM vessels WHERE vessel_id = 'zero/2'").fetchone()
        self.assertEqual((v['station_id'], v['status']), (dest, 'docked'))
        self.assertEqual(ref.get_vessel_location('marvin')['status'], 'no_ship')
        clean(self, ref)

    def test_last_corp_standing_wins(self):
        ref = game()
        for target in ('marvin', 'aerial', 'amos'):
            sym = {'marvin': 'EQ_MARV', 'aerial': 'EQ_AERL', 'amos': 'EQ_AMOS'}[target]
            need = TAKEOVER_SHARES - ref.get_balance('zero', sym)
            move(ref, f'test-buy-{target}', ((target, sym, -need), ('zero', sym, need)))
        ref.step_round()
        self.assertEqual(ref.corporate.summary()['winner'], 'zero')
        from agora.briefing import build_briefing
        self.assertIn('has won', build_briefing(ref))
        clean(self, ref)

    def test_unpaid_contract_penalty_becomes_debt(self):
        ref = game()
        for _ in range(4):
            ref.step_round()
        c = ref.contract_desk.list()[0]
        ref.contract_desk.claim('aerial', c['contract_id'])
        # Leave aerial nearly broke so the penalty cannot be paid.
        cr = ref.get_balance('aerial', 'CR')
        move(ref, 'test-drain', (('aerial', 'CR', -cr), ('SYSTEM', 'CR', cr)))
        while ref.current_round <= c['deadline']:
            ref.step_round()
        self.assertGreater(ref.contract_desk.get(c['contract_id'])['shortfall'], 0)
        ev = [e for e in ref.corporate.summary()['events'] if e['agent_id'] == 'aerial']
        self.assertTrue(ev)
        clean(self, ref)


if __name__ == '__main__':
    unittest.main()
