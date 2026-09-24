"""
tests/test_corporate_loans_followups.py - Corporate Loans & Hostile M&A Follow-ups (#220).

Addresses re-audit findings from Mike Carmody (#220, follow-ups to #217/#164):
1. Medium: a borrower can default for free (auto-repay cancels resting bids; lender claim survives default; takeovers inherit loans).
2. Low-Medium: debt_buy moves a net-worth hit onto rival for free (leaderboard subtracts referee debt).
3. Low: server blanket except Exception replaced with (ValueError, TypeError, OverflowError) so internal bugs surface.
4. Low: parsers reject bools and fractions (shares=True, principal=999.9).
5. Low: unified net worth between stock_marks, pill, and auction pricing.
"""

import http.client
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
from unittest.mock import patch

from agora.corporate import _safe_int, _safe_float
from agora.order_book import Order
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


class TestCorporateLoansFollowups(unittest.TestCase):
    def test_auto_repay_cancels_resting_bids(self):
        """Auto-repay cancels resting bids to free up parked cash before declaring default (#220)."""
        ref = game()
        # amos offers 5,000 CR loan to marvin (due 6,000 CR in 3 rounds)
        res = ref.corporate.create_loan_offer('amos', 'marvin', principal=5000, interest_rate=0.20, due_rounds=3)
        self.assertEqual(res['kind'], 'loan_offer_ok')
        offer_id = res['payload']['offer_id']

        acc = ref.corporate.accept_loan_offer('marvin', offer_id)
        self.assertEqual(acc['kind'], 'loan_accept_ok')
        loan_id = acc['payload']['loan_id']
        self.assertEqual(ref.get_balance('marvin', 'CR'), 15000)

        # Marvin parks 15,000 CR in a resting bid on Ceres ORE order book
        order = Order(order_id='bid-1', agent_id='marvin', instrument='ORE', side='bid', qty=15000, limit_price=1, seq_seen=0)
        ref.books['ceres']['ORE'].bids.append(order)

        # Marvin's available cash is now 0, but total balance is 15,000 CR
        self.assertEqual(ref.peer._available('marvin', 'CR'), 0)
        self.assertEqual(ref.get_balance('marvin', 'CR'), 15000)

        amos_cr_before = ref.get_balance('amos', 'CR')

        # Advance 3 rounds to loan maturity
        ref.step_round()
        ref.step_round()
        ref.step_round()

        # Loan should NOT have defaulted! Auto-repay cancelled the resting bid and paid amos
        loan_row = ref.conn.execute("SELECT status FROM corp_predatory_loans WHERE loan_id = ?", (loan_id,)).fetchone()
        self.assertEqual(loan_row['status'], 'repaid')
        self.assertEqual(ref.corporate._row('marvin')['debt'], 0)
        self.assertEqual(ref.get_balance('amos', 'CR'), amos_cr_before + 6000)
        self.assertEqual(ref.get_balance('marvin', 'CR'), 9000)
        clean(self, ref)

    def test_defaulted_loan_claim_survives_and_routes_to_lender(self):
        """On default, the lender's claim survives: pay-down routes to lender, not SYSTEM (#220)."""
        ref = game()
        res = ref.corporate.create_loan_offer('amos', 'marvin', principal=5000, interest_rate=0.20, due_rounds=3)
        offer_id = res['payload']['offer_id']
        acc = ref.corporate.accept_loan_offer('marvin', offer_id)
        loan_id = acc['payload']['loan_id']

        # Completely drain marvin's cash so default is genuine
        m_cr = ref.get_balance('marvin', 'CR')
        move(ref, 'drain-marvin-full', (('marvin', 'CR', -m_cr), ('SYSTEM', 'CR', m_cr)))
        self.assertEqual(ref.get_balance('marvin', 'CR'), 0)

        # Step 3 rounds to maturity
        ref.step_round()
        ref.step_round()
        ref.step_round()

        # Loan defaulted and converted into referee debt
        loan_row = ref.conn.execute("SELECT status, due_amount FROM corp_predatory_loans WHERE loan_id = ?", (loan_id,)).fetchone()
        self.assertEqual(loan_row['status'], 'defaulted')
        self.assertEqual(loan_row['due_amount'], 6000)
        self.assertEqual(ref.corporate._row('marvin')['debt'], 6000)

        # Now Marvin receives cash (e.g. from operations or distress sales)
        move(ref, 'marvin-earns-cash', (('SYSTEM', 'CR', -6000), ('marvin', 'CR', 6000)))
        amos_cr_before = ref.get_balance('amos', 'CR')

        # Run debt pay-down
        with ref.lock:
            rem = ref.corporate._pay_down('marvin')
        self.assertEqual(rem, 0)
        self.assertEqual(ref.corporate._row('marvin')['debt'], 0)

        # Amos recovers the 6,000 CR payment!
        self.assertEqual(ref.get_balance('amos', 'CR'), amos_cr_before + 6000)

        # Loan is updated to repaid
        loan_row_after = ref.conn.execute("SELECT status, due_amount FROM corp_predatory_loans WHERE loan_id = ?", (loan_id,)).fetchone()
        self.assertEqual(loan_row_after['status'], 'repaid')
        self.assertEqual(loan_row_after['due_amount'], 0)
        clean(self, ref)

    def test_debt_buy_does_not_inflict_free_net_worth_hit(self):
        """Leaderboard subtracts referee debt so debt_buy does not move a free net-worth hit onto rivals (#220)."""
        ref = game()
        # Fund Amos first so cash injection doesn't skew rival equity holdings
        move(ref, 'fund-amos-debt-buy', (('SYSTEM', 'CR', -10000), ('amos', 'CR', 10000)))
        # Marvin incurs 4,000 CR referee debt (e.g. penalty)
        ref.corporate.add_debt('marvin', 4000, 'unpaid_contract_penalty')
        self.assertEqual(ref.corporate._row('marvin')['debt'], 4000)

        # Leaderboard reflects the 4,000 CR referee debt deduction
        board_before = {e['agent_id']: e for e in ref.get_leaderboard()}
        nw_marvin_before = board_before['marvin']['net_worth']
        nw_amos_before = board_before['amos']['net_worth']
        self.assertEqual(board_before['marvin']['debt'], 4000)

        # Amos buys 4,000 CR of Marvin's debt from SYSTEM
        buy_res = ref.corporate.buy_distressed_debt('amos', 'marvin', 4000)
        self.assertEqual(buy_res['kind'], 'debt_bought_ok')

        # Check leaderboard post-debt-buy
        board_after = {e['agent_id']: e for e in ref.get_leaderboard()}
        nw_marvin_after = board_after['marvin']['net_worth']
        nw_amos_after = board_after['amos']['net_worth']

        # Marvin's net worth is UNCHANGED! (Referee debt was replaced 1:1 by loan payable)
        self.assertEqual(nw_marvin_after, nw_marvin_before)
        # Amos paid 4,000 cash and gained 4,000 loan receivable -> net worth unchanged
        self.assertEqual(nw_amos_after, nw_amos_before)
        clean(self, ref)

    def test_takeover_reassigns_loans(self):
        """Raider absorbs target's loan obligations and creditor claims (#220)."""
        ref = game()
        sym_marv = 'EQ_MARV'
        # amos issues loan to marvin (5,000 CR principal, 6,000 CR due in 5 rounds)
        l_res = ref.corporate.create_loan_offer('amos', 'marvin', principal=5000, interest_rate=0.20, due_rounds=5)
        acc = ref.corporate.accept_loan_offer('marvin', l_res['payload']['offer_id'])
        loan_id = acc['payload']['loan_id']

        # zero takes over marvin by accumulating majority shares (600 shares)
        move(ref, 'zero-accum-marvin', (('marvin', sym_marv, -500), ('zero', sym_marv, 500)))
        self.assertGreaterEqual(ref.get_balance('zero', sym_marv), ref.corporate.takeover_threshold('marvin'))

        ref.step_round()
        self.assertEqual(ref.corporate.status('marvin'), 'absorbed')

        # The loan borrower is reassigned from marvin to zero!
        loan_row = ref.conn.execute("SELECT borrower FROM corp_predatory_loans WHERE loan_id = ?", (loan_id,)).fetchone()
        self.assertEqual(loan_row['borrower'], 'zero')

        # When the loan matures, zero auto-repays amos
        amos_cr_before = ref.get_balance('amos', 'CR')
        ref.step_round()
        ref.step_round()
        ref.step_round()
        ref.step_round()

        loan_row_matured = ref.conn.execute("SELECT status FROM corp_predatory_loans WHERE loan_id = ?", (loan_id,)).fetchone()
        self.assertEqual(loan_row_matured['status'], 'repaid')
        self.assertEqual(ref.get_balance('amos', 'CR'), amos_cr_before + 6000)
        clean(self, ref)

    def test_safe_int_and_safe_float_reject_bools_and_fractions(self):
        """_safe_int and _safe_float reject booleans and floats (#220)."""
        with self.assertRaises(ValueError):
            _safe_int(True, 'shares')
        with self.assertRaises(ValueError):
            _safe_int(False, 'shares')
        with self.assertRaises(ValueError):
            _safe_int(999.9, 'principal')
        with self.assertRaises(ValueError):
            _safe_int(10.0, 'shares')
        with self.assertRaises(ValueError):
            _safe_float(True, 'interest_rate')
        with self.assertRaises(ValueError):
            _safe_float(False, 'interest_rate')

        # Valid values succeed
        self.assertEqual(_safe_int(100, 'shares'), 100)
        self.assertEqual(_safe_int('100', 'shares'), 100)
        self.assertEqual(_safe_float(0.20, 'interest_rate'), 0.20)
        self.assertEqual(_safe_float('0.20', 'interest_rate'), 0.20)

    def test_http_parsers_reject_bools_and_fractions(self):
        """HTTP endpoints return 400 invalid_parameters for boolean and float inputs (#220)."""
        ref = game()
        handler_cls = make_handler(ref, auth_tokens=TOKENS)
        server = HTTPServer(('127.0.0.1', 0), handler_cls)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            # 1. shares=True on tender_offer
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/referee/corporate/tender_offer",
                headers={'Authorization': 'Bearer t-amos', 'Content-Type': 'application/json'},
                data=json.dumps({'target': 'marvin', 'price': 100, 'shares': True}).encode('utf-8')
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400)
            err_body = json.loads(ctx.exception.read().decode('utf-8'))
            self.assertEqual(err_body['payload']['reason'], 'invalid_parameters')

            # 2. principal=999.9 on loan_offer
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/referee/corporate/loan_offer",
                headers={'Authorization': 'Bearer t-amos', 'Content-Type': 'application/json'},
                data=json.dumps({'borrower': 'marvin', 'principal': 999.9, 'due_rounds': 5}).encode('utf-8')
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx2:
                urllib.request.urlopen(req2)
            self.assertEqual(ctx2.exception.code, 400)
            err_body2 = json.loads(ctx2.exception.read().decode('utf-8'))
            self.assertEqual(err_body2['payload']['reason'], 'invalid_parameters')
        finally:
            server.shutdown()
            server.server_close()

    def test_server_corporate_route_propagates_internal_bugs(self):
        """Server does not swallow internal server errors as 400 invalid_parameters (#220)."""
        ref = game()
        handler_cls = make_handler(ref, auth_tokens=TOKENS)
        server = HTTPServer(('127.0.0.1', 0), handler_cls)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            # Simulate an internal KeyError in create_tender_offer
            with patch.object(ref.corporate, 'create_tender_offer', side_effect=KeyError("simulated_internal_bug")):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/referee/corporate/tender_offer",
                    headers={'Authorization': 'Bearer t-amos', 'Content-Type': 'application/json'},
                    data=json.dumps({'target': 'marvin', 'price': 100, 'shares': 50}).encode('utf-8')
                )
                # An internal KeyError must NOT be caught as 400 invalid_parameters!
                # It raises out of the handler (server error / remote disconnect)
                with self.assertRaises((urllib.error.HTTPError, http.client.RemoteDisconnected)) as ctx:
                    urllib.request.urlopen(req)
                if isinstance(ctx.exception, urllib.error.HTTPError):
                    self.assertNotEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()

    def test_unified_net_worth_in_stock_marks(self):
        """Leaderboard net worth before stocks matches stock_marks NAV base (#220)."""
        ref = game()
        # Put marvin in debt and give amos a loan offer escrow
        ref.corporate.add_debt('marvin', 3000, 'unpaid_contract_penalty')
        move(ref, 'fund-amos-loan-escrow', (('SYSTEM', 'CR', -10000), ('amos', 'CR', 10000)))
        ref.corporate.create_loan_offer('amos', 'marvin', principal=4000, interest_rate=0.20, due_rounds=5)

        board = ref.get_leaderboard()
        board_dict = {e['agent_id']: e for e in board}

        # base before stocks
        base = {e['agent_id']: e['net_worth'] - e.get('stocks_value', 0) for e in board}
        marks = ref.stock_marks(base)

        # NAV for amos should reflect the loan offer escrow
        self.assertAlmostEqual(marks['EQ_AMOS']['nav'], round(base['amos'] / 1000, 2), places=2)
        # NAV for marvin should reflect the referee debt deduction
        self.assertAlmostEqual(marks['EQ_MARV']['nav'], round(base['marvin'] / 1000, 2), places=2)
        clean(self, ref)


if __name__ == '__main__':
    unittest.main()
