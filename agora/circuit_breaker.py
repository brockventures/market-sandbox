"""
agora.circuit_breaker - Market Variance: Dynamic LULD Circuit Breakers & Station Halts.

Implements rolling +-10% VWAP price bands, discrete 2-round station trading halts,
and call auction reopen matching for Issue #24.
"""

import json
import math
import time
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple

from agora.order_book import Order, Trade


DEFAULT_BAND_PCT = 0.10         # Rolling +-10% price bands
DEFAULT_HALT_DURATION = 2       # 2-round discrete trading halt


@dataclass
class CircuitBreakerHalt:
    halt_id: str
    station_id: str
    instrument: str
    halt_round: int
    reopen_round: int
    trigger_price: float
    lower_limit: float
    upper_limit: float
    vwap: float
    reason: str
    status: str  # 'halted', 'reopened', 'cancelled'
    reopen_price: Optional[float] = None
    reopen_volume: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def find_clearing_price(bids: List[Order], asks: List[Order], ref_price: float) -> Tuple[Optional[int], int]:
    """
    Computes the single call auction clearing price that maximizes executable volume.
    Ties broken by proximity to ref_price (previous VWAP).
    """
    if not bids or not asks:
        return None, 0

    candidate_prices = set()
    for o in bids:
        candidate_prices.add(o.limit_price)
    for o in asks:
        candidate_prices.add(o.limit_price)

    sorted_prices = sorted(candidate_prices)
    max_volume = 0
    best_candidates = []

    for p in sorted_prices:
        buy_vol = sum(o.remaining_qty for o in bids if o.limit_price >= p)
        sell_vol = sum(o.remaining_qty for o in asks if o.limit_price <= p)
        match_vol = min(buy_vol, sell_vol)

        if match_vol > max_volume:
            max_volume = match_vol
            best_candidates = [p]
        elif match_vol == max_volume and match_vol > 0:
            best_candidates.append(p)

    if max_volume == 0 or not best_candidates:
        return None, 0

    best_price = min(best_candidates, key=lambda p: (abs(p - ref_price), p))
    return best_price, max_volume


class CircuitBreakerEngine:
    """
    Manages rolling VWAPs, LULD bands, discrete 2-round halts,
    and auction reopens across all Sol station order books.
    """

    def __init__(self, conn, referee=None, band_pct: float = DEFAULT_BAND_PCT, halt_duration: int = DEFAULT_HALT_DURATION):
        self.conn = conn
        self.referee = referee
        self.band_pct = band_pct
        self.halt_duration = halt_duration
        # In-memory rolling trade cache: (station_id, instrument) -> list of (price, qty, round, time)
        self.recent_trades: Dict[Tuple[str, str], List[Tuple[float, int, int, float]]] = {}
        self._ensure_tables()

    def _ensure_tables(self):
        """Ensures circuit breaker halt persistence table exists."""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breaker_halts (
                    halt_id         TEXT PRIMARY KEY,
                    station_id      TEXT NOT NULL,
                    instrument      TEXT NOT NULL,
                    halt_round      INTEGER NOT NULL,
                    reopen_round    INTEGER NOT NULL,
                    trigger_price   REAL NOT NULL,
                    lower_limit     REAL NOT NULL,
                    upper_limit     REAL NOT NULL,
                    vwap            REAL NOT NULL,
                    reason          TEXT NOT NULL,
                    status          TEXT NOT NULL CHECK (status IN ('halted', 'reopened', 'cancelled')),
                    reopen_price    REAL,
                    reopen_volume   INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)

    def record_trade(self, station_id: str, instrument: str, price: float, qty: int, round_num: int):
        """Records an executed trade in the rolling VWAP window."""
        key = (station_id.lower(), instrument.upper())
        if key not in self.recent_trades:
            self.recent_trades[key] = []
        self.recent_trades[key].append((float(price), int(qty), int(round_num), time.time()))
        # Retain last 50 trades in memory
        if len(self.recent_trades[key]) > 50:
            self.recent_trades[key] = self.recent_trades[key][-50:]

    def has_prior_trades(self, station_id: str, instrument: str) -> bool:
        """Returns True if at least one trade has been executed and recorded for this station/instrument."""
        key = (station_id.lower(), instrument.upper())
        return key in self.recent_trades and len(self.recent_trades[key]) > 0

    def get_vwap(self, station_id: str, instrument: str) -> float:
        """
        Computes the volume-weighted average price (VWAP) for the station book.
        Falls back to StationPriceEngine spot price or baseline fundamental.
        """
        key = (station_id.lower(), instrument.upper())
        trades = self.recent_trades.get(key, [])
        if trades:
            total_vol = sum(t[1] for t in trades)
            if total_vol > 0:
                weighted_sum = sum(t[0] * t[1] for t in trades)
                return round(weighted_sum / total_vol, 2)

        # Fallback to station spot price or referee last price
        if self.referee:
            if hasattr(self.referee, "spatial"):
                spot = self.referee.spatial.get_station_price(station_id, instrument)
                if spot > 0:
                    return float(spot)
            if getattr(self.referee, "last_price", None):
                return float(self.referee.last_price)

        return 10.0

    def get_bands(self, station_id: str, instrument: str) -> Dict[str, Any]:
        """
        Returns the current +-10% LULD bounds and halt status for the station book.
        """
        st = station_id.lower()
        inst = instrument.upper()
        vwap = self.get_vwap(st, inst)

        lower_limit = max(1.0, round(vwap * (1.0 - self.band_pct), 2))
        upper_limit = max(lower_limit + 1.0, round(vwap * (1.0 + self.band_pct), 2))

        halt_info = self.get_active_halt(st, inst)
        status = 'halted' if halt_info else 'open'

        return {
            'station_id': st,
            'instrument': inst,
            'vwap': vwap,
            'band_pct': self.band_pct,
            'lower_limit': lower_limit,
            'upper_limit': upper_limit,
            'status': status,
            'halt_info': halt_info
        }

    def get_active_halt(self, station_id: str, instrument: str) -> Optional[Dict[str, Any]]:
        """Checks if a station book is currently halted."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT halt_id, station_id, instrument, halt_round, reopen_round,
                   trigger_price, lower_limit, upper_limit, vwap, reason, status, created_at
            FROM circuit_breaker_halts
            WHERE station_id = ? AND instrument = ? AND status = 'halted'
            ORDER BY created_at DESC LIMIT 1
        """, (station_id.lower(), instrument.upper()))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)

    def is_halted(self, station_id: str, instrument: str) -> bool:
        return self.get_active_halt(station_id, instrument) is not None

    def trigger_halt(
        self,
        station_id: str,
        instrument: str,
        trigger_price: float,
        reason: str,
        current_round: int
    ) -> Dict[str, Any]:
        """
        Enacts a discrete 2-round station trading halt following an out-of-band breach.
        """
        st = station_id.lower()
        inst = instrument.upper()
        bands = self.get_bands(st, inst)

        halt_id = f"halt-{st}-{inst}-{int(time.time() * 1000)}"
        reopen_round = current_round + self.halt_duration

        with self.conn:
            self.conn.execute("""
                INSERT INTO circuit_breaker_halts (
                    halt_id, station_id, instrument, halt_round, reopen_round,
                    trigger_price, lower_limit, upper_limit, vwap, reason, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'halted')
            """, (
                halt_id, st, inst, current_round, reopen_round,
                trigger_price, bands['lower_limit'], bands['upper_limit'],
                bands['vwap'], reason
            ))

            if self.referee:
                next_seq = self.referee._get_next_seq()
                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload) VALUES (?, 'circuit_breaker_halt', ?)
                """, (next_seq, json.dumps({
                    'halt_id': halt_id,
                    'station_id': st,
                    'instrument': inst,
                    'halt_round': current_round,
                    'reopen_round': reopen_round,
                    'trigger_price': trigger_price,
                    'lower_limit': bands['lower_limit'],
                    'upper_limit': bands['upper_limit'],
                    'vwap': bands['vwap'],
                    'reason': reason
                })))

        return {
            'ok': True,
            'halt_id': halt_id,
            'station_id': st,
            'instrument': inst,
            'halt_round': current_round,
            'reopen_round': reopen_round,
            'lower_limit': bands['lower_limit'],
            'upper_limit': bands['upper_limit'],
            'vwap': bands['vwap'],
            'status': 'halted'
        }

    def execute_auction_reopen(self, station_id: str, instrument: str, round_num: int) -> Dict[str, Any]:
        """
        Executes call auction reopen for a halted station book.
        Finds the single clearing price maximizing volume, settles trades atomically,
        updates VWAP and LULD bands, and reopens the floor.
        """
        st = station_id.lower()
        inst = instrument.upper()

        halt = self.get_active_halt(st, inst)
        if not halt:
            return {'ok': False, 'reason': 'not_halted', 'detail': f"{st}/{inst} is not currently halted"}

        halt_id = halt['halt_id']
        book = self.referee.books[st][inst] if self.referee and st in self.referee.books and inst in self.referee.books[st] else None
        if not book:
            return {'ok': False, 'reason': 'book_not_found', 'detail': f"No order book found for {st}/{inst}"}

        ref_price = self.get_vwap(st, inst)
        clearing_price, match_volume = find_clearing_price(book.bids, book.asks, ref_price)

        matched_trades = []
        trades_executed = 0

        if clearing_price is not None and match_volume > 0:
            # Execute crossed orders at the single clearing price
            # Bids with limit_price >= clearing_price matched with asks with limit_price <= clearing_price
            while book.bids and book.asks:
                best_bid = book.bids[0]
                best_ask = book.asks[0]

                if best_bid.limit_price < clearing_price or best_ask.limit_price > clearing_price:
                    break

                qty = min(best_bid.remaining_qty, best_ask.remaining_qty)
                if qty <= 0:
                    break

                next_seq = self.referee._get_next_seq() if self.referee else 1
                trades_executed += 1
                trade_id = f"trd-auc-{next_seq}-{trades_executed}"
                cost = clearing_price * qty

                # Update order object filled quantities
                best_bid.filled_qty += qty
                best_ask.filled_qty += qty

                # Settle atomically on ledger
                with self.conn:
                    txn_id = f"auction-{next_seq}-{trade_id}"
                    currency = 'CR'

                    # Currency deltas
                    self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?", (cost, best_bid.agent_id, currency))
                    self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (cost, best_ask.agent_id, currency))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (txn_id, next_seq, best_bid.agent_id, currency, -cost))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (txn_id, next_seq, best_ask.agent_id, currency, cost))

                    # Commodity deltas
                    self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (qty, best_bid.agent_id, inst))
                    self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?", (qty, best_ask.agent_id, inst))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (txn_id, next_seq, best_bid.agent_id, inst, qty))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (txn_id, next_seq, best_ask.agent_id, inst, -qty))

                    # Update orders table
                    self.conn.execute("""
                        UPDATE orders SET filled_qty = filled_qty + ?,
                            status = CASE WHEN filled_qty + ? >= qty THEN 'filled' ELSE status END,
                            resolved_seq = CASE WHEN filled_qty + ? >= qty THEN ? ELSE resolved_seq END
                        WHERE order_id = ? AND agent_id = ?
                    """, (qty, qty, qty, next_seq, best_bid.order_id, best_bid.agent_id))

                    self.conn.execute("""
                        UPDATE orders SET filled_qty = filled_qty + ?,
                            status = CASE WHEN filled_qty + ? >= qty THEN 'filled' ELSE status END,
                            resolved_seq = CASE WHEN filled_qty + ? >= qty THEN ? ELSE resolved_seq END
                        WHERE order_id = ? AND agent_id = ?
                    """, (qty, qty, qty, next_seq, best_ask.order_id, best_ask.agent_id))

                    # Record trade event
                    self.conn.execute("""
                        INSERT INTO book_events (seq, kind, payload) VALUES (?, 'trade', ?)
                    """, (next_seq, json.dumps({
                        'trade_id': trade_id,
                        'buyer_id': best_bid.agent_id,
                        'seller_id': best_ask.agent_id,
                        'price': clearing_price,
                        'qty': qty,
                        'cost': cost,
                        'station_id': st,
                        'instrument': inst,
                        'is_auction': True
                    })))

                matched_trades.append({
                    'trade_id': trade_id,
                    'buyer_id': best_bid.agent_id,
                    'seller_id': best_ask.agent_id,
                    'price': clearing_price,
                    'qty': qty
                })

                # Prune filled orders from in-memory book
                if best_bid.is_filled:
                    book.bids.pop(0)
                if best_ask.is_filled:
                    book.asks.pop(0)

            # Record auction trade into VWAP
            self.record_trade(st, inst, clearing_price, match_volume, round_num)

        # Mark halt as reopened in database
        final_price = clearing_price if clearing_price is not None else ref_price
        with self.conn:
            self.conn.execute("""
                UPDATE circuit_breaker_halts
                SET status = 'reopened', reopen_price = ?, reopen_volume = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE halt_id = ?
            """, (final_price, match_volume, halt_id))

            if self.referee:
                next_seq = self.referee._get_next_seq()
                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload) VALUES (?, 'circuit_breaker_reopen', ?)
                """, (next_seq, json.dumps({
                    'halt_id': halt_id,
                    'station_id': st,
                    'instrument': inst,
                    'reopen_round': round_num,
                    'clearing_price': clearing_price,
                    'reopen_volume': match_volume,
                    'trades_count': len(matched_trades),
                    'new_vwap': self.get_vwap(st, inst)
                })))

        return {
            'ok': True,
            'halt_id': halt_id,
            'station_id': st,
            'instrument': inst,
            'status': 'reopened',
            'round': round_num,
            'clearing_price': clearing_price,
            'reopen_volume': match_volume,
            'trades': matched_trades,
            'new_bands': self.get_bands(st, inst)
        }

    def step_round(self, new_round: int) -> List[Dict[str, Any]]:
        """
        Audits active halts during step_round.
        Triggers auction reopens for halts where new_round >= reopen_round.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT station_id, instrument, reopen_round, halt_id
            FROM circuit_breaker_halts
            WHERE status = 'halted' AND reopen_round <= ?
        """, (new_round,))
        expiring = cur.fetchall()

        reopen_reports = []
        for row in expiring:
            report = self.execute_auction_reopen(row['station_id'], row['instrument'], new_round)
            reopen_reports.append(report)

        return reopen_reports

    def get_all_bands(self) -> List[Dict[str, Any]]:
        """Returns LULD bands and statuses across all Sol stations and commodities."""
        from agora.spatial import STATIONS
        commodities = ['FRAG', 'FUEL']
        results = []
        for st in STATIONS:
            for comm in commodities:
                results.append(self.get_bands(st, comm))
        return results

    def get_halts(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns history of circuit breaker halts."""
        cur = self.conn.cursor()
        if status:
            cur.execute("""
                SELECT halt_id, station_id, instrument, halt_round, reopen_round,
                       trigger_price, lower_limit, upper_limit, vwap, reason, status,
                       reopen_price, reopen_volume, created_at, updated_at
                FROM circuit_breaker_halts WHERE status = ? ORDER BY created_at DESC
            """, (status,))
        else:
            cur.execute("""
                SELECT halt_id, station_id, instrument, halt_round, reopen_round,
                       trigger_price, lower_limit, upper_limit, vwap, reason, status,
                       reopen_price, reopen_volume, created_at, updated_at
                FROM circuit_breaker_halts ORDER BY created_at DESC
            """)
        return [dict(r) for r in cur.fetchall()]
