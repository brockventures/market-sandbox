"""
agora/peer.py - fleet-to-fleet goods trades agreed at a distance.

Spec: docs/fleet-market-spec.md section 1. Simulator evidence: #101.

A fleet docked at station S offers goods it holds. Any fleet, anywhere
(in transit included), accepts. Goods and CR both sit in SYSTEM escrow
until the buyer docks at S, when the goods go to the buyer and the CR to
the seller. If the buyer has not collected by the pickup deadline, both
sides are refunded. Every move is a balanced ledger entry.

Off unless the referee's peer_trades flag is set (new_game or
AGORA_PEER_TRADES=1).
"""

import os
import uuid
from typing import Any, Dict, List, Optional

from agora.spatial import STATIONS, COMMODITIES

PICKUP_ROUNDS = 12
MAX_OPEN_OFFERS_PER_FLEET = 10


def env_peer_trades() -> bool:
    return os.environ.get("AGORA_PEER_TRADES", "").strip().lower() in ("1", "true", "yes", "on")


SCHEMA = """
CREATE TABLE IF NOT EXISTS station_escrow (
    escrow_id        TEXT PRIMARY KEY,
    station_id       TEXT NOT NULL,
    seller           TEXT NOT NULL,
    buyer            TEXT,
    instrument       TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    price            INTEGER NOT NULL,
    created_round    INTEGER NOT NULL,
    accepted_round   INTEGER,
    pickup_deadline  INTEGER,
    status           TEXT NOT NULL CHECK (status IN ('offered', 'accepted', 'collected', 'expired', 'cancelled'))
)
"""


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {'v': 1, 'kind': 'reject', 'payload': {'reason': reason, 'detail': detail}}


class PeerDesk:
    def __init__(self, ref):
        self.ref = ref
        with ref.conn:
            ref.conn.execute(SCHEMA)

    # ------------------------------------------------------------ ledger

    def _move(self, txn: str, legs) -> None:
        """Caller holds ref.lock and an open transaction."""
        conn, seq = self.ref.conn, self.ref._get_next_seq()
        for acct, inst, delta in legs:
            conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
            conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (delta, acct, inst))
            conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                         (txn, seq, acct, inst, delta))

    def _available(self, agent: str, inst: str) -> int:
        """Balance less what the agent's resting orders already commit."""
        committed = 0
        for books in self.ref.books.values():
            for comm, book in books.items():
                if inst == 'CR':
                    committed += sum(o.remaining_qty * o.limit_price for o in book.bids if o.agent_id == agent)
                elif comm == inst:
                    committed += sum(o.remaining_qty for o in book.asks if o.agent_id == agent)
        return self.ref.get_balance(agent, inst) - committed

    def _row(self, escrow_id: str):
        return self.ref.conn.execute("SELECT * FROM station_escrow WHERE escrow_id = ?", (escrow_id,)).fetchone()

    # ------------------------------------------------------------ actions

    def offer(self, seller: str, station_id: str, instrument: str, qty: int, price: int) -> Dict[str, Any]:
        self.ref.mark_active(seller)
        ref = self.ref
        st, inst = (station_id or '').lower().strip(), (instrument or '').upper().strip()
        if st not in STATIONS:
            return _reject('invalid_station', f"Unknown station '{station_id}'. Valid: {STATIONS}")
        if inst not in COMMODITIES:
            return _reject('invalid_instrument', f"Offers take {COMMODITIES}, not '{instrument}'")
        if int(qty) <= 0 or int(price) <= 0:
            return _reject('invalid_qty_or_price', 'qty and price must be positive integers')
        with ref.lock, ref.conn:
            loc = ref.get_vessel_location(seller)
            if loc['status'] != 'docked' or loc['station_id'] != st:
                where = loc['station_id'] if loc['status'] == 'docked' else 'in transit'
                return _reject('vessel_not_docked',
                               f"You can only offer goods at the station you are docked at; you are {where}, not {st}.")
            open_n = ref.conn.execute("SELECT COUNT(*) FROM station_escrow WHERE seller = ? AND status = 'offered'",
                                      (seller,)).fetchone()[0]
            if open_n >= MAX_OPEN_OFFERS_PER_FLEET:
                return _reject('too_many_offers', f"At most {MAX_OPEN_OFFERS_PER_FLEET} open offers per fleet")
            if self._available(seller, inst) < int(qty):
                return _reject('insufficient_balance', f"Offer needs {qty} {inst}; available {self._available(seller, inst)}")
            eid = f"x{uuid.uuid4().hex[:6]}"
            self._move(f"peer-offer-{eid}", ((seller, inst, -int(qty)), ('SYSTEM', inst, int(qty))))
            ref.conn.execute("""INSERT INTO station_escrow
                (escrow_id, station_id, seller, instrument, qty, price, created_round, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'offered')""",
                             (eid, st, seller, inst, int(qty), int(price), ref.current_round))
        return {'v': 1, 'kind': 'peer_offer_ok', 'payload': self.get(eid)}

    def accept(self, buyer: str, escrow_id: str) -> Dict[str, Any]:
        self.ref.mark_active(buyer)
        ref = self.ref
        with ref.lock, ref.conn:
            row = self._row(escrow_id)
            if not row or row['status'] != 'offered':
                return _reject('not_open', f"Offer '{escrow_id}' is not open")
            if row['seller'] == buyer:
                return _reject('self_trade', 'You cannot accept your own offer; cancel it instead')
            cost = row['qty'] * row['price']
            if self._available(buyer, 'CR') < cost:
                return _reject('insufficient_credits', f"Offer costs {cost} CR; available {self._available(buyer, 'CR')}")
            self._move(f"peer-accept-{escrow_id}", ((buyer, 'CR', -cost), ('SYSTEM', 'CR', cost)))
            ref.conn.execute("""UPDATE station_escrow SET buyer = ?, accepted_round = ?, pickup_deadline = ?,
                                status = 'accepted' WHERE escrow_id = ?""",
                             (buyer, ref.current_round, ref.current_round + PICKUP_ROUNDS, escrow_id))
            # Already docked there: collect now rather than next round.
            loc = ref.get_vessel_location(buyer)
            if loc['status'] == 'docked' and loc['station_id'] == row['station_id']:
                self._collect_locked(escrow_id)
        return {'v': 1, 'kind': 'peer_accept_ok', 'payload': self.get(escrow_id)}

    def cancel(self, seller: str, escrow_id: str) -> Dict[str, Any]:
        self.ref.mark_active(seller)
        ref = self.ref
        with ref.lock, ref.conn:
            row = self._row(escrow_id)
            if not row or row['status'] != 'offered':
                return _reject('not_open', f"Offer '{escrow_id}' is not open (accepted offers cannot be cancelled)")
            if row['seller'] != seller:
                return _reject('unauthorized', f"Offer '{escrow_id}' belongs to {row['seller']}")
            self._move(f"peer-cancel-{escrow_id}", (('SYSTEM', row['instrument'], -row['qty']), (seller, row['instrument'], row['qty'])))
            ref.conn.execute("UPDATE station_escrow SET status = 'cancelled' WHERE escrow_id = ?", (escrow_id,))
        return {'v': 1, 'kind': 'peer_cancel_ok', 'payload': self.get(escrow_id)}

    # ------------------------------------------------------------ settlement

    def _collect_locked(self, escrow_id: str) -> None:
        row = self._row(escrow_id)
        cost = row['qty'] * row['price']
        self._move(f"peer-collect-{escrow_id}", (
            ('SYSTEM', row['instrument'], -row['qty']), (row['buyer'], row['instrument'], row['qty']),
            ('SYSTEM', 'CR', -cost), (row['seller'], 'CR', cost)))
        self.ref.conn.execute("UPDATE station_escrow SET status = 'collected' WHERE escrow_id = ?", (escrow_id,))

    def step_locked(self, round_num: int) -> Dict[str, List[str]]:
        """Called from step_round, under ref.lock and inside its transaction,
        after arrivals have docked: collect for docked buyers, refund the
        expired."""
        ref, done = self.ref, {'collected': [], 'expired': []}
        for row in ref.conn.execute("SELECT * FROM station_escrow WHERE status = 'accepted'").fetchall():
            loc = ref.get_vessel_location(row['buyer'])
            if loc['status'] == 'docked' and loc['station_id'] == row['station_id']:
                self._collect_locked(row['escrow_id'])
                done['collected'].append(row['escrow_id'])
            elif round_num > row['pickup_deadline']:
                cost = row['qty'] * row['price']
                self._move(f"peer-expire-{row['escrow_id']}", (
                    ('SYSTEM', row['instrument'], -row['qty']), (row['seller'], row['instrument'], row['qty']),
                    ('SYSTEM', 'CR', -cost), (row['buyer'], 'CR', cost)))
                ref.conn.execute("UPDATE station_escrow SET status = 'expired' WHERE escrow_id = ?", (row['escrow_id'],))
                done['expired'].append(row['escrow_id'])
        return done

    # ------------------------------------------------------------ reads

    def get(self, escrow_id: str) -> Optional[Dict[str, Any]]:
        row = self._row(escrow_id)
        return dict(row) if row else None

    def list(self, station_id: Optional[str] = None, status: str = 'offered') -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM station_escrow WHERE status = ?", [status]
        if station_id:
            q += " AND station_id = ?"
            args.append(station_id.lower())
        return [dict(r) for r in self.ref.conn.execute(q + " ORDER BY created_round, escrow_id", args)]

    def holdings_adjustment(self) -> Dict[str, Dict[str, int]]:
        """What escrow owes each fleet, for net worth: offered goods still
        belong to the seller; once accepted, the goods belong to the buyer and
        the CR to the seller."""
        adj: Dict[str, Dict[str, int]] = {}
        for r in self.ref.conn.execute("SELECT * FROM station_escrow WHERE status IN ('offered', 'accepted')"):
            if r['status'] == 'offered':
                adj.setdefault(r['seller'], {}).setdefault(r['instrument'], 0)
                adj[r['seller']][r['instrument']] += r['qty']
            else:
                adj.setdefault(r['buyer'], {}).setdefault(r['instrument'], 0)
                adj[r['buyer']][r['instrument']] += r['qty']
                adj.setdefault(r['seller'], {}).setdefault('CR', 0)
                adj[r['seller']]['CR'] += r['qty'] * r['price']
        return adj
