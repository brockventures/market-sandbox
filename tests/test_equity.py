"""
tests/test_equity.py - Unit and integration tests for Syndicate Equity Short-Selling & Bilateral Borrow Fees (Task #23).
Tests genesis equity minting, bilateral loans, order book short sales, fee transfers, and margin liquidation.
"""

import unittest
from agora.referee import AgoraReferee
from agora.equity import (
    FLEET_EQUITIES, EQUITY_SYMBOLS, DEFAULT_BORROW_FEE_RATE,
    INITIAL_MARGIN_RATIO, MAINTENANCE_MARGIN_RATIO
)


class TestSyndicateEquityEngine(unittest.TestCase):
    def setUp(self):
        self.referee = AgoraReferee()

    def test_01_genesis_equities_and_invariants(self):
        """Verify genesis equities are seeded with strict double-entry conservation."""
        valid, errors = self.referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant breach: {errors}")
        self.assertEqual(len(errors), 0)

        # Verify amos, marvin, zero have 1000 shares of their own equity
        for issuer, conf in FLEET_EQUITIES.items():
            sym = conf["symbol"]
            bal = self.referee.get_balance(issuer, sym)
            self.assertEqual(bal, 1000, f"Expected {issuer} to hold 1000 shares of {sym}, got {bal}")

            # Verify SYSTEM holds -1000
            sys_bal = self.referee.get_balance("SYSTEM", sym)
            self.assertEqual(sys_bal, -1000)

    def test_02_equity_summary_telemetry(self):
        """Verify get_equity_summary returns correct NAV, market cap, and short interest."""
        summary = self.referee.get_equity_summary()
        self.assertEqual(len(summary), 3)

        for sym, data in summary.items():
            self.assertIn("spot_price", data)
            self.assertIn("nav_per_share", data)
            self.assertIn("market_cap", data)
            self.assertEqual(data["short_interest_shares"], 0)
            self.assertEqual(data["short_interest_pct"], 0.0)
            self.assertEqual(data["borrow_fee_rate"], DEFAULT_BORROW_FEE_RATE)

    def test_03_self_short_rejected(self):
        """An agent cannot short their own fleet equity."""
        res = self.referee.borrow_equity(
            borrower_id="amos",
            equity_symbol="EQ_AMOS",
            shares=50
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "self_short_prohibited")

    def test_04_insufficient_collateral_rejected(self):
        """Borrowing without sufficient margin collateral is rejected."""
        # Spot price ~20 CR, 100 shares = 2000 CR notional. 120% margin = 2400 CR.
        res = self.referee.borrow_equity(
            borrower_id="zero",
            equity_symbol="EQ_AMOS",
            shares=100,
            collateral_cr=1000  # < 2400 CR
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "insufficient_collateral_margin")

    def test_05_bilateral_borrow_and_ledger_settlement(self):
        """Verify successful stock loan locks collateral in escrow and transfers shares."""
        init_zero_cr = self.referee.get_balance("zero", "CR")
        init_amos_shares = self.referee.get_balance("amos", "EQ_AMOS")

        res = self.referee.borrow_equity(
            borrower_id="zero",
            equity_symbol="EQ_AMOS",
            shares=100,
            collateral_cr=5400,
            lender_id="amos"
        )
        self.assertTrue(res["ok"])
        loan_id = res["loan_id"]

        # Invariants must hold
        valid, errors = self.referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Invariant error after borrow: {errors}")

        # Zero paid 5400 CR collateral to SYSTEM
        self.assertEqual(self.referee.get_balance("zero", "CR"), init_zero_cr - 5400)
        # Amos lent 100 shares of EQ_AMOS to Zero
        self.assertEqual(self.referee.get_balance("amos", "EQ_AMOS"), init_amos_shares - 100)
        self.assertEqual(self.referee.get_balance("zero", "EQ_AMOS"), 100)

        # Check loan status
        loans = self.referee.get_equity_loans(borrower_id="zero")
        self.assertEqual(len(loans), 1)
        self.assertEqual(loans[0]["loan_id"], loan_id)
        self.assertEqual(loans[0]["status"], "active")
        self.assertEqual(loans[0]["shares"], 100)

        # Check short interest updated
        summary = self.referee.get_equity_summary()
        self.assertEqual(summary["EQ_AMOS"]["short_interest_shares"], 100)
        self.assertEqual(summary["EQ_AMOS"]["short_interest_pct"], 10.0)

    def test_06_short_sale_on_order_book(self):
        """Zero borrows EQ_AMOS and sells it on the order book to Marvin."""
        # 1. Zero borrows 50 shares of EQ_AMOS from Amos
        b_res = self.referee.borrow_equity("zero", "EQ_AMOS", 50, collateral_cr=2700, lender_id="amos")
        self.assertTrue(b_res["ok"])

        # 2. Zero sells 50 shares of EQ_AMOS at 25 CR on ceres
        ask_res = self.referee.submit_envelope({
            "v": 1, "kind": "order",
            "payload": {
                "order_id": "zero-short-sale-1",
                "agent_id": "zero",
                "station_id": "ceres",
                "instrument": "EQ_AMOS",
                "side": "ask",
                "qty": 50,
                "limit_price": 25,
                "seq_seen": 0
            }
        })
        self.assertEqual(ask_res["kind"], "market_tick")

        # 3. Marvin buys 50 shares of EQ_AMOS at 25 CR
        bid_res = self.referee.submit_envelope({
            "v": 1, "kind": "order",
            "payload": {
                "order_id": "marvin-buy-eq-1",
                "agent_id": "marvin",
                "station_id": "ceres",
                "instrument": "EQ_AMOS",
                "side": "bid",
                "qty": 50,
                "limit_price": 25,
                "seq_seen": 0
            }
        })
        self.assertEqual(bid_res["kind"], "market_tick")

        # Zero now has 0 EQ_AMOS and received 1250 CR cash
        self.assertEqual(self.referee.get_balance("zero", "EQ_AMOS"), 0)
        self.assertEqual(self.referee.get_balance("marvin", "EQ_AMOS"), 50)

        # Invariants strictly hold
        valid, errors = self.referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant error after short sale: {errors}")

    def test_07_round_step_and_bilateral_fee_distribution(self):
        """Verify borrow fees are transferred from borrower to lender on step_round."""
        self.referee.borrow_equity("zero", "EQ_AMOS", 100, collateral_cr=5400, lender_id="amos")

        init_zero_cr = self.referee.get_balance("zero", "CR")
        init_amos_cr = self.referee.get_balance("amos", "CR")

        # Step to round 2 (loan opened in round 0 or 1)
        step_res = self.referee.step_round(2)
        fee_reports = step_res.get("borrow_fee_reports", [])
        self.assertEqual(len(fee_reports), 1)
        report = fee_reports[0]
        self.assertEqual(report["fee_paid"], 108)  # 5400 * 0.02 = 108 CR
        self.assertEqual(report["lender_id"], "amos")

        # Zero paid 108 CR directly to Amos
        self.assertEqual(self.referee.get_balance("zero", "CR"), init_zero_cr - 108)
        self.assertEqual(self.referee.get_balance("amos", "CR"), init_amos_cr + 108)

        # Invariants hold
        valid, errors = self.referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant error after fee transfer: {errors}")

    def test_08_return_loan_releases_collateral(self):
        """Borrower returns shares to lender and recovers escrowed collateral."""
        b_res = self.referee.borrow_equity("zero", "EQ_AMOS", 100, collateral_cr=5400, lender_id="amos")
        loan_id = b_res["loan_id"]

        init_zero_cr = self.referee.get_balance("zero", "CR")
        init_amos_shares = self.referee.get_balance("amos", "EQ_AMOS")

        # Return loan
        ret_res = self.referee.return_equity_loan("zero", loan_id)
        self.assertTrue(ret_res["ok"])
        self.assertEqual(ret_res["shares_returned"], 100)
        self.assertEqual(ret_res["collateral_released"], 5400)

        # Zero got 5400 CR back, Amos got 100 shares back
        self.assertEqual(self.referee.get_balance("zero", "CR"), init_zero_cr + 5400)
        self.assertEqual(self.referee.get_balance("amos", "EQ_AMOS"), init_amos_shares + 100)
        self.assertEqual(self.referee.get_balance("zero", "EQ_AMOS"), 0)

        # Loan is closed
        loans = self.referee.get_equity_loans(borrower_id="zero")
        self.assertEqual(loans[0]["status"], "closed")

        # Invariants strictly hold
        valid, errors = self.referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant error after return: {errors}")

    def test_09_forced_margin_liquidation(self):
        """When collateral is insufficient or liquidated, lender receives forfeited collateral."""
        b_res = self.referee.borrow_equity("zero", "EQ_MARV", 100, collateral_cr=5400, lender_id="marvin")
        loan_id = b_res["loan_id"]

        init_marvin_cr = self.referee.get_balance("marvin", "CR")

        # Trigger liquidation
        liq_res = self.referee.equity.liquidate_loan(loan_id, reason="maintenance_margin_breach")
        self.assertTrue(liq_res["ok"])
        self.assertEqual(liq_res["collateral_forfeited"], 5400)

        # Marvin receives the 5400 CR collateral as settlement payout
        self.assertEqual(self.referee.get_balance("marvin", "CR"), init_marvin_cr + 5400)

        # Invariants strictly hold
        valid, errors = self.referee.verify_ledger_invariants()
        self.assertTrue(valid, f"Ledger invariant error after liquidation: {errors}")


if __name__ == '__main__':
    unittest.main()
