"""
agora.referee - Central referee for order validation, solvency auditing,
book matching, and atomic double-entry ledger settlement.
"""

import sqlite3
import json
import time
import copy
import threading
from typing import Optional, Dict, Any, Tuple, List
from pathlib import Path

from agora.order_book import OrderBook, Order, Trade


class AgoraReferee:
    def __init__(self, db_path: str = ':memory:'):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.book = OrderBook(instrument='BANANA')
        self.last_price: Optional[int] = None
        self.last_qty: Optional[int] = None
        self._init_db()

    def _init_db(self):
        """Load schema and genesis seed if database is uninitialized."""
        with self.conn:
            tables = [r[0] for r in self.conn.execute("""
                SELECT name FROM sqlite_master WHERE type='table'
            """).fetchall()]
            if 'accounts' not in tables:
                schema_path = Path(__file__).resolve().parent.parent / 'db' / 'schema.sql'
                seed_path = Path(__file__).resolve().parent.parent / 'db' / 'seed.sql'
                if schema_path.exists():
                    self.conn.executescript(schema_path.read_text())
                if seed_path.exists():
                    self.conn.executescript(seed_path.read_text())

    @property
    def current_seq(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM book_events")
        row = cur.fetchone()
        return row[0] if row else 0

    def get_balance(self, agent_id: str, instrument: str) -> int:
        cur = self.conn.cursor()
        # Support both CREDITS and CASH as currency instrument
        cur.execute(
            "SELECT balance FROM accounts WHERE agent_id = ? AND instrument = ?",
            (agent_id, instrument)
        )
        row = cur.fetchone()
        if not row and instrument in ('CREDITS', 'CASH'):
            alt = 'CASH' if instrument == 'CREDITS' else 'CREDITS'
            cur.execute(
                "SELECT balance FROM accounts WHERE agent_id = ? AND instrument = ?",
                (agent_id, alt)
            )
            row = cur.fetchone()
        return row[0] if row else 0

    def get_currency_instrument(self, agent_id: str = 'amos') -> str:
        cur = self.conn.cursor()
        cur.execute("SELECT instrument FROM accounts WHERE agent_id = ? AND instrument IN ('CREDITS', 'CASH') LIMIT 1", (agent_id,))
        row = cur.fetchone()
        return row[0] if row else 'CREDITS'

    def get_book_snapshot(self) -> Dict[str, Any]:
        return self.book.to_dict()

    def get_ticks(self, since_seq: int = 0) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT seq, kind, payload, created_at FROM book_events WHERE seq > ? ORDER BY seq ASC",
            (since_seq,)
        )
        ticks = []
        for r in cur.fetchall():
            payload = r['payload']
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    pass
            ticks.append({
                'seq': r['seq'],
                'kind': r['kind'],
                'payload': payload,
                'created_at': r['created_at']
            })
        return ticks

    def get_accounts(self, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        if agent_id:
            cur.execute(
                "SELECT agent_id, instrument, balance FROM accounts WHERE agent_id = ? ORDER BY instrument ASC",
                (agent_id,)
            )
        else:
            cur.execute(
                "SELECT agent_id, instrument, balance FROM accounts ORDER BY agent_id ASC, instrument ASC"
            )
        return [
            {
                'agent_id': r['agent_id'],
                'instrument': r['instrument'],
                'balance': r['balance']
            }
            for r in cur.fetchall()
        ]

    def submit_envelope(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            return self._submit_envelope_locked(envelope)

    def _submit_envelope_locked(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process an inbound handoff envelope.
        Routes kind='order' through validation, book matching, and ledger settlement.
        """
        kind = envelope.get('kind')
        if kind != 'order':
            return self._reject_envelope(
                order_id=envelope.get('payload', {}).get('order_id', 'unknown'),
                agent_id=envelope.get('payload', {}).get('agent_id', 'unknown'),
                reason='invalid_format',
                detail=f"Referee only processes kind='order', received '{kind}'"
            )

        payload = envelope.get('payload', {})
        order_id = payload.get('order_id')
        agent_id = payload.get('agent_id')
        instrument = payload.get('instrument')
        side = payload.get('side')
        qty = payload.get('qty')
        limit_price = payload.get('limit_price')
        seq_seen = payload.get('seq_seen', 0)

        # 1. Format validation
        if not all([order_id, agent_id, instrument, side, qty is not None, limit_price is not None]):
            return self._reject_envelope(order_id or 'unknown', agent_id or 'unknown', 'invalid_format', 'Missing required order fields')

        if instrument != 'BANANA':
            return self._reject_envelope(order_id, agent_id, 'invalid_format', f"Unsupported instrument: {instrument}")

        if side not in ('bid', 'ask'):
            return self._reject_envelope(order_id, agent_id, 'invalid_format', f"Invalid side: {side}")

        if not (isinstance(qty, int) and qty > 0):
            return self._reject_envelope(order_id, agent_id, 'invalid_format', 'Qty must be positive integer')

        if not (isinstance(limit_price, int) and limit_price > 0):
            return self._reject_envelope(order_id, agent_id, 'invalid_format', 'Limit price must be positive integer')

        # 2. Idempotency Check (composite key: agent_id, order_id)
        cur = self.conn.cursor()
        cur.execute("SELECT agent_id, side, qty, limit_price FROM orders WHERE agent_id = ? AND order_id = ?", (agent_id, order_id))
        existing = cur.fetchone()
        if existing:
            if existing['side'] == side and existing['qty'] == qty and existing['limit_price'] == limit_price:
                return {
                    'v': 1,
                    'kind': 'status',
                    'reply': 'none',
                    'status': 'noop_duplicate',
                    'order_id': order_id,
                    'note': 'Order already processed identically (idempotent noop)'
                }
            else:
                return self._reject_envelope(order_id, agent_id, 'duplicate_order', f"Order ID '{order_id}' already exists for agent '{agent_id}' with conflicting parameters")

        # 3. Solvency & Committed Exposure Audit (No negative balances for non-SYSTEM agents)
        currency = self.get_currency_instrument(agent_id)
        if side == 'bid':
            max_cost = qty * limit_price
            committed_funds = sum(
                o.remaining_qty * o.limit_price
                for o in self.book.bids
                if o.agent_id == agent_id
            )
            buyer_balance = self.get_balance(agent_id, currency)
            available_funds = buyer_balance - committed_funds
            if available_funds < max_cost:
                return self._reject_envelope(
                    order_id, agent_id, 'insufficient_balance',
                    f"Account '{agent_id}' available {currency} balance {available_funds} "
                    f"(balance {buyer_balance} - committed {committed_funds}) insufficient for bid requirement {max_cost}"
                )
        elif side == 'ask':
            committed_banana = sum(
                o.remaining_qty
                for o in self.book.asks
                if o.agent_id == agent_id
            )
            seller_balance = self.get_balance(agent_id, 'BANANA')
            available_banana = seller_balance - committed_banana
            if available_banana < qty:
                return self._reject_envelope(
                    order_id, agent_id, 'insufficient_balance',
                    f"Account '{agent_id}' available BANANA balance {available_banana} "
                    f"(balance {seller_balance} - committed {committed_banana}) insufficient for ask requirement {qty}"
                )

        # 3b. Currency Compatibility Audit for Crossing Orders (Pre-matching validation)
        # Mirrors OrderBook.add_order walk: only check resting orders that would actually be consumed.
        needed_qty = qty
        if side == 'bid':
            for ask in self.book.asks:
                if needed_qty <= 0:
                    break
                if ask.limit_price > limit_price:
                    break
                ask_currency = self.get_currency_instrument(ask.agent_id)
                if ask_currency != currency:
                    return self._reject_envelope(
                        order_id, agent_id, 'currency_mismatch',
                        f"Order crosses resting ask from '{ask.agent_id}' with incompatible currency '{ask_currency}' vs '{currency}'"
                    )
                needed_qty -= ask.remaining_qty
        elif side == 'ask':
            for bid in self.book.bids:
                if needed_qty <= 0:
                    break
                if bid.limit_price < limit_price:
                    break
                bid_currency = self.get_currency_instrument(bid.agent_id)
                if bid_currency != currency:
                    return self._reject_envelope(
                        order_id, agent_id, 'currency_mismatch',
                        f"Order crosses resting bid from '{bid.agent_id}' with incompatible currency '{bid_currency}' vs '{currency}'"
                    )
                needed_qty -= bid.remaining_qty

        # 4. Matching & Atomic Ledger Settlement
        order = Order(
            order_id=order_id,
            agent_id=agent_id,
            instrument=instrument,
            side=side,
            qty=qty,
            limit_price=limit_price,
            seq_seen=seq_seen
        )

        book_snapshot = copy.deepcopy(self.book)
        try:
            with self.conn:
                next_seq = self.current_seq + 1

                # Match against book
                trades, resting = self.book.add_order(order, current_seq=next_seq)

                # Record submission in orders table
                status = 'filled' if order.is_filled else 'open'
                self.conn.execute("""
                    INSERT INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, resolved_seq)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, next_seq if order.is_filled else None))

                # Record book event
                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload)
                    VALUES (?, 'order', ?)
                """, (next_seq, json.dumps(payload)))

                # Settle each executed trade atomically in ledger_entries and accounts
                for trade in trades:
                    self.last_price = trade.price
                    self.last_qty = trade.qty
                    buyer_currency = self.get_currency_instrument(trade.buyer_id)
                    seller_currency = self.get_currency_instrument(trade.seller_id)
                    if buyer_currency != seller_currency:
                        raise RuntimeError(
                            f"Currency mismatch during settlement: buyer '{trade.buyer_id}' uses {buyer_currency} "
                            f"but seller '{trade.seller_id}' uses {seller_currency}"
                        )
                    trade_currency = buyer_currency
                    cost = trade.price * trade.qty
                    txn_id = f'trade-{trade.trade_id}'

                    # Double-entry rows: sum(delta) == 0 per instrument
                    # Currency deltas
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta)
                        VALUES (?, ?, ?, ?, ?)
                    """, (txn_id, next_seq, trade.buyer_id, trade_currency, -cost))
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta)
                        VALUES (?, ?, ?, ?, ?)
                    """, (txn_id, next_seq, trade.seller_id, trade_currency, cost))

                    # Commodity (BANANA) deltas
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta)
                        VALUES (?, ?, ?, ?, ?)
                    """, (txn_id, next_seq, trade.buyer_id, 'BANANA', trade.qty))
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta)
                        VALUES (?, ?, ?, ?, ?)
                    """, (txn_id, next_seq, trade.seller_id, 'BANANA', -trade.qty))

                    # Update accounts with strict rowcount validation (must match exactly 1 row per update)
                    cur = self.conn.execute(
                        "UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?",
                        (cost, trade.buyer_id, trade_currency)
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Failed to debit {trade.buyer_id} {trade_currency}: rowcount {cur.rowcount} != 1")

                    cur = self.conn.execute(
                        "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                        (cost, trade.seller_id, trade_currency)
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Failed to credit {trade.seller_id} {trade_currency}: rowcount {cur.rowcount} != 1")

                    cur = self.conn.execute(
                        "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'BANANA'",
                        (trade.qty, trade.buyer_id)
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Failed to credit {trade.buyer_id} BANANA: rowcount {cur.rowcount} != 1")

                    cur = self.conn.execute(
                        "UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'BANANA'",
                        (trade.qty, trade.seller_id)
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Failed to debit {trade.seller_id} BANANA: rowcount {cur.rowcount} != 1")

                    # Record trade event in book_events
                    trade_seq = self.current_seq + 1
                    self.conn.execute("""
                        INSERT INTO book_events (seq, kind, payload)
                        VALUES (?, 'trade', ?)
                    """, (trade_seq, json.dumps({
                        'trade_id': trade.trade_id,
                        'buyer_id': trade.buyer_id,
                        'seller_id': trade.seller_id,
                        'price': trade.price,
                        'qty': trade.qty,
                        'cost': cost
                    })))
        except Exception:
            self.book = book_snapshot
            raise

        # 5. Emit Market Discovery Broadcast (kind: market_tick)
        return {
            'v': 1,
            'kind': 'market_tick',
            'reply': 'optional',
            'floor': 'open',
            'scope': 'channel',
            'subject': 'agent-collaborative-project',
            'payload': {
                'seq': self.current_seq,
                'best_bid': self.book.best_bid(),
                'best_ask': self.book.best_ask(),
                'last_price': self.last_price,
                'last_qty': self.last_qty,
                'status': 'open',
                'trades_count': len(trades)
            }
        }

    def _reject_envelope(self, order_id: str, agent_id: str, reason: str, detail: str) -> Dict[str, Any]:
        return {
            'v': 1,
            'kind': 'reject',
            'reply': 'optional',
            'floor': 'open',
            'scope': 'channel',
            'subject': 'agent-collaborative-project',
            'payload': {
                'order_id': order_id,
                'agent_id': agent_id,
                'seq': self.current_seq,
                'reason': reason,
                'detail': detail
            }
        }

    def verify_ledger_invariants(self) -> Tuple[bool, List[str]]:
        """
        Verify standing invariants:
        1. Conservation: sum(delta) == 0 for every txn_id.
        2. Non-negativity: balance >= 0 for all agents except SYSTEM.
        3. Account Reconciliation: accounts.balance == sum(ledger_entries.delta) per (agent_id, instrument).
        """
        errors = []
        cur = self.conn.cursor()

        # Invariant 1: Conservation
        cur.execute("""
            SELECT txn_id, SUM(delta) as net
            FROM ledger_entries
            GROUP BY txn_id
            HAVING net != 0
        """)
        leaks = cur.fetchall()
        for row in leaks:
            errors.append(f"Conservation breach on {row['txn_id']}: net delta = {row['net']}")

        # Invariant 2: Non-negative balances
        cur.execute("""
            SELECT agent_id, instrument, balance
            FROM accounts
            WHERE balance < 0 AND agent_id != 'SYSTEM'
        """)
        deficits = cur.fetchall()
        for row in deficits:
            errors.append(f"Insolvency breach: {row['agent_id']} {row['instrument']} balance = {row['balance']}")

        # Invariant 3: Account-Ledger Reconciliation
        cur.execute("""
            SELECT agent_id, instrument, SUM(delta) as ledger_sum, SUM(balance) as account_balance
            FROM (
                SELECT agent_id, instrument, delta, 0 as balance FROM ledger_entries
                UNION ALL
                SELECT agent_id, instrument, 0 as delta, balance FROM accounts
            )
            GROUP BY agent_id, instrument
            HAVING SUM(delta) != SUM(balance)
        """)
        mismatches = cur.fetchall()
        for row in mismatches:
            errors.append(
                f"Reconciliation breach: {row['agent_id']} {row['instrument']} "
                f"account balance = {row['account_balance']} vs ledger delta sum = {row['ledger_sum']}"
            )

        return len(errors) == 0, errors

    def get_leaderboard(self) -> List[Dict[str, Any]]:
        """
        Calculate Net Worth = Balance(Cash/Credits) + Qty(BANANA) * Mark Price.
        """
        mark = self.last_price if self.last_price is not None else 10  # default mark if no trades
        cur = self.conn.cursor()
        cur.execute("""
            SELECT agent_id,
                   SUM(CASE WHEN instrument IN ('CREDITS', 'CASH') THEN balance ELSE 0 END) as liquid,
                   SUM(CASE WHEN instrument = 'BANANA' THEN balance ELSE 0 END) as bananas
            FROM accounts
            WHERE agent_id != 'SYSTEM'
            GROUP BY agent_id
        """)
        rows = cur.fetchall()
        board = []
        for r in rows:
            net_worth = r['liquid'] + (r['bananas'] * mark)
            board.append({
                'agent_id': r['agent_id'],
                'net_worth': net_worth,
                'liquid': r['liquid'],
                'bananas': r['bananas'],
                'mark_price': mark
            })
        board.sort(key=lambda x: x['net_worth'], reverse=True)
        return board