"""
agora.referee - Central referee for order validation, solvency auditing,
book matching, and atomic double-entry ledger settlement.
"""

import sqlite3
import json
import time
import copy
import math
import threading
from typing import Optional, Dict, Any, Tuple, List
from pathlib import Path

from agora.order_book import OrderBook, Order, Trade
from agora.galnet import GalNetEngine
from agora.spatial import (
    StationPriceEngine, STATIONS, COMMODITIES, BASE_PRICES, get_route, ROUTES,
    get_alignment_windows, PERISHABLE_COMMODITIES
)
from agora.equity import (
    SyndicateEquityEngine, FLEET_EQUITIES, EQUITY_SYMBOLS,
    AGENT_BY_SYMBOL, DEFAULT_BORROW_FEE_RATE
)
from agora.salvage import DerelictSalvageEngine


class AgoraReferee:
    def __init__(self, db_path: str = ':memory:', instrument: Optional[str] = None, galnet: Optional[GalNetEngine] = None, spatial: Optional[StationPriceEngine] = None):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.default_instrument = instrument or 'FRAG'
        self.galnet = galnet or GalNetEngine()
        self.spatial = spatial or StationPriceEngine()
        self.current_round: int = 0
        self.last_price: Optional[int] = None
        self.last_qty: Optional[int] = None
        self.floor: str = 'open'
        # Multi-station order books across Sol nodes, supporting commodities and equities
        self.books: Dict[str, Dict[str, OrderBook]] = {
            st: {comm: OrderBook(instrument=comm) for comm in ('FRAG', 'BANANA', 'FUEL', *EQUITY_SYMBOLS)}
            for st in STATIONS
        }
        self.book = self.books['ceres'][self.default_instrument if self.default_instrument in self.books['ceres'] else 'FRAG']
        self._init_db()
        self.equity = SyndicateEquityEngine(self.conn, self)
        self.salvage = DerelictSalvageEngine(self.conn, self)

    def _init_db(self):
        """Load schema and genesis seed if database is uninitialized, and rehydrate book from open orders."""
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
            else:
                # Migration check: ensure filled_qty and station_id columns exist in orders table
                if 'orders' in tables:
                    cols = [r[1] for r in self.conn.execute("PRAGMA table_info(orders)").fetchall()]
                    if 'filled_qty' not in cols:
                        self.conn.execute("ALTER TABLE orders ADD COLUMN filled_qty INTEGER NOT NULL DEFAULT 0")
                    if 'station_id' not in cols:
                        self.conn.execute("ALTER TABLE orders ADD COLUMN station_id TEXT NOT NULL DEFAULT 'ceres'")

                # Migration: ensure spatial tables exist if database pre-dated Phase 2
                if 'station_prices' not in tables:
                    self.conn.execute("""
                        CREATE TABLE IF NOT EXISTS station_prices (
                            station_id  TEXT NOT NULL,
                            commodity   TEXT NOT NULL,
                            round       INTEGER NOT NULL,
                            base_price  REAL NOT NULL,
                            drift_bias  REAL NOT NULL DEFAULT 0.0,
                            spot_price  REAL NOT NULL,
                            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                            PRIMARY KEY (station_id, commodity, round)
                        )
                    """)
                if 'transits' not in tables:
                    self.conn.execute("""
                        CREATE TABLE IF NOT EXISTS transits (
                            transit_id      TEXT PRIMARY KEY,
                            agent_id        TEXT NOT NULL,
                            origin          TEXT NOT NULL,
                            destination     TEXT NOT NULL,
                            departure_round INTEGER NOT NULL,
                            arrival_round   INTEGER NOT NULL,
                            commodity       TEXT NOT NULL,
                            cargo_qty       INTEGER NOT NULL DEFAULT 0,
                            fuel_burned     INTEGER NOT NULL DEFAULT 0,
                            status          TEXT NOT NULL CHECK (status IN ('in_transit', 'arrived', 'cancelled')),
                            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                        )
                    """)
                if 'vessel_locations' not in tables:
                    self.conn.execute("""
                        CREATE TABLE IF NOT EXISTS vessel_locations (
                            agent_id        TEXT PRIMARY KEY,
                            station_id      TEXT NOT NULL,
                            docked_since    INTEGER NOT NULL DEFAULT 0,
                            updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                        )
                    """)

                # Migration: book_events.kind CHECK constraint pre-dated 'cancel', 'news', 'transit', 'borrow', 'distress', 'rescue', 'salvage' support
                if 'book_events' in tables:
                    row = self.conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name='book_events'"
                    ).fetchone()
                    existing_sql = row[0] if row else ''
                    if existing_sql and ("'borrow'" not in existing_sql or "'cancel'" not in existing_sql or "'news'" not in existing_sql or "'transit'" not in existing_sql or "'distress'" not in existing_sql or "'rescue'" not in existing_sql or "'salvage'" not in existing_sql):
                        self.conn.executescript("""
                            CREATE TABLE book_events_new (
                                seq         INTEGER PRIMARY KEY,
                                kind        TEXT NOT NULL CHECK (kind IN ('order','trade','floor_open','floor_close','cancel','news','transit','transit_arrived','borrow','loan_closed','liquidation','distress','rescue','salvage')),
                                payload     TEXT NOT NULL,
                                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                            );
                            INSERT INTO book_events_new SELECT * FROM book_events;
                            DROP TABLE book_events;
                            ALTER TABLE book_events_new RENAME TO book_events;
                        """)

                # Ensure default vessel locations exist for baseline fleet
                for agent in ('amos', 'marvin', 'zero'):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO vessel_locations (agent_id, station_id, docked_since) VALUES (?, 'ceres', 0)",
                        (agent,)
                    )

                # Ensure genesis fuel exists if pre-dated Phase 2
                fuel_rows = self.conn.execute("SELECT agent_id FROM accounts WHERE instrument = 'FUEL'").fetchall()
                if not fuel_rows:
                    self.conn.execute("""
                        INSERT INTO accounts (agent_id, instrument, balance) VALUES
                        ('SYSTEM', 'FUEL', -1500),
                        ('amos', 'FUEL', 500),
                        ('marvin', 'FUEL', 500),
                        ('zero', 'FUEL', 500)
                    """)
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES
                        ('genesis-fuel', 0, 'SYSTEM', 'FUEL', -1500),
                        ('genesis-fuel', 0, 'amos', 'FUEL', 500),
                        ('genesis-fuel', 0, 'marvin', 'FUEL', 500),
                        ('genesis-fuel', 0, 'zero', 'FUEL', 500)
                    """)

            # Ensure equity_loans table exists unconditionally
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS equity_loans (
                    loan_id         TEXT PRIMARY KEY,
                    borrower_id     TEXT NOT NULL,
                    lender_id       TEXT NOT NULL,
                    equity_symbol   TEXT NOT NULL,
                    shares          INTEGER NOT NULL,
                    collateral_cr   INTEGER NOT NULL,
                    fee_rate        REAL NOT NULL DEFAULT 0.02,
                    start_round     INTEGER NOT NULL,
                    status          TEXT NOT NULL CHECK (status IN ('active', 'closed', 'liquidated')),
                    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    closed_at       TEXT
                )
            """)

            # Ensure genesis syndicate equities exist unconditionally
            equity_rows = self.conn.execute("SELECT agent_id FROM accounts WHERE instrument LIKE 'EQ_%'").fetchall()
            if not equity_rows:
                for issuer_id, conf in FLEET_EQUITIES.items():
                    sym = conf["symbol"]
                    total_shares = conf["total_shares"]
                    self.conn.execute("""
                        INSERT INTO accounts (agent_id, instrument, balance) VALUES
                        ('SYSTEM', ?, ?),
                        (?, ?, ?)
                    """, (sym, -total_shares, issuer_id, sym, total_shares))
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES
                        (?, 0, 'SYSTEM', ?, ?),
                        (?, 0, ?, ?, ?)
                    """, (f"genesis-{sym.lower()}", sym, -total_shares, f"genesis-{sym.lower()}", issuer_id, sym, total_shares))

            if 'transits' in tables:
                t_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(transits)").fetchall()]
                if 'perishable' not in t_cols:
                    self.conn.execute("ALTER TABLE transits ADD COLUMN perishable INTEGER NOT NULL DEFAULT 0")
                if 'decay_rate' not in t_cols:
                    self.conn.execute("ALTER TABLE transits ADD COLUMN decay_rate REAL NOT NULL DEFAULT 0.0")
                if 'decayed_qty' not in t_cols:
                    self.conn.execute("ALTER TABLE transits ADD COLUMN decayed_qty INTEGER NOT NULL DEFAULT 0")
                if 'toll_paid' not in t_cols:
                    self.conn.execute("ALTER TABLE transits ADD COLUMN toll_paid INTEGER NOT NULL DEFAULT 0")

            if 'orders' in tables or 'accounts' not in tables:
                if not self.default_instrument:
                    row = self.conn.execute(
                        "SELECT instrument FROM accounts WHERE instrument IN ('FRAG', 'BANANA') "
                        "ORDER BY CASE instrument WHEN 'FRAG' THEN 1 WHEN 'BANANA' THEN 2 ELSE 3 END LIMIT 1"
                    ).fetchone()
                    if row:
                        self.default_instrument = row[0]
                        self.book = self.books['ceres'][row[0]]
                self._rehydrate_book()

    def _rehydrate_book(self):
        """Rehydrate resting orders from database into in-memory order book in price-time priority."""
        cur = self.conn.cursor()
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(orders)").fetchall()]
        has_station_col = 'station_id' in cols

        query = f"""
            SELECT order_id, agent_id, instrument, side, qty, limit_price, seq_seen, filled_qty
                   {', station_id' if has_station_col else ''}
            FROM orders
            WHERE status = 'open' AND (qty - filled_qty) > 0
            ORDER BY submitted_at ASC
        """
        cur.execute(query)
        for r in cur.fetchall():
            order = Order(
                order_id=r['order_id'],
                agent_id=r['agent_id'],
                instrument=r['instrument'],
                side=r['side'],
                qty=r['qty'],
                limit_price=r['limit_price'],
                seq_seen=r['seq_seen'],
                filled_qty=r['filled_qty']
            )
            st = r['station_id'] if has_station_col and r['station_id'] in self.books else 'ceres'
            inst = order.instrument
            if inst not in self.books[st]:
                self.books[st][inst] = OrderBook(instrument=inst)
            target = self.books[st][inst]
            if order.side == 'bid':
                target._insert_bid(order)
            elif order.side == 'ask':
                target._insert_ask(order)

    @property
    def current_seq(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM book_events")
        row = cur.fetchone()
        return row[0] if row else 0

    def _get_next_seq(self) -> int:
        return self.current_seq + 1

    def set_floor(self, state: str) -> str:
        with self.lock:
            state = state.lower().strip()
            if state not in ('open', 'closed'):
                raise ValueError(f"Invalid floor state '{state}', expected 'open' or 'closed'")
            self.floor = state
            return self.floor

    def record_news(self, event_dict: Dict[str, Any]) -> int:
        """Records a GalNet breaking news wire event into book_events ticks."""
        with self.lock:
            next_seq = self.current_seq + 1
            self.conn.execute(
                "INSERT INTO book_events (seq, kind, payload) VALUES (?, 'news', ?)",
                (next_seq, json.dumps(event_dict))
            )
            return next_seq

    def get_balance(self, agent_id: str, instrument: str) -> int:
        cur = self.conn.cursor()
        # Support CR, CREDITS, and CASH as currency instrument; FRAG and BANANA as commodity
        cur.execute(
            "SELECT balance FROM accounts WHERE agent_id = ? AND instrument = ?",
            (agent_id, instrument)
        )
        row = cur.fetchone()
        if not row and instrument in ('CR', 'CREDITS', 'CASH'):
            for alt in ('CR', 'CREDITS', 'CASH'):
                if alt == instrument:
                    continue
                cur.execute(
                    "SELECT balance FROM accounts WHERE agent_id = ? AND instrument = ?",
                    (agent_id, alt)
                )
                row = cur.fetchone()
                if row:
                    break
        elif not row and instrument in ('FRAG', 'BANANA'):
            alt = 'BANANA' if instrument == 'FRAG' else 'FRAG'
            cur.execute(
                "SELECT balance FROM accounts WHERE agent_id = ? AND instrument = ?",
                (agent_id, alt)
            )
            row = cur.fetchone()
        return row[0] if row else 0

    def get_currency_instrument(self, agent_id: str = 'amos') -> str:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT instrument FROM accounts WHERE agent_id = ? AND instrument IN ('CR', 'CREDITS', 'CASH') "
            "ORDER BY CASE instrument WHEN 'CR' THEN 1 WHEN 'CREDITS' THEN 2 WHEN 'CASH' THEN 3 ELSE 4 END LIMIT 1",
            (agent_id,)
        )
        row = cur.fetchone()
        return row[0] if row else 'CR'

    def get_commodity_instrument(self, agent_id: str = 'amos') -> str:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT instrument FROM accounts WHERE agent_id = ? AND instrument IN ('FRAG', 'BANANA') "
            "ORDER BY CASE instrument WHEN 'FRAG' THEN 1 WHEN 'BANANA' THEN 2 ELSE 3 END LIMIT 1",
            (agent_id,)
        )
        row = cur.fetchone()
        return row[0] if row else 'FRAG'

    def get_book_snapshot(self, station_id: Optional[str] = None, instrument: Optional[str] = None) -> Dict[str, Any]:
        if station_id:
            st = station_id.lower().strip()
            inst = (instrument or 'FRAG').upper().strip()
            if st in self.books and inst in self.books[st]:
                return self.books[st][inst].to_dict()
        elif instrument:
            inst = instrument.upper().strip()
            if 'ceres' in self.books and inst in self.books['ceres']:
                return self.books['ceres'][inst].to_dict()
        return self.book.to_dict()

    def get_vessel_location(self, agent_id: str) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT transit_id, origin, destination, departure_round, arrival_round, commodity, cargo_qty, fuel_burned
            FROM transits
            WHERE agent_id = ? AND status = 'in_transit'
            ORDER BY departure_round DESC LIMIT 1
        """, (agent_id,))
        tx = cur.fetchone()
        if tx:
            return {
                'agent_id': agent_id,
                'station_id': 'in_transit',
                'status': 'in_transit',
                'docked_since': None,
                'transit': {
                    'transit_id': tx['transit_id'],
                    'origin': tx['origin'],
                    'destination': tx['destination'],
                    'departure_round': tx['departure_round'],
                    'arrival_round': tx['arrival_round'],
                    'commodity': tx['commodity'],
                    'cargo_qty': tx['cargo_qty'],
                    'fuel_burned': tx['fuel_burned']
                }
            }

        cur.execute("SELECT station_id, docked_since FROM vessel_locations WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        if not row:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO vessel_locations (agent_id, station_id, docked_since) VALUES (?, 'ceres', 0)",
                    (agent_id,)
                )
            return {
                'agent_id': agent_id,
                'station_id': 'ceres',
                'status': 'docked',
                'docked_since': 0,
                'transit': None
            }
        return {
            'agent_id': agent_id,
            'station_id': row['station_id'],
            'status': 'docked',
            'docked_since': row['docked_since'],
            'transit': None
        }

    def get_all_vessel_locations(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT DISTINCT agent_id FROM vessel_locations
            UNION
            SELECT DISTINCT agent_id FROM accounts WHERE agent_id != 'SYSTEM'
            ORDER BY agent_id ASC
        """)
        agents = [r[0] for r in cur.fetchall()]
        return [self.get_vessel_location(a) for a in agents]

    def get_station_prices(self, station_id: Optional[str] = None, commodity: Optional[str] = None) -> Dict[str, Any]:
        all_prices = self.spatial.get_prices()
        if station_id:
            st = station_id.lower().strip()
            if commodity:
                comm = commodity.upper().strip()
                val = all_prices.get(st, {}).get(comm)
                return {'station_id': st, 'commodity': comm, 'spot_price': val, 'round': self.current_round}
            return {'station_id': st, 'prices': all_prices.get(st, {}), 'round': self.current_round}
        return {'round': self.current_round, 'prices': all_prices}

    def initiate_transit(self, agent_id: str, destination: str, commodity: str = 'FRAG', cargo_qty: int = 0, perishable: Optional[bool] = None) -> Dict[str, Any]:
        with self.lock:
            dest = destination.lower().strip()
            if dest not in STATIONS:
                return {
                    'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'invalid_station', 'detail': f"Unknown destination station '{destination}'. Valid stations: {STATIONS}"}
                }

            loc = self.get_vessel_location(agent_id)
            if loc['status'] == 'in_transit':
                return {
                    'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'already_in_transit', 'detail': f"Agent '{agent_id}' is already in transit to '{loc['transit']['destination']}'"}
                }

            origin = loc['station_id']
            if origin == dest:
                return {
                    'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'invalid_transit', 'detail': f"Vessel is already docked at '{origin}'"}
                }

            route = get_route(origin, dest, self.current_round)
            if not route:
                return {
                    'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'invalid_route', 'detail': f"No route between '{origin}' and '{dest}'"}
                }

            required_fuel = route['fuel']
            fuel_bal = self.get_balance(agent_id, 'FUEL')
            committed_fuel = sum(
                o.remaining_qty
                for st_books in self.books.values()
                for o in st_books.get('FUEL', OrderBook('FUEL')).asks
                if o.agent_id == agent_id
            )
            avail_fuel = fuel_bal - committed_fuel
            if avail_fuel < required_fuel:
                return {
                    'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'insufficient_fuel', 'detail': f"Route {origin}->{dest} requires {required_fuel} FUEL, available {avail_fuel} (balance {fuel_bal} - committed {committed_fuel})"}
                }

            comm = (commodity or 'FRAG').upper().strip()
            is_perishable = bool(perishable) if perishable is not None else (comm in PERISHABLE_COMMODITIES)
            decay_rate = route.get('decay_rate', 0.0) if is_perishable else 0.0

            toll_required = route.get('toll', 0)
            if toll_required > 0:
                cr_bal = self.get_balance(agent_id, 'CR')
                committed_cr = sum(
                    o.remaining_qty * o.limit_price
                    for st_books in self.books.values()
                    for b in st_books.values()
                    for o in b.bids
                    if o.agent_id == agent_id
                )
                avail_cr = cr_bal - committed_cr
                if avail_cr < toll_required:
                    return {
                        'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                        'payload': {'reason': 'insufficient_credits_for_toll', 'detail': f"Asteroid belt route {origin}->{dest} requires {toll_required} CR toll, available {avail_cr} CR (balance {cr_bal} - committed {committed_cr})"}
                    }

            try:
                cargo_qty = int(cargo_qty)
            except (ValueError, TypeError):
                cargo_qty = 0

            if cargo_qty < 0:
                return {
                    'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'invalid_format', 'detail': "Cargo quantity must be non-negative"}
                }

            if cargo_qty > 0:
                comm_bal = self.get_balance(agent_id, comm)
                committed_comm = sum(
                    o.remaining_qty
                    for st_books in self.books.values()
                    for o in st_books.get(comm, OrderBook(comm)).asks
                    if o.agent_id == agent_id
                )
                avail_comm = comm_bal - committed_comm
                if avail_comm < cargo_qty:
                    return {
                        'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                        'payload': {'reason': 'insufficient_cargo', 'detail': f"Required {cargo_qty} {comm}, available {avail_comm} (balance {comm_bal} - committed {committed_comm})"}
                    }

            # Cancel open resting orders for agent at origin station
            if origin in self.books:
                for b in self.books[origin].values():
                    for o in list(b.bids) + list(b.asks):
                        if o.agent_id == agent_id:
                            self.cancel_order(agent_id, o.order_id)

            transit_id = f"tx-{agent_id}-{time.time_ns()}"
            dep_round = self.current_round
            arr_round = dep_round + route['rounds']

            with self.conn:
                next_seq = self._get_next_seq()
                # 1. Fuel debit (agent -> SYSTEM)
                self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'FUEL'", (required_fuel, agent_id))
                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'SYSTEM' AND instrument = 'FUEL'", (required_fuel,))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'FUEL', ?)", (f"fuel-{transit_id}", next_seq, agent_id, -required_fuel))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'FUEL', ?)", (f"fuel-{transit_id}", next_seq, required_fuel))

                # 2. Belt toll debit (agent -> SYSTEM)
                if toll_required > 0:
                    self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'CR'", (toll_required, agent_id))
                    self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'SYSTEM' AND instrument = 'CR'", (toll_required,))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)", (f"toll-{transit_id}", next_seq, agent_id, -toll_required))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'CR', ?)", (f"toll-{transit_id}", next_seq, toll_required))

                # 3. Cargo escrow (if cargo_qty > 0)
                if cargo_qty > 0:
                    self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?", (cargo_qty, agent_id, comm))
                    self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'SYSTEM' AND instrument = ?", (cargo_qty, comm))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (f"escrow-{transit_id}", next_seq, agent_id, comm, -cargo_qty))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', ?, ?)", (f"escrow-{transit_id}", next_seq, comm, cargo_qty))

                # 4. Transits record
                self.conn.execute("""
                    INSERT INTO transits (transit_id, agent_id, origin, destination, departure_round, arrival_round, commodity, cargo_qty, fuel_burned, status, perishable, decay_rate, decayed_qty, toll_paid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_transit', ?, ?, 0, ?)
                """, (transit_id, agent_id, origin, dest, dep_round, arr_round, comm, cargo_qty, required_fuel, int(is_perishable), decay_rate, toll_required))

                # 5. Vessel locations
                self.conn.execute("""
                    INSERT INTO vessel_locations (agent_id, station_id, docked_since, updated_at)
                    VALUES (?, 'in_transit', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    ON CONFLICT(agent_id) DO UPDATE SET
                        station_id = 'in_transit',
                        docked_since = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                """, (agent_id, dep_round, dep_round))

                # 6. Book event tick
                self.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'transit', ?)", (
                    next_seq,
                    json.dumps({
                        'transit_id': transit_id,
                        'agent_id': agent_id,
                        'origin': origin,
                        'destination': dest,
                        'departure_round': dep_round,
                        'arrival_round': arr_round,
                        'rounds_duration': route['rounds'],
                        'commodity': comm,
                        'cargo_qty': cargo_qty,
                        'fuel_burned': required_fuel,
                        'is_aligned': route.get('is_aligned', False),
                        'window_name': route.get('window_name'),
                        'toll_paid': toll_required,
                        'perishable': is_perishable,
                        'decay_rate': decay_rate,
                    })
                ))

            return {
                'v': 1,
                'kind': 'status',
                'status': 'in_transit',
                'payload': {
                    'transit_id': transit_id,
                    'agent_id': agent_id,
                    'origin': origin,
                    'destination': dest,
                    'departure_round': dep_round,
                    'arrival_round': arr_round,
                    'rounds_duration': route['rounds'],
                    'commodity': comm,
                    'cargo_qty': cargo_qty,
                    'fuel_burned': required_fuel,
                    'is_aligned': route.get('is_aligned', False),
                    'window_name': route.get('window_name'),
                    'toll_paid': toll_required,
                    'perishable': is_perishable,
                    'decay_rate': decay_rate,
                }
            }

    def step_round(self, round_num: Optional[int] = None) -> Dict[str, Any]:
        with self.lock:
            new_round = round_num if round_num is not None else self.current_round + 1
            self.current_round = new_round

            # Advance prices
            spot_prices = self.spatial.step_round(new_round, galnet_engine=self.galnet)
            with self.conn:
                for p in spot_prices:
                    self.conn.execute("""
                        INSERT OR REPLACE INTO station_prices (station_id, commodity, round, base_price, drift_bias, spot_price, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    """, (p.station_id, p.commodity, p.round, p.base_price, p.drift_bias, p.spot_price))

                # Settle arriving transits
                cur = self.conn.cursor()
                cur.execute("""
                    SELECT transit_id, agent_id, origin, destination, commodity, cargo_qty, arrival_round, departure_round, perishable, decay_rate
                    FROM transits
                    WHERE status = 'in_transit' AND arrival_round <= ?
                """, (new_round,))
                arrivals = cur.fetchall()
                arrived_list = []
                for a in arrivals:
                    t_id = a['transit_id']
                    ag_id = a['agent_id']
                    dest = a['destination']
                    comm = a['commodity']
                    c_qty = a['cargo_qty']
                    is_perish = bool(a['perishable'])
                    decay_rate = a['decay_rate'] or 0.0
                    next_seq = self._get_next_seq()

                    decay_qty = 0
                    deliver_qty = c_qty
                    if c_qty > 0:
                        if is_perish and decay_rate > 0:
                            transit_rounds = max(1, a['arrival_round'] - a['departure_round'])
                            decay_qty = min(c_qty, math.floor(c_qty * decay_rate * transit_rounds))
                            deliver_qty = c_qty - decay_qty

                        if deliver_qty > 0:
                            self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (deliver_qty, ag_id, comm))
                            self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = 'SYSTEM' AND instrument = ?", (deliver_qty, comm))
                            self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (f"release-{t_id}", next_seq, ag_id, comm, deliver_qty))
                            self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', ?, ?)", (f"release-{t_id}", next_seq, comm, -deliver_qty))

                    self.conn.execute("UPDATE transits SET status = 'arrived', decayed_qty = ? WHERE transit_id = ?", (decay_qty, t_id))
                    self.conn.execute("""
                        INSERT INTO vessel_locations (agent_id, station_id, docked_since, updated_at)
                        VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                        ON CONFLICT(agent_id) DO UPDATE SET
                            station_id = ?,
                            docked_since = ?,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    """, (ag_id, dest, new_round, dest, new_round))

                    arrival_payload = {
                        'transit_id': t_id,
                        'agent_id': ag_id,
                        'origin': a['origin'],
                        'destination': dest,
                        'commodity': comm,
                        'cargo_qty': c_qty,
                        'cargo_delivered': deliver_qty,
                        'cargo_decayed': decay_qty,
                        'perishable': is_perish,
                        'decay_rate': decay_rate,
                        'arrival_round': new_round
                    }
                    self.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'transit_arrived', ?)", (next_seq, json.dumps(arrival_payload)))
                    arrived_list.append(arrival_payload)

            # Distribute bilateral borrow fees & audit maintenance margin
            borrow_fee_reports = self.equity.step_borrow_fees(new_round)

            return {
                'status': 'ok',
                'round': new_round,
                'prices': self.spatial.get_prices(),
                'arrived_transits': arrived_list,
                'borrow_fee_reports': borrow_fee_reports
            }

    def get_equity_summary(self) -> Dict[str, Any]:
        """Returns market cap, NAV, and short interest for all fleet equities."""
        return self.equity.get_equity_summary()

    def get_orbital_windows(self) -> List[Dict[str, Any]]:
        """Returns active and upcoming planetary alignment windows at current round."""
        return get_alignment_windows(self.current_round)

    def get_equity_loans(self, borrower_id: Optional[str] = None, lender_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns active equity loans."""
        return self.equity.get_active_loans(borrower_id=borrower_id, lender_id=lender_id)

    def borrow_equity(self, borrower_id: str, equity_symbol: str, shares: int, collateral_cr: Optional[int] = None, lender_id: Optional[str] = None) -> Dict[str, Any]:
        """Executes a bilateral stock loan for short-selling."""
        with self.lock:
            return self.equity.initiate_loan(
                borrower_id=borrower_id,
                equity_symbol=equity_symbol,
                shares=shares,
                collateral_cr=collateral_cr,
                lender_id=lender_id
            )

    def return_equity_loan(self, borrower_id: str, loan_id: str) -> Dict[str, Any]:
        """Returns borrowed shares to lender and unlocks escrowed collateral."""
        with self.lock:
            return self.equity.return_loan(borrower_id=borrower_id, loan_id=loan_id)

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
        """Broadcasts a distress beacon and creates a rescue RFQ."""
        with self.lock:
            return self.salvage.broadcast_distress(
                agent_id=agent_id,
                location=location,
                cargo_bounty=cargo_bounty,
                transit_id=transit_id,
                fuel_needed=fuel_needed,
                max_reward_cr=max_reward_cr,
                reason=reason
            )

    def submit_rescue_quote(
        self,
        rescuer_id: str,
        rfq_id: str,
        fuel_offered: int,
        price_cr: int
    ) -> Dict[str, Any]:
        """Submits a competitive rescue quote offering propellant."""
        with self.lock:
            return self.salvage.submit_rescue_quote(
                rescuer_id=rescuer_id,
                rfq_id=rfq_id,
                fuel_offered=fuel_offered,
                price_cr=price_cr
            )

    def accept_rescue_quote(
        self,
        agent_id: str,
        quote_id: str
    ) -> Dict[str, Any]:
        """Accepts a rescue quote and atomically settles fuel and credits on the ledger."""
        with self.lock:
            return self.salvage.accept_rescue_quote(
                agent_id=agent_id,
                quote_id=quote_id
            )

    def claim_salvage(
        self,
        salvager_id: str,
        beacon_id: str
    ) -> Dict[str, Any]:
        """Claims derelict cargo bounty on an unrescued distress beacon."""
        with self.lock:
            return self.salvage.claim_salvage(
                salvager_id=salvager_id,
                beacon_id=beacon_id
            )

    def get_distress_beacons(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns active or filtered distress beacons."""
        return self.salvage.get_beacons(status=status)

    def get_rescue_rfqs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns open rescue RFQs and quotes."""
        return self.salvage.get_rfqs(status=status)

    def get_salvage_summary(self) -> Dict[str, Any]:
        """Returns high-level salvage and rescue statistics."""
        return self.salvage.get_salvage_summary()

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

        # 0. Floor state audit (halted / closed)
        if self.floor != 'open':
            return self._reject_envelope(
                order_id=order_id or 'unknown',
                agent_id=agent_id or 'unknown',
                reason='market_halted',
                detail=f"Trading floor is currently {self.floor}. Order submissions rejected."
            )

        instrument = payload.get('instrument')
        side = payload.get('side')
        qty = payload.get('qty')
        limit_price = payload.get('limit_price')
        seq_seen = payload.get('seq_seen', 0)

        # 1. Format validation
        if not all([order_id, agent_id, instrument, side, qty is not None, limit_price is not None]):
            return self._reject_envelope(order_id or 'unknown', agent_id or 'unknown', 'invalid_format', 'Missing required order fields')

        if instrument not in ('FRAG', 'BANANA', 'FUEL') and instrument not in EQUITY_SYMBOLS:
            return self._reject_envelope(order_id or 'unknown', agent_id or 'unknown', 'invalid_format', f"Unsupported instrument: {instrument}")

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

        # 2b. Spatial Locality & Docking Audit
        vessel = self.get_vessel_location(agent_id)
        if vessel['status'] == 'in_transit':
            dest_station = vessel.get('transit', {}).get('destination', 'destination')
            return self._reject_envelope(
                order_id, agent_id, 'vessel_in_transit',
                f"Agent '{agent_id}' is currently in transit to '{dest_station}' and cannot place orders until docked."
            )

        docked_station = vessel['station_id']
        order_station = payload.get('station_id')
        if order_station:
            order_station = order_station.lower().strip()
            if order_station not in STATIONS:
                return self._reject_envelope(
                    order_id, agent_id, 'invalid_station',
                    f"Unknown station '{order_station}'. Valid stations: {STATIONS}"
                )
            if order_station != docked_station:
                return self._reject_envelope(
                    order_id, agent_id, 'vessel_not_docked',
                    f"Agent '{agent_id}' is docked at '{docked_station}', cannot place orders at '{order_station}'. Local trading only."
                )
        else:
            order_station = docked_station

        if order_station not in self.books:
            self.books[order_station] = {}
        if instrument not in self.books[order_station]:
            self.books[order_station][instrument] = OrderBook(instrument=instrument)
        target_book = self.books[order_station][instrument]

        # 3. Solvency & Committed Exposure Audit (No negative balances for non-SYSTEM agents)
        currency = self.get_currency_instrument(agent_id)
        if side == 'bid':
            max_cost = qty * limit_price
            committed_funds = sum(
                o.remaining_qty * o.limit_price
                for st_books in self.books.values()
                for b in st_books.values()
                for o in b.bids
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
            committed_commodity = sum(
                o.remaining_qty
                for st_books in self.books.values()
                for b in st_books.values()
                for o in b.asks
                if o.agent_id == agent_id and o.instrument == instrument
            )
            seller_balance = self.get_balance(agent_id, instrument)
            available_commodity = seller_balance - committed_commodity
            if available_commodity < qty:
                return self._reject_envelope(
                    order_id, agent_id, 'insufficient_balance',
                    f"Account '{agent_id}' available {instrument} balance {available_commodity} "
                    f"(balance {seller_balance} - committed {committed_commodity}) insufficient for ask requirement {qty}"
                )

        # 3b. Currency Compatibility Audit for Crossing Orders (Pre-matching validation)
        # Mirrors OrderBook.add_order walk: only check resting orders that would actually be consumed.
        needed_qty = qty
        if side == 'bid':
            for ask in target_book.asks:
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
            for bid in target_book.bids:
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

        book_snapshot = copy.deepcopy(target_book)
        try:
            with self.conn:
                next_seq = self.current_seq + 1

                # Match against target book
                trades, resting = target_book.add_order(order, current_seq=next_seq)

                # Record submission in orders table
                status = 'filled' if order.is_filled else 'open'
                self.conn.execute("""
                    INSERT INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, resolved_seq, filled_qty, station_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, next_seq if order.is_filled else None, order.filled_qty, order_station))

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
                    commodity_inst = trade.instrument

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

                    # Commodity (FRAG / BANANA / FUEL) deltas
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta)
                        VALUES (?, ?, ?, ?, ?)
                    """, (txn_id, next_seq, trade.buyer_id, commodity_inst, trade.qty))
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta)
                        VALUES (?, ?, ?, ?, ?)
                    """, (txn_id, next_seq, trade.seller_id, commodity_inst, -trade.qty))

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

                    self.conn.execute(
                        "INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)",
                        (trade.buyer_id, commodity_inst)
                    )
                    cur = self.conn.execute(
                        "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                        (trade.qty, trade.buyer_id, commodity_inst)
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Failed to credit {trade.buyer_id} {commodity_inst}: rowcount {cur.rowcount} != 1")

                    cur = self.conn.execute(
                        "UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?",
                        (trade.qty, trade.seller_id, commodity_inst)
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Failed to debit {trade.seller_id} {commodity_inst}: rowcount {cur.rowcount} != 1")

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
                        'cost': cost,
                        'station_id': order_station
                    })))

                    # Update maker resting order in orders table
                    maker_order_id = trade.ask_order_id if order.side == 'bid' else trade.bid_order_id
                    maker_agent_id = trade.seller_id if order.side == 'bid' else trade.buyer_id
                    self.conn.execute("""
                        UPDATE orders
                        SET filled_qty = filled_qty + ?,
                            status = CASE WHEN filled_qty + ? >= qty THEN 'filled' ELSE status END,
                            resolved_seq = CASE WHEN filled_qty + ? >= qty THEN ? ELSE resolved_seq END
                        WHERE order_id = ? AND agent_id = ?
                    """, (trade.qty, trade.qty, trade.qty, next_seq, maker_order_id, maker_agent_id))
        except Exception:
            self.books[order_station][instrument] = book_snapshot
            if order_station == 'ceres' and instrument == getattr(self, 'default_instrument', 'FRAG'):
                self.book = book_snapshot
            raise

        # 5. Emit Market Discovery Broadcast (kind: market_tick)
        return {
            'v': 1,
            'kind': 'market_tick',
            'reply': 'optional',
            'floor': self.floor,
            'scope': 'channel',
            'subject': 'agent-collaborative-project',
            'payload': {
                'seq': self.current_seq,
                'station_id': order_station,
                'instrument': instrument,
                'best_bid': target_book.best_bid(),
                'best_ask': target_book.best_ask(),
                'last_price': self.last_price,
                'last_qty': self.last_qty,
                'status': self.floor,
                'trades_count': len(trades)
            }
        }

    def cancel_order(self, agent_id: str, order_id: str) -> Dict[str, Any]:
        """
        Cancel one resting order across all station order books.
        """
        with self.lock:
            removed = None
            for st_books in self.books.values():
                for b in st_books.values():
                    removed = b.remove_order(order_id, agent_id)
                    if removed is not None:
                        break
                if removed is not None:
                    break

            if removed is None:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT status FROM orders WHERE agent_id = ? AND order_id = ?",
                    (agent_id, order_id)
                )
                row = cur.fetchone()
                if row is None:
                    detail = f"No order '{order_id}' found for agent '{agent_id}'"
                else:
                    detail = f"Order '{order_id}' is already '{row['status']}', not resting"
                return self._reject_envelope(order_id, agent_id, 'order_not_cancellable', detail)

            next_seq = self.current_seq + 1
            with self.conn:
                self.conn.execute(
                    "UPDATE orders SET status = 'cancelled' WHERE agent_id = ? AND order_id = ?",
                    (agent_id, order_id)
                )
                self.conn.execute(
                    "INSERT INTO book_events (seq, kind, payload) VALUES (?, 'cancel', ?)",
                    (next_seq, json.dumps({
                        'order_id': order_id,
                        'agent_id': agent_id,
                        'side': removed.side,
                        'remaining_qty': removed.remaining_qty,
                    }))
                )

            return {
                'v': 1,
                'kind': 'status',
                'reply': 'none',
                'status': 'cancelled',
                'floor': self.floor,
                'payload': {
                    'order_id': order_id,
                    'agent_id': agent_id,
                    'seq': self.current_seq,
                    'released_qty': removed.remaining_qty,
                }
            }

    def cancel_all(self, agent_id: str) -> Dict[str, Any]:
        """Cancel every resting order across all stations for one agent."""
        with self.lock:
            targets = []
            for st_books in self.books.values():
                for b in st_books.values():
                    for o in list(b.bids) + list(b.asks):
                        if o.agent_id == agent_id and o.order_id not in targets:
                            targets.append(o.order_id)

        cancelled = []
        for order_id in targets:
            result = self.cancel_order(agent_id, order_id)
            if result.get('status') == 'cancelled':
                cancelled.append(order_id)

        return {
            'v': 1,
            'kind': 'status',
            'reply': 'none',
            'status': 'cancelled_all',
            'floor': self.floor,
            'payload': {
                'agent_id': agent_id,
                'seq': self.current_seq,
                'cancelled_order_ids': cancelled,
                'count': len(cancelled),
            }
        }

    def _reject_envelope(self, order_id: str, agent_id: str, reason: str, detail: str) -> Dict[str, Any]:
        return {
            'v': 1,
            'kind': 'reject',
            'reply': 'optional',
            'floor': self.floor,
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
        Calculate Net Worth = Balance(Credits/CR) + Qty(FRAG) * Mark Price.
        """
        mark = self.last_price if self.last_price is not None else 10  # default mark if no trades
        cur = self.conn.cursor()
        cur.execute("""
            SELECT agent_id,
                   SUM(CASE WHEN instrument IN ('CR', 'CREDITS', 'CASH') THEN balance ELSE 0 END) as liquid,
                   SUM(CASE WHEN instrument IN ('FRAG', 'BANANA') THEN balance ELSE 0 END) as frags,
                   SUM(CASE WHEN instrument = 'FUEL' THEN balance ELSE 0 END) as fuel
            FROM accounts
            WHERE agent_id != 'SYSTEM'
            GROUP BY agent_id
        """)
        rows = cur.fetchall()
        board = []
        for r in rows:
            net_worth = r['liquid'] + (r['frags'] * mark)
            board.append({
                'agent_id': r['agent_id'],
                'net_worth': net_worth,
                'liquid': r['liquid'],
                'frags': r['frags'],
                'fuel': r['fuel'],
                'bananas': r['frags'],  # backward compatibility alias
                'mark_price': mark
            })
        board.sort(key=lambda x: x['net_worth'], reverse=True)
        return board