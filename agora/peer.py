"""
agora/peer.py - fleet-to-fleet goods trades agreed at a distance.

Spec: docs/fleet-market-spec.md section 1. Simulator evidence: #101.

A fleet docked at station S offers goods it holds. Any fleet, anywhere
(in transit included), accepts. Goods and CR both sit in SYSTEM escrow
until the buyer docks at S, when the goods go to the buyer and the CR to
the seller. If the buyer has not collected by the pickup deadline, both
sides are refunded. Every move is a balanced ledger entry.

Ships (#175): the goods come out of one ship docked at S (vessel_id, ship 1
by default) and are collected by whichever of the buyer's ships docks at S
first (the one named at accept, if it is there). A refund goes back to a
seller ship docked at S, or else into the seller's hold at S: goods never
jump to a ship somewhere else.

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
    status           TEXT NOT NULL CHECK (status IN ('offered', 'accepted', 'collected', 'expired', 'cancelled')),
    seller_vessel    TEXT,
    buyer_vessel     TEXT
)
"""


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {'v': 1, 'kind': 'reject', 'payload': {'reason': reason, 'detail': detail}}


class PeerDesk:
    def __init__(self, ref):
        self.ref = ref
        with ref.conn:
            ref.conn.execute(SCHEMA)
            cols = {r[1] for r in ref.conn.execute("PRAGMA table_info(station_escrow)")}
            for col in ('seller_vessel', 'buyer_vessel'):
                if col not in cols:
                    ref.conn.execute(f"ALTER TABLE station_escrow ADD COLUMN {col} TEXT")

    # ------------------------------------------------------------ ledger

    def _move(self, txn: str, legs) -> None:
        """Caller holds ref.lock and an open transaction."""
        conn, seq = self.ref.conn, self.ref._get_next_seq()
        for acct, inst, delta in legs:
            conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
            conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (delta, acct, inst))
            conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                         (txn, seq, acct, inst, delta))

    def _available(self, agent: str, inst: str, vessel_id=None) -> int:
        """Balance less what the agent's resting orders already commit: the
        corp's CR, or one ship's goods (ship 1 unless vessel_id)."""
        return self.ref.available(agent, inst, vessel_id)

    def _ship_at(self, agent: str, st: str, prefer=None):
        """The account a delivery to `agent` at `st` lands on: its ship
        docked there (`prefer` first, then the lowest-numbered), or None if
        none is. A non-roster agent is its own account while docked at st."""
        ref = self.ref
        if not ref.fleet.is_corp(agent):
            loc = ref.get_vessel_location(agent)
            return agent if loc.get('status') == 'docked' and loc.get('station_id') == st else None
        docked = [l['vessel_id'] for l in ref.fleet_locations(agent)
                  if l.get('status') == 'docked' and l.get('station_id') == st]
        if prefer in docked:
            return prefer
        return docked[0] if docked else None

    def _refund_account(self, row) -> str:
        """Where refunded goods go: a seller ship at the escrow station, else
        the seller's hold there (#175: never to a ship elsewhere)."""
        from agora.fleet import hold_account
        acct = self._ship_at(row['seller'], row['station_id'], row['seller_vessel'])
        if acct:
            return acct
        if not self.ref.fleet.is_corp(row['seller']):
            return row['seller']
        return hold_account(row['seller'], row['station_id'])

    def _row(self, escrow_id: str):
        return self.ref.conn.execute("SELECT * FROM station_escrow WHERE escrow_id = ?", (escrow_id,)).fetchone()

    # ------------------------------------------------------------ actions

    def offer(self, seller: str, station_id: str, instrument: str, qty: int, price: int,
              vessel_id=None) -> Dict[str, Any]:
        self.ref.mark_active(seller)
        if getattr(self.ref, 'fleet_out', None) and self.ref.fleet_out(seller):
            return _reject('fleet_out', self.ref.fleet_out(seller))
        ref = self.ref
        st, inst = (station_id or '').lower().strip(), (instrument or '').upper().strip()
        if st not in STATIONS:
            return _reject('invalid_station', f"Unknown station '{station_id}'. Valid: {STATIONS}")
        if inst not in COMMODITIES:
            return _reject('invalid_instrument', f"Offers take {COMMODITIES}, not '{instrument}'")
        if int(qty) <= 0 or int(price) <= 0:
            return _reject('invalid_qty_or_price', 'qty and price must be positive integers')
        with ref.lock, ref.conn:
            acct, err = ref.fleet.goods_account(seller, vessel_id)
            if err:
                return err
            loc = ref.get_vessel_location(seller, acct if acct != seller else None)
            if loc['status'] != 'docked' or loc['station_id'] != st:
                where = loc['station_id'] if loc['status'] == 'docked' else 'in transit'
                return _reject('vessel_not_docked',
                               f"You can only offer goods at the station your ship is docked at; {acct} is {where}, not {st}.")
            open_n = ref.conn.execute("SELECT COUNT(*) FROM station_escrow WHERE seller = ? AND status = 'offered'",
                                      (seller,)).fetchone()[0]
            if open_n >= MAX_OPEN_OFFERS_PER_FLEET:
                return _reject('too_many_offers', f"At most {MAX_OPEN_OFFERS_PER_FLEET} open offers per fleet")
            have = ref.available_account(acct, inst)
            if have < int(qty):
                return _reject('insufficient_balance', f"Offer needs {qty} {inst}; available {have}")
            # 12 hex digits: 6 (24 bits) collided within one simulated game, where
            # the peer desk creates and cancels thousands of offers (#162).
            eid = f"x{uuid.uuid4().hex[:12]}"
            self._move(f"peer-offer-{eid}", ((acct, inst, -int(qty)), ('SYSTEM', inst, int(qty))))
            ref.conn.execute("""INSERT INTO station_escrow
                (escrow_id, station_id, seller, instrument, qty, price, created_round, status, seller_vessel)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'offered', ?)""",
                             (eid, st, seller, inst, int(qty), int(price), ref.current_round, acct))
        return {'v': 1, 'kind': 'peer_offer_ok', 'payload': self.get(eid)}

    def accept(self, buyer: str, escrow_id: str, vessel_id=None) -> Dict[str, Any]:
        self.ref.mark_active(buyer)
        if getattr(self.ref, 'fleet_out', None) and self.ref.fleet_out(buyer):
            return _reject('fleet_out', self.ref.fleet_out(buyer))
        ref = self.ref
        with ref.lock, ref.conn:
            row = self._row(escrow_id)
            if not row or row['status'] != 'offered':
                return _reject('not_open', f"Offer '{escrow_id}' is not open")
            if row['seller'] == buyer:
                return _reject('self_trade', 'You cannot accept your own offer; cancel it instead')
            prefer = None
            if vessel_id is not None and ref.fleet.is_corp(buyer):
                prefer, err = ref.fleet.resolve(buyer, vessel_id)
                if err:
                    return err
            cost = row['qty'] * row['price']
            if self._available(buyer, 'CR') < cost:
                return _reject('insufficient_credits', f"Offer costs {cost} CR; available {self._available(buyer, 'CR')}")
            self._move(f"peer-accept-{escrow_id}", ((buyer, 'CR', -cost), ('SYSTEM', 'CR', cost)))
            ref.conn.execute("""UPDATE station_escrow SET buyer = ?, accepted_round = ?, pickup_deadline = ?,
                                status = 'accepted', buyer_vessel = ? WHERE escrow_id = ?""",
                             (buyer, ref.current_round, ref.current_round + PICKUP_ROUNDS, prefer, escrow_id))
            # A ship already docked there: collect now rather than next round.
            acct = self._ship_at(buyer, row['station_id'], prefer)
            if acct:
                self._collect_locked(escrow_id, acct)
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
            self._move(f"peer-cancel-{escrow_id}", (('SYSTEM', row['instrument'], -row['qty']),
                                                    (self._refund_account(row), row['instrument'], row['qty'])))
            ref.conn.execute("UPDATE station_escrow SET status = 'cancelled' WHERE escrow_id = ?", (escrow_id,))
        return {'v': 1, 'kind': 'peer_cancel_ok', 'payload': self.get(escrow_id)}

    # ------------------------------------------------------------ settlement

    def _collect_locked(self, escrow_id: str, acct: str) -> None:
        row = self._row(escrow_id)
        cost = row['qty'] * row['price']
        self._move(f"peer-collect-{escrow_id}", (
            ('SYSTEM', row['instrument'], -row['qty']), (acct, row['instrument'], row['qty']),
            ('SYSTEM', 'CR', -cost), (row['seller'], 'CR', cost)))
        self.ref.conn.execute("UPDATE station_escrow SET status = 'collected' WHERE escrow_id = ?", (escrow_id,))

    def step_locked(self, round_num: int) -> Dict[str, List[str]]:
        """Called from step_round, under ref.lock and inside its transaction,
        after arrivals have docked: collect for docked buyers, refund the
        expired."""
        ref, done = self.ref, {'collected': [], 'expired': []}
        for row in ref.conn.execute("SELECT * FROM station_escrow WHERE status = 'accepted'").fetchall():
            acct = self._ship_at(row['buyer'], row['station_id'], row['buyer_vessel'])
            if acct:
                self._collect_locked(row['escrow_id'], acct)
                done['collected'].append(row['escrow_id'])
            elif round_num > row['pickup_deadline']:
                cost = row['qty'] * row['price']
                self._move(f"peer-expire-{row['escrow_id']}", (
                    ('SYSTEM', row['instrument'], -row['qty']), (self._refund_account(row), row['instrument'], row['qty']),
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
        return [dict(r) for r in self.ref.conn.execute(q + " ORDER BY created_round, rowid", args)]

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

    def holdings_adjustment_by_station(self) -> Dict[str, Dict[str, Any]]:
        """What escrow owes each fleet, partitioned by station for local spot marks:
        {agent_id: {'CR': cr_amount, 'goods': [(station_id, instrument, qty), ...]}}
        Offered goods belong to the seller; accepted goods belong to the buyer,
        held at the escrow's station until collected (#196).
        """
        adj: Dict[str, Dict[str, Any]] = {}
        for r in self.ref.conn.execute("SELECT * FROM station_escrow WHERE status IN ('offered', 'accepted')"):
            st = r['station_id'].lower().strip()
            inst = r['instrument']
            qty = r['qty']
            if r['status'] == 'offered':
                seller = r['seller']
                adj.setdefault(seller, {'CR': 0, 'goods': []})
                adj[seller]['goods'].append((st, inst, qty))
            else:
                buyer = r['buyer']
                seller = r['seller']
                adj.setdefault(buyer, {'CR': 0, 'goods': []})
                adj[buyer]['goods'].append((st, inst, qty))
                adj.setdefault(seller, {'CR': 0, 'goods': []})
                adj[seller]['CR'] += qty * r['price']
        return adj
