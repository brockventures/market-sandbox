"""
agora.equity - Corporate Warfare: Syndicate Equity Short-Selling & Bilateral Borrow Fees.

Defines tradeable synthetic fleet equities (EQ_AMOS, EQ_MARV, EQ_ZERO, EQ_AERL),
bilateral stock loan agreements, round-by-round borrow fee transfers,
and margin liquidation protocols for Task #23.
"""

import json
import math
import time
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple


FLEET_EQUITIES = {
    "amos": {
        "symbol": "EQ_AMOS",
        "name": "First Solvency Combine",
        "ticker": "AMOS",
        "total_shares": 1000,
        "base_nav": 20.0,
    },
    "marvin": {
        "symbol": "EQ_MARV",
        "name": "Ballistic Liquidation Co.",
        "ticker": "MARV",
        "total_shares": 1000,
        "base_nav": 20.0,
    },
    "zero": {
        "symbol": "EQ_ZERO",
        "name": "Apex Vector Arbitrage",
        "ticker": "ZERO",
        "total_shares": 1000,
        "base_nav": 20.0,
    }
}

EQUITY_SYMBOLS = [v["symbol"] for v in FLEET_EQUITIES.values()]
AGENT_BY_SYMBOL = {v["symbol"]: k for k, v in FLEET_EQUITIES.items()}
DEFAULT_BORROW_FEE_RATE = 0.02       # 2.0% per round paid directly to lender
INITIAL_MARGIN_RATIO = 1.20          # 120% CR collateral required to borrow
MAINTENANCE_MARGIN_RATIO = 1.05      # 105% minimum collateral maintenance margin


@dataclass
class EquityLoan:
    loan_id: str
    borrower_id: str
    lender_id: str
    equity_symbol: str
    shares: int
    collateral_cr: int
    fee_rate: float
    start_round: int
    status: str
    created_at: str
    closed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SyndicateEquityEngine:
    """
    Manages synthetic fleet equities, bilateral borrow transactions,
    fee distributions, and margin liquidation audits.
    """

    def __init__(self, conn, referee=None):
        self.conn = conn
        self.referee = referee
        self.default_fee_rate = DEFAULT_BORROW_FEE_RATE

    def get_equity_summary(self) -> Dict[str, Any]:
        """Returns market cap, NAV per share, short interest, and borrow stats for all equities."""
        cur = self.conn.cursor()

        # Calculate NAV per share from each issuer's current net worth
        summaries = {}
        for agent_id, conf in FLEET_EQUITIES.items():
            sym = conf["symbol"]
            total_shares = conf["total_shares"]

            # Query agent balances
            cur.execute("""
                SELECT instrument, balance FROM accounts WHERE agent_id = ?
            """, (agent_id,))
            rows = cur.fetchall()
            bals = {r[0]: r[1] for r in rows}

            liquid = bals.get('CR', bals.get('CREDITS', 10000))
            frags = bals.get('FRAG', bals.get('BANANA', 1000))
            fuel = bals.get('FUEL', 500)

            # Mark prices
            frag_mark = 10.0
            fuel_mark = 15.0
            if self.referee and hasattr(self.referee, 'spatial'):
                prices = self.referee.spatial.get_prices().get('ceres', {})
                frag_mark = prices.get('FRAG', 10.0)
                fuel_mark = prices.get('FUEL', 15.0)

            net_worth = liquid + (frags * frag_mark) + (fuel * fuel_mark)
            nav_per_share = max(1.0, round(net_worth / total_shares, 2))

            # Active short interest
            cur.execute("""
                SELECT COALESCE(SUM(shares), 0), COUNT(*)
                FROM equity_loans
                WHERE equity_symbol = ? AND status = 'active'
            """, (sym,))
            short_row = cur.fetchone()
            short_shares = short_row[0] if short_row else 0
            active_loans_count = short_row[1] if short_row else 0

            short_interest_pct = round((short_shares / total_shares) * 100, 1)

            # Spot price: check order book if referee available, fallback to NAV
            spot_price = nav_per_share
            if self.referee and hasattr(self.referee, 'books'):
                book = self.referee.books.get('ceres', {}).get(sym)
                if book:
                    best_bid = book.bids[0].limit_price if book.bids else None
                    best_ask = book.asks[0].limit_price if book.asks else None
                    if best_bid and best_ask:
                        spot_price = round((best_bid + best_ask) / 2.0, 2)
                    elif best_ask:
                        spot_price = float(best_ask)
                    elif best_bid:
                        spot_price = float(best_bid)

            market_cap = round(spot_price * total_shares, 2)

            summaries[sym] = {
                "symbol": sym,
                "ticker": conf["ticker"],
                "name": conf["name"],
                "issuer_agent_id": agent_id,
                "total_shares": total_shares,
                "nav_per_share": nav_per_share,
                "spot_price": spot_price,
                "market_cap": market_cap,
                "short_interest_shares": short_shares,
                "short_interest_pct": short_interest_pct,
                "active_loans_count": active_loans_count,
                "borrow_fee_rate": self.default_fee_rate,
                "borrow_fee_apr": f"{round(self.default_fee_rate * 100, 1)}% / rnd"
            }

        return summaries

    def get_active_loans(self, borrower_id: Optional[str] = None, lender_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns list of active equity loans."""
        cur = self.conn.cursor()
        query = "SELECT loan_id, borrower_id, lender_id, equity_symbol, shares, collateral_cr, fee_rate, start_round, status, created_at, closed_at FROM equity_loans WHERE 1=1"
        params = []
        if borrower_id:
            query += " AND borrower_id = ?"
            params.append(borrower_id)
        if lender_id:
            query += " AND lender_id = ?"
            params.append(lender_id)
        query += " ORDER BY start_round DESC, created_at DESC"

        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        return [
            {
                "loan_id": r[0],
                "borrower_id": r[1],
                "lender_id": r[2],
                "equity_symbol": r[3],
                "shares": r[4],
                "collateral_cr": r[5],
                "fee_rate": r[6],
                "start_round": r[7],
                "status": r[8],
                "created_at": r[9],
                "closed_at": r[10]
            }
            for r in rows
        ]

    def initiate_loan(
        self,
        borrower_id: str,
        equity_symbol: str,
        shares: int,
        collateral_cr: Optional[int] = None,
        lender_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes a bilateral stock loan for short-selling:
        1. Validates equity symbol and ensures borrower is not shorting their own fleet.
        2. Selects lender with uncommitted shares.
        3. Escrows required collateral CR from borrower to SYSTEM.
        4. Transfers borrowed equity shares from lender to borrower.
        5. Logs strict zero-sum double-entry ledger rows.
        """
        b_id = borrower_id.lower().strip()
        sym = equity_symbol.upper().strip()

        if sym not in EQUITY_SYMBOLS:
            return {"ok": False, "reason": "invalid_equity", "detail": f"Unknown equity instrument '{equity_symbol}'. Valid: {EQUITY_SYMBOLS}"}

        issuer_id = AGENT_BY_SYMBOL[sym]
        if b_id == issuer_id:
            return {"ok": False, "reason": "self_short_prohibited", "detail": f"Agent '{borrower_id}' cannot short their own fleet equity '{sym}'"}

        if shares <= 0:
            return {"ok": False, "reason": "invalid_shares", "detail": "Shares quantity must be strictly positive"}

        # Determine price & minimum collateral
        summary = self.get_equity_summary()
        eq_meta = summary.get(sym, {})
        spot_price = eq_meta.get("spot_price", 20.0)
        min_collateral = int(math.ceil(shares * spot_price * INITIAL_MARGIN_RATIO))

        required_collateral = collateral_cr if collateral_cr is not None else min_collateral
        if required_collateral < min_collateral:
            return {
                "ok": False,
                "reason": "insufficient_collateral_margin",
                "detail": f"Collateral {required_collateral} CR is below the 120% initial margin requirement ({min_collateral} CR)"
            }

        cur = self.conn.cursor()

        # Check borrower available liquid CR
        cur.execute("SELECT balance FROM accounts WHERE agent_id = ? AND instrument = 'CR'", (b_id,))
        b_row = cur.fetchone()
        borrower_cr = b_row[0] if b_row else 0
        if borrower_cr < required_collateral:
            return {
                "ok": False,
                "reason": "insufficient_funds",
                "detail": f"Borrower '{borrower_id}' has {borrower_cr} CR, but {required_collateral} CR is required for collateral"
            }

        # Select lender
        if lender_id:
            target_lender = lender_id.lower().strip()
            if target_lender == b_id:
                return {"ok": False, "reason": "invalid_lender", "detail": "Borrower cannot borrow from themselves"}
            cur.execute("SELECT balance FROM accounts WHERE agent_id = ? AND instrument = ?", (target_lender, sym))
            l_row = cur.fetchone()
            lender_shares = l_row[0] if l_row else 0
            if lender_shares < shares:
                return {
                    "ok": False,
                    "reason": "lender_insufficient_shares",
                    "detail": f"Lender '{target_lender}' only holds {lender_shares} shares of '{sym}' (needed {shares})"
                }
        else:
            # Pick agent holding the most shares of this equity (excluding borrower and SYSTEM)
            cur.execute("""
                SELECT agent_id, balance FROM accounts
                WHERE instrument = ? AND agent_id NOT IN (?, 'SYSTEM')
                ORDER BY balance DESC LIMIT 1
            """, (sym, b_id))
            top_lender = cur.fetchone()
            if not top_lender or top_lender[1] < shares:
                avail = top_lender[1] if top_lender else 0
                return {
                    "ok": False,
                    "reason": "no_available_lend_inventory",
                    "detail": f"No market participants hold enough free shares of '{sym}' (available: {avail}, needed: {shares})"
                }
            target_lender = top_lender[0]

        # Generate durable loan ID
        loan_id = f"loan-{b_id}-{sym.lower()}-{int(time.time() * 1000) % 1000000}"
        curr_round = getattr(self.referee, 'current_round', 1)
        next_seq = self.referee._get_next_seq() if self.referee else 0

        # Execute double-entry ledger transfers
        with self.conn:
            # 1. Lock Collateral CR from borrower to SYSTEM escrow
            self.conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'CR'",
                (required_collateral, b_id)
            )
            self.conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE agent_id = 'SYSTEM' AND instrument = 'CR'",
                (required_collateral,)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                (f"col-{loan_id}", next_seq, b_id, -required_collateral)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'CR', ?)",
                (f"col-{loan_id}", next_seq, required_collateral)
            )

            # 2. Transfer shares of equity from lender to borrower
            self.conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?",
                (shares, target_lender, sym)
            )
            # Ensure borrower has an accounts row for this equity
            self.conn.execute(
                "INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)",
                (b_id, sym)
            )
            self.conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                (shares, b_id, sym)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                (f"share-{loan_id}", next_seq, target_lender, sym, -shares)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                (f"share-{loan_id}", next_seq, b_id, sym, shares)
            )

            # 3. Record active loan contract
            self.conn.execute("""
                INSERT INTO equity_loans (loan_id, borrower_id, lender_id, equity_symbol, shares, collateral_cr, fee_rate, start_round, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """, (loan_id, b_id, target_lender, sym, shares, required_collateral, self.default_fee_rate, curr_round))

            # 4. Stream tick event
            if self.referee:
                loan_payload = {
                    "loan_id": loan_id,
                    "borrower_id": b_id,
                    "lender_id": target_lender,
                    "equity_symbol": sym,
                    "shares": shares,
                    "collateral_cr": required_collateral,
                    "fee_rate": self.default_fee_rate,
                    "round": curr_round
                }
                self.conn.execute(
                    "INSERT INTO book_events (seq, kind, payload) VALUES (?, 'borrow', ?)",
                    (next_seq, json.dumps(loan_payload))
                )

        return {
            "ok": True,
            "loan_id": loan_id,
            "borrower_id": b_id,
            "lender_id": target_lender,
            "equity_symbol": sym,
            "shares": shares,
            "collateral_cr": required_collateral,
            "fee_rate": self.default_fee_rate,
            "round": curr_round,
            "message": f"Successfully borrowed {shares} shares of {sym} from '{target_lender}' with {required_collateral} CR collateral escrowed."
        }

    def return_loan(self, borrower_id: str, loan_id: str) -> Dict[str, Any]:
        """
        Closes an active equity loan:
        1. Validates borrower holds sufficient equity shares to return to lender.
        2. Transfers shares back from borrower to lender.
        3. Returns escrowed collateral CR from SYSTEM back to borrower.
        4. Marks loan status as 'closed'.
        """
        b_id = borrower_id.lower().strip()
        cur = self.conn.cursor()
        cur.execute("""
            SELECT loan_id, borrower_id, lender_id, equity_symbol, shares, collateral_cr, status
            FROM equity_loans WHERE loan_id = ?
        """, (loan_id,))
        loan = cur.fetchone()

        if not loan:
            return {"ok": False, "reason": "loan_not_found", "detail": f"No loan found with ID '{loan_id}'"}

        _, l_borrower, l_lender, sym, shares, collateral_cr, status = loan

        if l_borrower != b_id:
            return {"ok": False, "reason": "unauthorized", "detail": f"Loan belongs to '{l_borrower}', not '{borrower_id}'"}

        if status != 'active':
            return {"ok": False, "reason": "loan_not_active", "detail": f"Loan '{loan_id}' is already {status}"}

        # Check borrower has sufficient shares to return
        cur.execute("SELECT balance FROM accounts WHERE agent_id = ? AND instrument = ?", (b_id, sym))
        b_share_row = cur.fetchone()
        borrower_shares = b_share_row[0] if b_share_row else 0
        if borrower_shares < shares:
            return {
                "ok": False,
                "reason": "insufficient_shares_to_cover",
                "detail": f"Borrower holds {borrower_shares} shares of '{sym}', but {shares} shares are required to close loan"
            }

        next_seq = self.referee._get_next_seq() if self.referee else 0

        with self.conn:
            # 1. Return shares from borrower to lender
            self.conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?",
                (shares, b_id, sym)
            )
            self.conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                (shares, l_lender, sym)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                (f"ret-share-{loan_id}", next_seq, b_id, sym, -shares)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                (f"ret-share-{loan_id}", next_seq, l_lender, sym, shares)
            )

            # 2. Return remaining collateral from SYSTEM to borrower
            self.conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE agent_id = 'SYSTEM' AND instrument = 'CR'",
                (collateral_cr,)
            )
            self.conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'",
                (collateral_cr, b_id)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'CR', ?)",
                (f"ret-col-{loan_id}", next_seq, -collateral_cr)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                (f"ret-col-{loan_id}", next_seq, b_id, collateral_cr)
            )

            # 3. Mark loan closed
            self.conn.execute(
                "UPDATE equity_loans SET status = 'closed', closed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE loan_id = ?",
                (loan_id,)
            )

            # 4. Stream tick event
            if self.referee:
                close_payload = {
                    "loan_id": loan_id,
                    "borrower_id": b_id,
                    "lender_id": l_lender,
                    "equity_symbol": sym,
                    "shares_returned": shares,
                    "collateral_released": collateral_cr
                }
                self.conn.execute(
                    "INSERT INTO book_events (seq, kind, payload) VALUES (?, 'loan_closed', ?)",
                    (next_seq, json.dumps(close_payload))
                )

        return {
            "ok": True,
            "loan_id": loan_id,
            "shares_returned": shares,
            "collateral_released": collateral_cr,
            "message": f"Successfully returned {shares} shares of {sym} to '{l_lender}'. {collateral_cr} CR collateral unlocked."
        }

    def step_borrow_fees(self, current_round: int) -> List[Dict[str, Any]]:
        """
        Runs round-by-round borrow fee collection and checks maintenance margin:
        1. Calculates borrow fee = max(1, int(collateral * fee_rate)).
        2. Deducts fee from borrower and transfers directly to lender.
        3. If borrower liquid cash exhausted, deducts fee from escrowed collateral.
        4. If collateral drops below 105% maintenance margin, triggers forced margin liquidation!
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT loan_id, borrower_id, lender_id, equity_symbol, shares, collateral_cr, fee_rate, start_round
            FROM equity_loans
            WHERE status = 'active'
        """)
        active_loans = cur.fetchall()
        fee_reports = []

        summary = self.get_equity_summary()

        for loan in active_loans:
            loan_id, b_id, l_id, sym, shares, collateral_cr, fee_rate, start_round = loan
            if current_round <= start_round:
                continue

            fee = max(1, int(collateral_cr * fee_rate))
            next_seq = self.referee._get_next_seq() if self.referee else 0

            # Check borrower's liquid CR
            cur.execute("SELECT balance FROM accounts WHERE agent_id = ? AND instrument = 'CR'", (b_id,))
            b_row = cur.fetchone()
            b_cr = b_row[0] if b_row else 0

            with self.conn:
                if b_cr >= fee:
                    # Direct fee payment from borrower to lender
                    self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'CR'", (fee, b_id))
                    self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (fee, l_id))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)", (f"fee-{loan_id}-r{current_round}", next_seq, b_id, -fee))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)", (f"fee-{loan_id}-r{current_round}", next_seq, l_id, fee))
                    fee_source = "liquid_balance"
                else:
                    # Deduct from escrowed collateral
                    fee_to_take = min(fee, collateral_cr)
                    if fee_to_take > 0:
                        self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = 'SYSTEM' AND instrument = 'CR'", (fee_to_take,))
                        self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (fee_to_take, l_id))
                        self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'CR', ?)", (f"feecol-{loan_id}-r{current_round}", next_seq, -fee_to_take))
                        self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)", (f"feecol-{loan_id}-r{current_round}", next_seq, l_id, fee_to_take))
                        collateral_cr -= fee_to_take
                        self.conn.execute("UPDATE equity_loans SET collateral_cr = ? WHERE loan_id = ?", (collateral_cr, loan_id))
                    fee_source = "escrowed_collateral"

            # Check maintenance margin
            spot = summary.get(sym, {}).get("spot_price", 20.0)
            notional = shares * spot
            margin_ratio = collateral_cr / notional if notional > 0 else 999.0

            report = {
                "loan_id": loan_id,
                "borrower_id": b_id,
                "lender_id": l_id,
                "equity_symbol": sym,
                "fee_paid": fee,
                "fee_source": fee_source,
                "remaining_collateral": collateral_cr,
                "margin_ratio": round(margin_ratio, 3),
                "round": current_round,
                "liquidated": False
            }

            if margin_ratio < MAINTENANCE_MARGIN_RATIO:
                # FORCED MARGIN LIQUIDATION!
                liq_res = self.liquidate_loan(loan_id, reason="maintenance_margin_breach")
                report["liquidated"] = True
                report["liquidation_detail"] = liq_res

            fee_reports.append(report)

        return fee_reports

    def liquidate_loan(self, loan_id: str, reason: str = "maintenance_margin_breach") -> Dict[str, Any]:
        """
        Executes forced margin liquidation on a delinquent short loan:
        1. Forfeits all remaining escrowed collateral CR to lender.
        2. Marks loan as 'liquidated'.
        3. Releases lender obligation (lender keeps collateral as final payout).
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT loan_id, borrower_id, lender_id, equity_symbol, shares, collateral_cr, status
            FROM equity_loans WHERE loan_id = ?
        """, (loan_id,))
        loan = cur.fetchone()

        if not loan or loan[6] != 'active':
            return {"ok": False, "reason": "not_eligible_for_liquidation"}

        _, b_id, l_id, sym, shares, collateral_cr, _ = loan
        next_seq = self.referee._get_next_seq() if self.referee else 0

        with self.conn:
            # Transfer entire remaining collateral from SYSTEM to lender
            if collateral_cr > 0:
                self.conn.execute(
                    "UPDATE accounts SET balance = balance - ? WHERE agent_id = 'SYSTEM' AND instrument = 'CR'",
                    (collateral_cr,)
                )
                self.conn.execute(
                    "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'",
                    (collateral_cr, l_id)
                )
                self.conn.execute(
                    "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'CR', ?)",
                    (f"liq-{loan_id}", next_seq, -collateral_cr)
                )
                self.conn.execute(
                    "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                    (f"liq-{loan_id}", next_seq, l_id, collateral_cr)
                )

            self.conn.execute("""
                UPDATE equity_loans
                SET status = 'liquidated', collateral_cr = 0, closed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE loan_id = ?
            """, (loan_id,))

            if self.referee:
                liq_payload = {
                    "loan_id": loan_id,
                    "borrower_id": b_id,
                    "lender_id": l_id,
                    "equity_symbol": sym,
                    "shares": shares,
                    "collateral_forfeited": collateral_cr,
                    "reason": reason
                }
                self.conn.execute(
                    "INSERT INTO book_events (seq, kind, payload) VALUES (?, 'liquidation', ?)",
                    (next_seq, json.dumps(liq_payload))
                )

        return {
            "ok": True,
            "loan_id": loan_id,
            "borrower_id": b_id,
            "lender_id": l_id,
            "equity_symbol": sym,
            "collateral_forfeited": collateral_cr,
            "reason": reason,
            "message": f"Loan {loan_id} liquidated. {collateral_cr} CR collateral forfeited to '{l_id}'."
        }
