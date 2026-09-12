"""
agora.salvage - Corporate Warfare: Derelict Salvage Claims & Distress Escrow RFQs.

Defines distress beacons for stranded vessels (0 fuel / out of propellant),
extortionate rescue RFQ quoting and atomic referee settlement,
and competitive derelict cargo salvage claims for Issue #22.
"""

import json
import time
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple


@dataclass
class DistressBeacon:
    beacon_id: str
    agent_id: str
    location: str
    origin: Optional[str]
    destination: Optional[str]
    transit_id: Optional[str]
    round_declared: int
    reason: str
    cargo_bounty: Dict[str, int]
    status: str
    rescued_by: Optional[str] = None
    salvaged_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RescueRFQ:
    rfq_id: str
    beacon_id: str
    agent_id: str
    fuel_needed: int
    max_reward_cr: int
    status: str
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RescueQuote:
    quote_id: str
    rfq_id: str
    beacon_id: str
    rescuer_id: str
    fuel_offered: int
    price_cr: int
    status: str
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DerelictSalvageEngine:
    """
    Manages distress beacons, rescue RFQs with atomic referee escrow,
    and competitive derelict salvage claims.
    """

    def __init__(self, conn, referee=None):
        self.conn = conn
        self.referee = referee
        self._ensure_tables()

    def _ensure_tables(self):
        """Creates distress and salvage tables if not present."""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS distress_beacons (
                    beacon_id       TEXT PRIMARY KEY,
                    agent_id        TEXT NOT NULL,
                    location        TEXT NOT NULL,
                    origin          TEXT,
                    destination     TEXT,
                    transit_id      TEXT,
                    round_declared  INTEGER NOT NULL,
                    reason          TEXT NOT NULL DEFAULT 'out_of_fuel',
                    cargo_bounty    TEXT NOT NULL DEFAULT '{}',
                    status          TEXT NOT NULL CHECK (status IN ('active', 'rescued', 'salvaged', 'cancelled')),
                    rescued_by      TEXT,
                    salvaged_by     TEXT,
                    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS rescue_rfqs (
                    rfq_id          TEXT PRIMARY KEY,
                    beacon_id       TEXT NOT NULL,
                    agent_id        TEXT NOT NULL,
                    fuel_needed     INTEGER NOT NULL,
                    max_reward_cr   INTEGER NOT NULL DEFAULT 0,
                    status          TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'expired', 'cancelled')),
                    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS rescue_quotes (
                    quote_id        TEXT PRIMARY KEY,
                    rfq_id          TEXT NOT NULL,
                    beacon_id       TEXT NOT NULL,
                    rescuer_id      TEXT NOT NULL,
                    fuel_offered    INTEGER NOT NULL,
                    price_cr        INTEGER NOT NULL,
                    status          TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected', 'expired')),
                    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS salvage_claims (
                    claim_id        TEXT PRIMARY KEY,
                    beacon_id       TEXT NOT NULL,
                    salvager_id     TEXT NOT NULL,
                    cargo_claimed   TEXT NOT NULL,
                    claim_round     INTEGER NOT NULL,
                    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)

    def broadcast_distress(
        self,
        agent_id: str,
        location: Optional[str] = None,
        cargo_bounty: Optional[Dict[str, int]] = None,
        transit_id: Optional[str] = None,
        fuel_needed: int = 15,
        max_reward_cr: int = 0,
        reason: str = "out_of_fuel"
    ) -> Dict[str, Any]:
        """
        Broadcast a distress beacon for a stranded vessel and open an initial Rescue RFQ.
        """
        cur = self.conn.cursor()
        origin = None
        destination = None
        current_round = getattr(self.referee, "current_round", 0)

        # 1. Resolve transit linkage if specified
        if transit_id:
            cur.execute("""
                SELECT transit_id, agent_id, origin, destination, commodity, cargo_qty, status
                FROM transits WHERE transit_id = ?
            """, (transit_id,))
            t_row = cur.fetchone()
            if not t_row:
                return {'ok': False, 'reason': 'transit_not_found', 'detail': f"No transit '{transit_id}' found"}
            if t_row['agent_id'] != agent_id:
                return {'ok': False, 'reason': 'unauthorized', 'detail': f"Transit '{transit_id}' belongs to '{t_row['agent_id']}', not '{agent_id}'"}
            origin = t_row['origin']
            destination = t_row['destination']
            if not location:
                location = f"{origin}_{destination}"
            if cargo_bounty is None and t_row['cargo_qty'] > 0:
                cargo_bounty = {t_row['commodity']: t_row['cargo_qty']}

        # 2. Resolve location if not yet resolved
        if not location:
            cur.execute("SELECT station_id FROM vessel_locations WHERE agent_id = ?", (agent_id,))
            loc_row = cur.fetchone()
            location = loc_row[0] if loc_row else "ceres"

        # 3. Clean cargo bounty
        cleaned_bounty = {}
        if cargo_bounty:
            for k, v in cargo_bounty.items():
                try:
                    qty = int(v)
                    if qty > 0:
                        cleaned_bounty[str(k).upper()] = qty
                except (ValueError, TypeError):
                    pass

        # 4. Check for existing active beacon for this agent
        cur.execute("SELECT beacon_id FROM distress_beacons WHERE agent_id = ? AND status = 'active'", (agent_id,))
        existing = cur.fetchone()
        if existing:
            return {
                'ok': False,
                'reason': 'active_beacon_exists',
                'detail': f"Agent '{agent_id}' already has an active distress beacon: {existing['beacon_id']}"
            }

        beacon_id = f"beacon-{agent_id}-{int(time.time() * 1000)}"
        rfq_id = f"rfq-{beacon_id}"

        with self.conn:
            # Insert beacon
            self.conn.execute("""
                INSERT INTO distress_beacons (beacon_id, agent_id, location, origin, destination, transit_id, round_declared, reason, cargo_bounty, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """, (beacon_id, agent_id, location, origin, destination, transit_id, current_round, reason, json.dumps(cleaned_bounty)))

            # Create initial RFQ
            self.conn.execute("""
                INSERT INTO rescue_rfqs (rfq_id, beacon_id, agent_id, fuel_needed, max_reward_cr, status)
                VALUES (?, ?, ?, ?, ?, 'open')
            """, (rfq_id, beacon_id, agent_id, fuel_needed, max_reward_cr))

            # Emit book event tick
            if self.referee:
                next_seq = self.referee._get_next_seq()
                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload) VALUES (?, 'distress', ?)
                """, (next_seq, json.dumps({
                    'event': 'distress_beacon_declared',
                    'beacon_id': beacon_id,
                    'agent_id': agent_id,
                    'location': location,
                    'origin': origin,
                    'destination': destination,
                    'transit_id': transit_id,
                    'cargo_bounty': cleaned_bounty,
                    'fuel_needed': fuel_needed,
                    'max_reward_cr': max_reward_cr,
                    'round': current_round,
                })))

        return {
            'ok': True,
            'beacon_id': beacon_id,
            'rfq_id': rfq_id,
            'agent_id': agent_id,
            'location': location,
            'cargo_bounty': cleaned_bounty,
            'fuel_needed': fuel_needed,
            'status': 'active'
        }

    def submit_rescue_quote(
        self,
        rescuer_id: str,
        rfq_id: str,
        fuel_offered: int,
        price_cr: int
    ) -> Dict[str, Any]:
        """
        Submit a rescue quote offering fuel at a specified credit price.
        Price can be extortionate. Fuel solvency is checked against available balance.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT r.rfq_id, r.beacon_id, r.agent_id, r.fuel_needed, r.max_reward_cr, r.status, b.status as beacon_status
            FROM rescue_rfqs r
            JOIN distress_beacons b ON r.beacon_id = b.beacon_id
            WHERE r.rfq_id = ?
        """, (rfq_id,))
        rfq = cur.fetchone()
        if not rfq:
            return {'ok': False, 'reason': 'rfq_not_found', 'detail': f"No RFQ '{rfq_id}' found"}
        if rfq['status'] != 'open' or rfq['beacon_status'] != 'active':
            return {'ok': False, 'reason': 'rfq_not_open', 'detail': f"RFQ '{rfq_id}' is {rfq['status']} (beacon: {rfq['beacon_status']})"}
        if rescuer_id == rfq['agent_id']:
            return {'ok': False, 'reason': 'cannot_rescue_self', 'detail': "Cannot submit rescue quote to your own distress beacon"}

        try:
            fuel_offered = int(fuel_offered)
            price_cr = int(price_cr)
        except (ValueError, TypeError):
            return {'ok': False, 'reason': 'invalid_format', 'detail': "fuel_offered and price_cr must be integers"}

        if fuel_offered < rfq['fuel_needed']:
            return {'ok': False, 'reason': 'insufficient_fuel_offered', 'detail': f"RFQ requires at least {rfq['fuel_needed']} FUEL, offered {fuel_offered}"}
        if price_cr < 0:
            return {'ok': False, 'reason': 'invalid_price', 'detail': "price_cr cannot be negative"}

        # Solvency check: ensure rescuer has enough available FUEL
        if self.referee:
            fuel_bal = self.referee.get_balance(rescuer_id, 'FUEL')
            committed_fuel = sum(
                o.remaining_qty
                for st_books in self.referee.books.values()
                for o in st_books.get('FUEL', []).asks if hasattr(st_books.get('FUEL', []), 'asks')
                if o.agent_id == rescuer_id
            )
            avail_fuel = fuel_bal - committed_fuel
            if avail_fuel < fuel_offered:
                return {
                    'ok': False,
                    'reason': 'insufficient_available_fuel',
                    'detail': f"Rescuer has {avail_fuel} FUEL available (balance {fuel_bal}, committed {committed_fuel}), requires {fuel_offered}"
                }

        quote_id = f"quote-{rfq_id}-{rescuer_id}-{int(time.time() * 1000)}"
        with self.conn:
            self.conn.execute("""
                INSERT INTO rescue_quotes (quote_id, rfq_id, beacon_id, rescuer_id, fuel_offered, price_cr, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """, (quote_id, rfq_id, rfq['beacon_id'], rescuer_id, fuel_offered, price_cr))

            if self.referee:
                next_seq = self.referee._get_next_seq()
                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload) VALUES (?, 'rescue', ?)
                """, (next_seq, json.dumps({
                    'event': 'rescue_quote_submitted',
                    'quote_id': quote_id,
                    'rfq_id': rfq_id,
                    'beacon_id': rfq['beacon_id'],
                    'rescuer_id': rescuer_id,
                    'fuel_offered': fuel_offered,
                    'price_cr': price_cr,
                })))

        return {
            'ok': True,
            'quote_id': quote_id,
            'rfq_id': rfq_id,
            'beacon_id': rfq['beacon_id'],
            'rescuer_id': rescuer_id,
            'fuel_offered': fuel_offered,
            'price_cr': price_cr,
            'status': 'pending'
        }

    def accept_rescue_quote(
        self,
        agent_id: str,
        quote_id: str
    ) -> Dict[str, Any]:
        """
        Stranded agent accepts a rescue quote.
        Atomically transfers fuel and credits on the double-entry referee ledger.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT q.quote_id, q.rfq_id, q.beacon_id, q.rescuer_id, q.fuel_offered, q.price_cr, q.status as quote_status,
                   r.agent_id as stranded_agent, r.status as rfq_status,
                   b.status as beacon_status, b.transit_id
            FROM rescue_quotes q
            JOIN rescue_rfqs r ON q.rfq_id = r.rfq_id
            JOIN distress_beacons b ON q.beacon_id = b.beacon_id
            WHERE q.quote_id = ?
        """, (quote_id,))
        row = cur.fetchone()
        if not row:
            return {'ok': False, 'reason': 'quote_not_found', 'detail': f"No quote '{quote_id}' found"}
        if row['quote_status'] != 'pending':
            return {'ok': False, 'reason': 'quote_not_pending', 'detail': f"Quote is already {row['quote_status']}"}
        if row['rfq_status'] != 'open':
            return {'ok': False, 'reason': 'rfq_not_open', 'detail': f"RFQ is already {row['rfq_status']}"}
        if row['beacon_status'] != 'active':
            return {'ok': False, 'reason': 'beacon_not_active', 'detail': f"Distress beacon is already {row['beacon_status']}"}
        if agent_id != row['stranded_agent']:
            return {'ok': False, 'reason': 'unauthorized', 'detail': f"Only stranded agent '{row['stranded_agent']}' can accept quotes (caller: '{agent_id}')"}

        rescuer_id = row['rescuer_id']
        fuel_offered = row['fuel_offered']
        price_cr = row['price_cr']
        beacon_id = row['beacon_id']
        rfq_id = row['rfq_id']
        transit_id = row['transit_id']

        # 1. Audit solvency: stranded agent must have price_cr available
        if self.referee:
            cr_bal = self.referee.get_balance(agent_id, 'CR')
            committed_cr = sum(
                o.remaining_qty * o.limit_price
                for st_books in self.referee.books.values()
                for b in st_books.values()
                for o in b.bids
                if o.agent_id == agent_id
            )
            avail_cr = cr_bal - committed_cr
            if avail_cr < price_cr:
                return {
                    'ok': False,
                    'reason': 'insufficient_funds',
                    'detail': f"Stranded agent has {avail_cr} CR available (balance {cr_bal}, committed {committed_cr}), requires {price_cr} CR"
                }

            # Rescuer must have fuel_offered available
            fuel_bal = self.referee.get_balance(rescuer_id, 'FUEL')
            committed_fuel = sum(
                o.remaining_qty
                for st_books in self.referee.books.values()
                for o in st_books.get('FUEL', []).asks if hasattr(st_books.get('FUEL', []), 'asks')
                if o.agent_id == rescuer_id
            )
            avail_fuel = fuel_bal - committed_fuel
            if avail_fuel < fuel_offered:
                return {
                    'ok': False,
                    'reason': 'rescuer_insufficient_fuel',
                    'detail': f"Rescuer has {avail_fuel} FUEL available, requires {fuel_offered}"
                }

        next_seq = self.referee._get_next_seq() if self.referee else 1
        txn_id = f"rescue-{quote_id}"

        with self.conn:
            # 2. Settle Credits: stranded agent -> rescuer
            if price_cr > 0:
                self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'CR'", (price_cr, agent_id))
                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (price_cr, rescuer_id))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)", (txn_id, next_seq, agent_id, -price_cr))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)", (txn_id, next_seq, rescuer_id, price_cr))

            # 3. Settle Fuel: rescuer -> stranded agent
            self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'FUEL'", (fuel_offered, rescuer_id))
            self.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, 'FUEL', 0)", (agent_id,))
            self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'FUEL'", (fuel_offered, agent_id))
            self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'FUEL', ?)", (txn_id, next_seq, rescuer_id, -fuel_offered))
            self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'FUEL', ?)", (txn_id, next_seq, agent_id, fuel_offered))

            # 4. Update status
            self.conn.execute("UPDATE rescue_quotes SET status = 'accepted' WHERE quote_id = ?", (quote_id,))
            self.conn.execute("UPDATE rescue_quotes SET status = 'rejected' WHERE rfq_id = ? AND quote_id != ?", (rfq_id, quote_id))
            self.conn.execute("UPDATE rescue_rfqs SET status = 'accepted' WHERE rfq_id = ?", (rfq_id,))
            self.conn.execute("""
                UPDATE distress_beacons
                SET status = 'rescued', rescued_by = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE beacon_id = ?
            """, (rescuer_id, beacon_id))

            # 5. Book event tick
            if self.referee:
                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload) VALUES (?, 'rescue', ?)
                """, (next_seq, json.dumps({
                    'event': 'rescue_settled',
                    'quote_id': quote_id,
                    'beacon_id': beacon_id,
                    'agent_id': agent_id,
                    'rescuer_id': rescuer_id,
                    'fuel_delivered': fuel_offered,
                    'price_paid_cr': price_cr,
                    'transit_id': transit_id,
                })))

        return {
            'ok': True,
            'quote_id': quote_id,
            'beacon_id': beacon_id,
            'agent_id': agent_id,
            'rescuer_id': rescuer_id,
            'fuel_delivered': fuel_offered,
            'price_paid_cr': price_cr,
            'status': 'rescued'
        }

    def claim_salvage(
        self,
        salvager_id: str,
        beacon_id: str
    ) -> Dict[str, Any]:
        """
        Rival agent docks with derelict wreck and claims the cargo bounty.
        Atomically transfers cargo bounty to the salvager.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT beacon_id, agent_id, location, origin, destination, transit_id, cargo_bounty, status, round_declared
            FROM distress_beacons
            WHERE beacon_id = ?
        """, (beacon_id,))
        beacon = cur.fetchone()
        if not beacon:
            return {'ok': False, 'reason': 'beacon_not_found', 'detail': f"No distress beacon '{beacon_id}' found"}
        if beacon['status'] != 'active':
            return {'ok': False, 'reason': 'beacon_not_active', 'detail': f"Beacon '{beacon_id}' is already {beacon['status']}"}
        if salvager_id == beacon['agent_id']:
            return {'ok': False, 'reason': 'cannot_salvage_self', 'detail': "Cannot claim salvage on your own vessel"}

        # Parse cargo bounty
        cargo_bounty = {}
        if beacon['cargo_bounty']:
            try:
                raw_bounty = json.loads(beacon['cargo_bounty'])
                for k, v in raw_bounty.items():
                    if int(v) > 0:
                        cargo_bounty[str(k).upper()] = int(v)
            except Exception:
                pass

        current_round = getattr(self.referee, "current_round", 0)
        claim_id = f"salvage-{beacon_id}-{salvager_id}-{int(time.time() * 1000)}"
        txn_id = f"salvage-{claim_id}"
        next_seq = self.referee._get_next_seq() if self.referee else 1
        transit_id = beacon['transit_id']

        with self.conn:
            # Transfer cargo bounties
            for comm, qty in cargo_bounty.items():
                if qty <= 0:
                    continue
                # If cargo was escrowed in transit, source is SYSTEM. Otherwise source is stranded agent.
                source_agent = 'SYSTEM' if transit_id else beacon['agent_id']

                # Deduct from source
                self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?", (qty, source_agent, comm))
                # Credit to salvager
                self.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (salvager_id, comm))
                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (qty, salvager_id, comm))
                # Ledger entries
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (txn_id, next_seq, source_agent, comm, -qty))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (txn_id, next_seq, salvager_id, comm, qty))

            # Mark transit cancelled if linked
            if transit_id:
                self.conn.execute("UPDATE transits SET status = 'cancelled' WHERE transit_id = ?", (transit_id,))

            # Mark beacon salvaged
            self.conn.execute("""
                UPDATE distress_beacons
                SET status = 'salvaged', salvaged_by = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE beacon_id = ?
            """, (salvager_id, beacon_id))

            # Cancel open RFQs
            self.conn.execute("UPDATE rescue_rfqs SET status = 'cancelled' WHERE beacon_id = ? AND status = 'open'", (beacon_id,))
            self.conn.execute("UPDATE rescue_quotes SET status = 'expired' WHERE beacon_id = ? AND status = 'pending'", (beacon_id,))

            # Record salvage claim
            self.conn.execute("""
                INSERT INTO salvage_claims (claim_id, beacon_id, salvager_id, cargo_claimed, claim_round)
                VALUES (?, ?, ?, ?, ?)
            """, (claim_id, beacon_id, salvager_id, json.dumps(cargo_bounty), current_round))

            # Emit book event tick
            if self.referee:
                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload) VALUES (?, 'salvage', ?)
                """, (next_seq, json.dumps({
                    'event': 'salvage_claimed',
                    'claim_id': claim_id,
                    'beacon_id': beacon_id,
                    'salvager_id': salvager_id,
                    'original_agent': beacon['agent_id'],
                    'location': beacon['location'],
                    'cargo_claimed': cargo_bounty,
                    'round': current_round,
                })))

        return {
            'ok': True,
            'claim_id': claim_id,
            'beacon_id': beacon_id,
            'salvager_id': salvager_id,
            'original_agent': beacon['agent_id'],
            'cargo_claimed': cargo_bounty,
            'status': 'salvaged'
        }

    def get_beacons(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns distress beacons, optionally filtered by status ('active', 'rescued', 'salvaged')."""
        cur = self.conn.cursor()
        if status:
            cur.execute("""
                SELECT beacon_id, agent_id, location, origin, destination, transit_id, round_declared, reason, cargo_bounty, status, rescued_by, salvaged_by, created_at, updated_at
                FROM distress_beacons WHERE status = ? ORDER BY round_declared DESC, created_at DESC
            """, (status,))
        else:
            cur.execute("""
                SELECT beacon_id, agent_id, location, origin, destination, transit_id, round_declared, reason, cargo_bounty, status, rescued_by, salvaged_by, created_at, updated_at
                FROM distress_beacons ORDER BY round_declared DESC, created_at DESC
            """)
        beacons = []
        for r in cur.fetchall():
            bounty = {}
            if r['cargo_bounty']:
                try:
                    bounty = json.loads(r['cargo_bounty'])
                except Exception:
                    pass
            beacons.append({
                'beacon_id': r['beacon_id'],
                'agent_id': r['agent_id'],
                'location': r['location'],
                'origin': r['origin'],
                'destination': r['destination'],
                'transit_id': r['transit_id'],
                'round_declared': r['round_declared'],
                'reason': r['reason'],
                'cargo_bounty': bounty,
                'status': r['status'],
                'rescued_by': r['rescued_by'],
                'salvaged_by': r['salvaged_by'],
                'created_at': r['created_at'],
                'updated_at': r['updated_at'],
            })
        return beacons

    def get_rfqs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns rescue RFQs with attached quotes."""
        cur = self.conn.cursor()
        query = """
            SELECT r.rfq_id, r.beacon_id, r.agent_id, r.fuel_needed, r.max_reward_cr, r.status, r.created_at,
                   b.location, b.cargo_bounty
            FROM rescue_rfqs r
            JOIN distress_beacons b ON r.beacon_id = b.beacon_id
        """
        if status:
            query += " WHERE r.status = ? ORDER BY r.created_at DESC"
            cur.execute(query, (status,))
        else:
            query += " ORDER BY r.created_at DESC"
            cur.execute(query)

        rfqs = []
        for r in cur.fetchall():
            # Get quotes for this RFQ
            cur.execute("""
                SELECT quote_id, rescuer_id, fuel_offered, price_cr, status, created_at
                FROM rescue_quotes WHERE rfq_id = ? ORDER BY price_cr ASC, created_at ASC
            """, (r['rfq_id'],))
            quotes = [dict(q) for q in cur.fetchall()]

            bounty = {}
            if r['cargo_bounty']:
                try:
                    bounty = json.loads(r['cargo_bounty'])
                except Exception:
                    pass

            rfqs.append({
                'rfq_id': r['rfq_id'],
                'beacon_id': r['beacon_id'],
                'agent_id': r['agent_id'],
                'location': r['location'],
                'cargo_bounty': bounty,
                'fuel_needed': r['fuel_needed'],
                'max_reward_cr': r['max_reward_cr'],
                'status': r['status'],
                'created_at': r['created_at'],
                'quotes': quotes
            })
        return rfqs

    def get_claims(self) -> List[Dict[str, Any]]:
        """Returns all completed salvage claims."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT claim_id, beacon_id, salvager_id, cargo_claimed, claim_round, created_at
            FROM salvage_claims ORDER BY claim_round DESC, created_at DESC
        """)
        claims = []
        for r in cur.fetchall():
            cargo = {}
            if r['cargo_claimed']:
                try:
                    cargo = json.loads(r['cargo_claimed'])
                except Exception:
                    pass
            claims.append({
                'claim_id': r['claim_id'],
                'beacon_id': r['beacon_id'],
                'salvager_id': r['salvager_id'],
                'cargo_claimed': cargo,
                'claim_round': r['claim_round'],
                'created_at': r['created_at']
            })
        return claims

    def get_salvage_summary(self) -> Dict[str, Any]:
        """Returns high-level statistics for distress beacons, RFQs, and salvage claims."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM distress_beacons WHERE status = 'active'")
        active_beacons = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM distress_beacons WHERE status = 'rescued'")
        rescued_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM distress_beacons WHERE status = 'salvaged'")
        salvaged_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM rescue_rfqs WHERE status = 'open'")
        open_rfqs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM rescue_quotes WHERE status = 'pending'")
        pending_quotes = cur.fetchone()[0]

        return {
            'active_beacons': active_beacons,
            'rescued_count': rescued_count,
            'salvaged_count': salvaged_count,
            'open_rfqs': open_rfqs,
            'pending_quotes': pending_quotes,
            'beacons': self.get_beacons(),
            'recent_claims': self.get_claims()[:5]
        }
