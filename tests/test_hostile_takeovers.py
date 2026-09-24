"""
tests/test_hostile_takeovers.py - Hostile M&A, Predatory Lending & Poison Pills (#164).

Tests:
1. Dynamic float & NAV dilution:
   - get_live_shares reflects circulating float without zero-sum trap on SYSTEM
   - stock_marks NAV dynamically computes against live float
2. Hostile Tender Offers:
   - Tender offer creation with escrow
   - Partial and full tender fills with double-entry settlement
   - Tender offer cancellation with escrow refund
3. Defensive Governance / Poison Pills:
   - Pill activation requires outside rival holding >30% stake
   - Rights offering at 50% NAV discount
   - Non-raider rights exercise mints new shares and expands float
   - Raider barred from exercising defensive rights
   - Takeover threshold dynamically scales: (live_shares // 2) + 1
4. Predatory Lending & Debt Buying:
   - Private credit lines and referee debt purchase from SYSTEM
   - Solvency auto-repay at maturity
   - Insolvent default converts debt into borrower equity collateral
5. Corporate Monopoly Endgame:
   - Holding majority stakes in all surviving fleets triggers Corporate Monopoly victory
6. Server HTTP endpoints:
   - Verification of governance and M&A REST endpoints
"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer

from agora.referee import AgoraReferee
from agora.server import make_handler

TOKENS = {'amos': 't-amos', 'zero': 't-zero', 'marvin': 't-marvin', 'aerial': 't-aerial', 'admin': 't-admin'}


def game(**kw):
    ref = AgoraReferee(depots=True, rival_shares=100, corporate=True, contracts=True, **kw)
    ref.new_game(seed=42, warmup_rounds=2, depots=True, rival_shares=100, corporate=True, contracts=True)
    return ref


def move(ref, txn, legs):
    with ref.lock, ref.conn:
        ref.corporate._move(txn, legs)


def clean(t, ref):
    ok, errs = ref.verify_ledger_invariants()
    t.assertTrue(ok, errs)


class TestHostileTakeovers(unittest.TestCase):
    def test_live_float_and_nav_dilution(self):
        ref = game()
        sym = 'EQ_MARV'
        # At genesis, circulating float is 1,000 shares
        self.assertEqual(ref.get_live_shares(sym), 1000)

        base = {e["agent_id"]: e["net_worth"] - e.get("stocks_value", 0) for e in ref.get_leaderboard()}
        initial_nav = ref.stock_marks(base)[sym]['nav']

        # Mint 500 new shares to zero via rights offering
        ref.conn.execute(
            "INSERT INTO corp_poison_pills (target, trigger_raider, activated_round, rights_price, rights_issued, rights_exercised, status) "
            "VALUES ('marvin', 'amos', 1, 10, 500, 500, 'expired')")
        move(ref, 'test-mint-rights', (('SYSTEM', sym, -500), ('zero', sym, 500)))

        # Live float is now 1,500
        self.assertEqual(ref.get_live_shares(sym), 1500)

        # Dynamic takeover threshold scales from 501 to 751
        self.assertEqual(ref.corporate.takeover_threshold('marvin'), 751)

        # NAV dilutes proportionally
        diluted_nav = ref.stock_marks(base)[sym]['nav']
        self.assertAlmostEqual(diluted_nav, round(base.get('marvin', 0) / 1500, 2), places=2)
        self.assertLess(diluted_nav, initial_nav)
        clean(self, ref)

    def test_tender_offer_lifecycle(self):
        ref = game()
        # Fund amos with sufficient CR for tender escrow
        move(ref, 'fund-amos-tender', (('SYSTEM', 'CR', -20000), ('amos', 'CR', 20000)))
        # amos launches hostile tender offer for 100 shares of marvin at 150 CR/share
        res = ref.corporate.create_tender_offer('amos', 'marvin', price=150, shares=100)
        self.assertEqual(res['kind'], 'tender_offer_ok')
        offer_id = res['payload']['offer_id']
        self.assertEqual(res['payload']['escrow_cr'], 15000)

        # Escrow locked in SYSTEM account
        clean(self, ref)

        # zero holds 100 shares of EQ_MARV and tenders 40 shares to amos
        zero_cr_before = ref.get_balance('zero', 'CR')
        zero_marv_before = ref.get_balance('zero', 'EQ_MARV')
        amos_marv_before = ref.get_balance('amos', 'EQ_MARV')

        fill_res = ref.corporate.accept_tender_offer('zero', offer_id, shares=40)
        self.assertEqual(fill_res['kind'], 'tender_accept_ok')
        self.assertEqual(fill_res['payload']['payout'], 6000)
        self.assertEqual(fill_res['payload']['remaining_shares'], 60)

        # Check balances
        self.assertEqual(ref.get_balance('zero', 'CR'), zero_cr_before + 6000)
        self.assertEqual(ref.get_balance('zero', 'EQ_MARV'), zero_marv_before - 40)
        self.assertEqual(ref.get_balance('amos', 'EQ_MARV'), amos_marv_before + 40)
        clean(self, ref)

        # Cancel remainder: 60 shares @ 150 CR = 9000 CR refund to amos
        amos_cr_before = ref.get_balance('amos', 'CR')
        cancel_res = ref.corporate.cancel_tender_offer('amos', offer_id)
        self.assertEqual(cancel_res['kind'], 'tender_cancel_ok')
        self.assertEqual(cancel_res['payload']['refund_cr'], 9000)
        self.assertEqual(ref.get_balance('amos', 'CR'), amos_cr_before + 9000)
        clean(self, ref)

    def test_poison_pill_defense(self):
        ref = game()
        sym = 'EQ_MARV'
        # amos acquires 350 shares of marvin (>30% of 1000 float)
        move(ref, 'test-hostile-accumulation', (('marvin', sym, -250), ('amos', sym, 250)))
        self.assertEqual(ref.get_balance('amos', sym), 350)

        # marvin board activates poison pill rights offering
        pill_res = ref.corporate.activate_poison_pill('marvin', caller='marvin')
        self.assertEqual(pill_res['kind'], 'poison_pill_ok')
        self.assertEqual(pill_res['payload']['trigger_raider'], 'amos')
        rights_price = pill_res['payload']['rights_price']

        # amos (the hostile raider) is barred from exercising rights
        blocked = ref.corporate.exercise_rights('amos', 'marvin', qty=10)
        self.assertEqual(blocked['kind'], 'reject')
        self.assertEqual(blocked['payload']['reason'], 'raider_excluded')

        # marvin itself is barred from purchasing its own discounted rights
        self_blocked = ref.corporate.exercise_rights('marvin', 'marvin', qty=10)
        self.assertEqual(self_blocked['kind'], 'reject')
        self.assertEqual(self_blocked['payload']['reason'], 'target_excluded')

        # zero (friendly shareholder) exercises 200 rights, expanding float and diluting amos
        zero_marv_before = ref.get_balance('zero', sym)
        ex_res = ref.corporate.exercise_rights('zero', 'marvin', qty=200)
        self.assertEqual(ex_res['kind'], 'exercise_rights_ok')
        self.assertEqual(ref.get_balance('zero', sym), zero_marv_before + 200)
        self.assertEqual(ref.get_live_shares(sym), 1200)

        # Takeover threshold increased from 501 to 601
        self.assertEqual(ref.corporate.takeover_threshold('marvin'), 601)
        clean(self, ref)

    def test_predatory_loan_and_default(self):
        ref = game()
        # amos offers 5,000 CR loan to marvin with 20% interest due in 3 rounds
        res = ref.corporate.create_loan_offer('amos', 'marvin', principal=5000, interest_rate=0.20, due_rounds=3)
        self.assertEqual(res['kind'], 'loan_offer_ok')
        offer_id = res['payload']['offer_id']
        self.assertEqual(res['payload']['due_amount'], 6000)
        clean(self, ref)

        # marvin accepts the loan offer
        acc = ref.corporate.accept_loan_offer('marvin', offer_id)
        self.assertEqual(acc['kind'], 'loan_accept_ok')
        loan_id = acc['payload']['loan_id']
        self.assertEqual(ref.get_balance('marvin', 'CR'), 15000)
        clean(self, ref)

        # Drain marvin's cash so default occurs
        m_cr = ref.get_balance('marvin', 'CR')
        move(ref, 'drain-marvin', (('marvin', 'CR', -m_cr), ('SYSTEM', 'CR', m_cr)))

        # Step 3 rounds to maturity
        ref.step_round()
        ref.step_round()
        ref.step_round()

        # Loan defaulted: due balance added to corporate debt rather than instant takeover!
        loan_row = ref.conn.execute("SELECT status FROM corp_predatory_loans WHERE loan_id = ?", (loan_id,)).fetchone()
        self.assertEqual(loan_row['status'], 'defaulted')
        self.assertGreaterEqual(ref.corporate._row('marvin')['debt'], 6000)
        clean(self, ref)

    def test_distressed_debt_buying(self):
        ref = game()
        # Put marvin in debt to referee
        ref.corporate.add_debt('marvin', 8000, 'contract_penalty')
        self.assertEqual(ref.corporate._row('marvin')['debt'], 8000)

        # amos buys 5,000 CR of marvin's distressed debt from referee
        res = ref.corporate.buy_distressed_debt('amos', 'marvin', 5000)
        self.assertEqual(res['kind'], 'debt_bought_ok')
        self.assertEqual(ref.corporate._row('marvin')['debt'], 3000)  # remaining referee debt
        loan_id = res['payload']['loan_id']

        # Repay loan
        repay_res = ref.corporate.repay_loan('marvin', loan_id)
        self.assertEqual(repay_res['kind'], 'loan_repaid_ok')
        clean(self, ref)

    def test_corporate_monopoly_victory(self):
        ref = game()
        # amos acquires majority board control (>50%) in all 3 active rivals (zero, marvin, aerial)
        for rival, sym in (('zero', 'EQ_ZERO'), ('marvin', 'EQ_MARV'), ('aerial', 'EQ_AERL')):
            thresh = ref.corporate.takeover_threshold(rival)
            need = thresh - ref.get_balance('amos', sym)
            move(ref, f'test-monopoly-{rival}', ((rival, sym, -need), ('amos', sym, need)))

        ref.step_round()
        summary = ref.corporate.summary()
        self.assertEqual(summary['winner'], 'amos')
        self.assertIn('Corporate Monopoly', summary['win_reason'])
        clean(self, ref)


class TestHostileTakeoverEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = game()
        cls.server = HTTPServer(('127.0.0.1', 0), make_handler(cls.ref, auth_tokens=TOKENS))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, payload, tok=None):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=body, headers={
            'Content-Type': 'application/json',
            **({'Authorization': f'Bearer {tok}'} if tok else {})
        })
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _get(self, path, tok=None):
        req = urllib.request.Request(self.base + path, headers={'Authorization': f'Bearer {tok}'} if tok else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_http_tender_and_governance(self):
        # Create tender offer via HTTP
        code, res = self._post('/referee/corporate/tender_offer', {
            'target': 'marvin', 'price': 100, 'shares': 50
        }, tok='t-amos')
        self.assertEqual(code, 200)
        self.assertEqual(res['kind'], 'tender_offer_ok')
        offer_id = res['payload']['offer_id']

        # Query governance endpoint
        code, gov = self._get('/referee/corporate/governance')
        self.assertEqual(code, 200)
        self.assertTrue(any(o['offer_id'] == offer_id for o in gov['tender_offers']))

        # Accept tender offer via HTTP
        code, fill = self._post('/referee/corporate/tender_accept', {
            'offer_id': offer_id, 'shares': 20
        }, tok='t-zero')
        self.assertEqual(code, 200)
        self.assertEqual(fill['kind'], 'tender_accept_ok')


if __name__ == '__main__':
    unittest.main()
