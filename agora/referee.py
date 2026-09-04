"""
agora.referee - Central referee for order validation, solvency auditing,
book matching, and atomic double-entry ledger settlement.
"""

import sqlite3
import json
import time
from typing import Optional, Dict, Any, Tuple, List
from pathlib import Path

from agora.order_book import OrderBook, Order, Trade


class AgoraReferee:
    def __init__(self, db_path: str = ':memory:'):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
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
        return row[0] if row else 'CASH'

    def submit_envelope(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
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

        # 2. Idempotency Check
        cur = self.conn.cursor()
        cur.execute("SELECT agent_id, side, qty, limit_price FROM orders WHERE order_id = ?", (order_id,))
        existing = cur.fetchone()
        if existing:
            if existing['agent_id'] == agent_id and existing['side'] == side and existing['qty'] == qty and existing['limit_price'] == limit_price:
                return {
                    'v': 1,
                    'kind': 'status',
                    'reply': 'none',
                    'status': 'noop_duplicate',
                    'order_id': order_id,
                    'note': 'Order already processed identically (idempotent noop)'
                }
            else:
                return self._reject_envelope(order_id, agent_id, 'duplicate_order', 'Order ID already exists with conflicting parameters')

        # 3. Solvency Audit (No negative balances for non-SYSTEM agents)
        currency = self.get_currency_instrument(agent_id)
        if side == 'bid':
            max_cost = qty * limit_price
            buyer_balance = self.get_balance(agent_id, currency)
            if buyer_balance < max_cost:
                return self._reject_envelope(
                    order_id, agent_id, 'insufficient_balance',
                    f"Account '{agent_id}' {currency} balance {buyer_balance} insufficient for bid requirement {max_cost}"
                )
        elif side == 'ask':
            seller_balance = self.get_balance(agent_id, 'BANANA')
            if seller_balance < qty:
                return self._reject_envelope(
                    order_id, agent_id, 'insufficient_balance',
                    f"Account '{agent_id}' BANANA balance {seller_balance} insufficient for ask requirement {qty}"
                )

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
                trade_currency = self.get_currency_instrument(trade.buyer_id)
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

                # Update accounts
                self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?", (cost, trade.buyer_id, trade_currency))
                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (cost, trade.seller_id, trade_currency))
                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'BANANA'", (trade.qty, trade.buyer_id))
                self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'BANANA'", (trade.qty, trade.seller_id))

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