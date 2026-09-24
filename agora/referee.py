"""
agora.referee - Central referee for order validation, solvency auditing,
book matching, and atomic double-entry ledger settlement.
"""

import os
import sqlite3
import json
import time
import copy
import math
import random
import threading
from typing import Optional, Dict, Any, Tuple, List
from pathlib import Path

from agora.order_book import OrderBook, Order, Trade
from agora.galnet import GalNetEngine
from agora.peer import PeerDesk, env_peer_trades
from agora.contracts import ContractDesk, env_contracts
from agora.corporate import CorporateDesk, env_corporate
from agora.upgrades import UpgradeDesk, env_upgrades
from agora.events import EventDesk, env_events
from agora.covert import CovertDesk
from agora.fog import FogEngine, env_fog, parse_fog
from agora.order_flow import OrderFlowDesk, env_order_flow
from agora.fleet import FleetDesk, GOODS, corp_of, is_ship_account, VESSEL_LOCATIONS_VIEW
from agora.lobbying import LobbyingDesk

STOCK_EXCHANGE_STATION = 'ceres'  # the one book every fleet stock trades on

from agora.spatial import (
    StationPriceEngine, STATIONS, COMMODITIES, BASE_PRICES, get_route, ROUTES,
    get_alignment_windows, PERISHABLE_COMMODITIES
)
from agora.equity import (
    SyndicateEquityEngine, FLEET_EQUITIES, EQUITY_SYMBOLS,
    AGENT_BY_SYMBOL, DEFAULT_BORROW_FEE_RATE
)
from agora.salvage import DerelictSalvageEngine
from agora.circuit_breaker import CircuitBreakerEngine, DEFAULT_BAND_PCT
from agora.history import PriceHistoryEngine
from agora.hazards import HazardEngine, env_hazards, parse_hazards
from agora.standing import StandingDesk, env_standing, TABLES as STANDING_TABLES
from agora.piracy import PiracyDesk, env_piracy, parse_piracy, ESCORT_PCT as PIRACY_ESCORT_PCT
from agora.piracy import cargo_value as piracy_cargo_value
from agora.exchange import EquityExchange, EXCHANGE_ID, clamp_shares, DEFAULT_VOL


ASYMMETRIC_SPAWN_LOCATIONS: Dict[str, str] = {
    'zero': 'earth',
    'amos': 'ceres',
    'marvin': 'mars',
    'aerial': 'luna',
}


# Reactive depot model (AGORA_DEPOT_MODEL=reactive, or depot_model= on the
# constructor / new_game / reset). Tuned in tools/economy_sim.py, 2026-09-22:
# finite shelves restocked by a per-round production drip, a finite buy-side
# hold drained by consumption, and quotes skewed by both.
DEPOT_MODELS = ("static", "reactive")
REACTIVE_TARGET = 2000        # shelf capacity and hold capacity, units
REACTIVE_MAIN_DRIP = 100      # per-round restock (cheapest station) / consumption (dearest station)
REACTIVE_SIDE_DRIP = 20       # per-round restock / consumption everywhere else
REACTIVE_SKEW = 0.5           # price elasticity to hold fill (depot bid)
REACTIVE_SHELF_SKEW = 0.20    # price elasticity to shelf depletion (depot ask; damped per #188)


# Static depot model: (bid, ask) offsets from round(base price) for the
# #71 spec quotes at Earth and Ceres. Every other quote is spot +/- 1.
STATIC_SPEC_OFFSETS = {
    ('earth', 'FRAG'): (0, 1), ('earth', 'FUEL'): (0, 1), ('earth', 'FOOD'): (0, 1), ('earth', 'ORE'): (-1, 1),
    ('ceres', 'FRAG'): (-1, 0), ('ceres', 'FUEL'): (-1, 0), ('ceres', 'FOOD'): (-1, 1), ('ceres', 'ORE'): (0, 1),
}


def _env_depot_model() -> str:
    m = os.environ.get("AGORA_DEPOT_MODEL", "static").strip().lower()
    return m if m in DEPOT_MODELS else "static"


def _env_band_pct() -> float:
    try:
        v = float(os.environ.get("AGORA_BAND_PCT", ""))
        return v if 0 < v < 1 else DEFAULT_BAND_PCT
    except ValueError:
        return DEFAULT_BAND_PCT


def _env_shelf_skew() -> float:
    try:
        v = float(os.environ.get("AGORA_SHELF_SKEW", ""))
        return v if 0 < v < 1 else REACTIVE_SHELF_SKEW
    except ValueError:
        return REACTIVE_SHELF_SKEW


class AgoraReferee:
    def __init__(
        self,
        db_path: str = ':memory:',
        instrument: Optional[str] = None,
        galnet: Optional[GalNetEngine] = None,
        spatial: Optional[StationPriceEngine] = None,
        depots: bool = False,
        asymmetric: bool = False,
        depot_model: Optional[str] = None,
        band_pct: Optional[float] = None,
        reactive_bands: bool = True,
        shelf_skew: Optional[float] = None,
        peer_trades: Optional[bool] = None,
        fog: Any = None,
        idle_fee: Optional[int] = None,
        rival_shares: Optional[int] = None,
        exchange_shares: Optional[int] = None,
        exchange_vol: Optional[float] = None,
        contracts: Optional[bool] = None,
        hazards: Any = None,
        corporate: Optional[bool] = None,
        upgrades: Optional[bool] = None,
        piracy: Any = None,
        events: Optional[bool] = None,
        order_flow: Optional[bool] = None,
        standing: Optional[bool] = None,
        ship_hold: Optional[int] = None,
    ):
        self.db_path = db_path
        # Cargo units each ship's hold carries (agora/fleet.py SHIP_HOLD; the
        # live server sets it). 0 = no limit, the conservative default for tests.
        self.ship_hold = max(0, int(ship_hold)) if ship_hold is not None else 0
        # Debt, distress sales, bankruptcy and takeovers (agora/corporate.py).
        self.corporate_enabled = env_corporate() if corporate is None else bool(corporate)
        # Ship upgrades that cut hazard / piracy odds (agora/upgrades.py).
        self.upgrades_enabled = env_upgrades() if upgrades is None else bool(upgrades)
        # Secrecy and exposure: private/secret corp events, leaks, scandals (agora/events.py).
        self.events_enabled = env_events() if events is None else bool(events)
        # Owned, tradable station contracts with a claim deposit (agora/contracts.py).
        self.contracts_enabled = env_contracts() if contracts is None else bool(contracts)
        self._hazard_odds = env_hazards() if hazards is None else parse_hazards(hazards)
        # Raids on the space lanes (agora/piracy.py): (belt, inner) odds or None = off.
        self._piracy_odds = env_piracy() if piracy is None else parse_piracy(piracy)
        # The stock exchange's market maker (agora/exchange.py): shares of
        # each fleet it takes from treasury at genesis. 0 = no exchange quotes.
        self.exchange_shares = clamp_shares(exchange_shares) if exchange_shares is not None else 0
        self.exchange = EquityExchange(self, vol=DEFAULT_VOL if exchange_vol is None else exchange_vol)
        # Shares of each rival's stock every fleet starts with (0 = issuers
        # hold all their own stock, the old behaviour).
        self.rival_shares = max(0, int(rival_shares)) if rival_shares is not None else 0
        # CR charged each round to a docked fleet that did nothing that round
        # (no order, cancel, trip or peer offer/accept). 0 = off.
        self.idle_fee = max(0, int(idle_fee)) if idle_fee is not None else 0
        self._active_this_round: set = set()
        _fog = parse_fog(fog) if fog is not None else env_fog()
        self.fog: Optional[FogEngine] = FogEngine(*_fog) if _fog else None
        # Fleet-to-fleet goods trades agreed at a distance (agora/peer.py).
        self.peer_trades = env_peer_trades() if peer_trades is None else bool(peer_trades)
        self.depot_model = depot_model if depot_model in DEPOT_MODELS else _env_depot_model()
        self.band_pct = band_pct if band_pct is not None else _env_band_pct()
        self.shelf_skew = shelf_skew if shelf_skew is not None else _env_shelf_skew()
        # Keep reactive quotes inside the circuit-breaker band (True), or let
        # them float freely and trip halts (False).
        self.reactive_bands = reactive_bands
        self._reactive: Dict[str, Any] = {}
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # One connection, shared by every request thread, the ticker and the
        # websocket streams, so EVERY use of it -- reads included -- must hold
        # this lock (#197). Reentrant, so cheap self-locking helpers
        # (current_seq, fleet_out, get_vessel_location) can be called both
        # from outside and from inside a locked writer.
        self.lock = threading.RLock()
        self.default_instrument = instrument or 'FRAG'
        self.galnet = galnet or GalNetEngine()
        self.spatial = spatial or StationPriceEngine()
        self.depots_enabled = depots
        self.asymmetric_enabled = asymmetric
        self.current_round: int = 0
        self.last_price: Optional[int] = None
        self.last_qty: Optional[int] = None
        self.last_prices: Dict[Tuple[str, str], int] = {}
        self.last_quantities: Dict[Tuple[str, str], int] = {}
        self.floor: str = 'open'
        # Multi-station order books across Sol nodes, supporting commodities and equities
        self.books: Dict[str, Dict[str, OrderBook]] = {
            st: {comm: OrderBook(instrument=comm) for comm in (*COMMODITIES, 'BANANA', *EQUITY_SYMBOLS)}
            for st in STATIONS
        }
        self.book = self.books['ceres'][self.default_instrument if self.default_instrument in self.books['ceres'] else 'FRAG']
        # Ships (agora/fleet.py, #175). Before _init_db: rehydrating resting
        # orders needs it to know which agents' goods live on ships.
        self.fleet = FleetDesk(self)
        self._init_db()
        self.peer = PeerDesk(self)
        self.contract_desk = ContractDesk(self)
        # Owns corp_events, so it comes before CorporateDesk, which logs to it.
        self.events = EventDesk(self)
        self.corporate = CorporateDesk(self)
        self.covert = CovertDesk(self)
        self.upgrades = UpgradeDesk(self)
        # Earned institutional standing by income lane (agora/standing.py, #187 track 2).
        self.standing = StandingDesk(self, env_standing() if standing is None else standing)
        self.hazards = HazardEngine(self.conn, self._hazard_odds)
        self.piracy = PiracyDesk(self, self._piracy_odds)
        # NPC buyers and sellers at each station that fill fleet quotes before the depot (agora/order_flow.py).
        self.order_flow = OrderFlowDesk(self, env_order_flow() if order_flow is None else order_flow)
        self.equity = SyndicateEquityEngine(self.conn, self)
        self.salvage = DerelictSalvageEngine(self.conn, self)
        self.circuit_breaker = CircuitBreakerEngine(self.conn, self, band_pct=self.band_pct)
        self.lobbying = LobbyingDesk(self)
        self.history_engine = PriceHistoryEngine(self.conn)
        self._migrate_rng_bags()
        # A database from before the hold limit (or one started with a bigger
        # hold): unload what docked ships hold over capacity into their
        # station holds. Idempotent; flying ships are unloaded as they land.
        self._unload_overflow_all()
        if self.depots_enabled:
            self.seed_depots()
        if asymmetric:
            self.set_asymmetric_roster()
        if self.fog:
            self.fog.record(self)
        # Quote stocks from boot. Otherwise a freshly deployed server shows no
        # stock bid/ask until its first round, and the live ticker can sit at
        # round 0 (seen on Railway 2026-09-23 after #137 deployed).
        if self.exchange_shares:
            self._start_exchange(seed=0)
        if hasattr(self, 'spatial') and self.spatial:
            stock_marks = self._stock_marks()
            self.history_engine.record_genesis(self.spatial.get_prices(), stock_marks=stock_marks)

    def set_asymmetric_roster(self, spawn_map: Optional[Dict[str, str]] = None) -> None:
        """Update fleet_roster with asymmetric home stations and sync vessel locations."""
        mapping = spawn_map or ASYMMETRIC_SPAWN_LOCATIONS
        with self.lock, self.conn:
            for agent_id, home_station in mapping.items():
                self.conn.execute(
                    "UPDATE fleet_roster SET home_station = ? WHERE agent_id = ?",
                    (home_station, agent_id)
                )
            # Ship 1 of each fleet starts at its home. This runs on every
            # boot of an asymmetric server, so it only places ships that have
            # never moved: it used to reset every fleet's position to home,
            # teleporting a ship mid-trip on each restart (found by the
            # #175 vessel invariant on a database carried across the upgrade).
            self.conn.execute("""
                INSERT OR IGNORE INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status)
                SELECT agent_id || '/1', agent_id, agent_id || ' Ship 1', home_station, 0, 'docked'
                FROM fleet_roster
                WHERE agent_id NOT LIKE 'depot_%' AND agent_id != 'SYSTEM'
            """)
            self.conn.execute("""
                UPDATE vessels SET station_id = (SELECT home_station FROM fleet_roster f WHERE f.agent_id = vessels.agent_id)
                WHERE vessel_id = agent_id || '/1' AND status = 'docked' AND docked_since = 0
                  AND agent_id IN (SELECT agent_id FROM fleet_roster)
                  AND NOT EXISTS (SELECT 1 FROM transits t WHERE t.vessel_id = vessels.vessel_id)
            """)

    def get_last_price(self, station_id: str, instrument: str) -> Optional[int]:
        """Return the most recent trade price for a specific station and instrument."""
        return self.last_prices.get((station_id.lower(), instrument.upper()))

    def get_last_qty(self, station_id: str, instrument: str) -> Optional[int]:
        """Return the most recent trade quantity for a specific station and instrument."""
        return self.last_quantities.get((station_id.lower(), instrument.upper()))

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
                # seed.sql only populates fleet_roster; turn that into real
                # accounts/ledger_entries/vessel_locations rows.
                self._seed_genesis_from_roster()
            else:
                # Migration: back-fill fleet_roster on a database that pre-dates
                # it, from whatever accounts/vessel_locations already exist, so
                # an upgrade never loses a fleet that was already seeded the old
                # (hardcoded) way.
                if 'fleet_roster' not in tables:
                    self.conn.execute("""
                        CREATE TABLE IF NOT EXISTS fleet_roster (
                            agent_id        TEXT PRIMARY KEY CHECK (agent_id NOT LIKE '%/%'),
                            display_name    TEXT NOT NULL,
                            home_station    TEXT NOT NULL DEFAULT 'ceres',
                            genesis_cr      INTEGER NOT NULL,
                            genesis_frag    INTEGER NOT NULL,
                            genesis_fuel    INTEGER NOT NULL,
                            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                        )
                    """)
                    existing_agents = [r[0] for r in self.conn.execute(
                        "SELECT DISTINCT agent_id FROM accounts WHERE agent_id != 'SYSTEM'"
                    ).fetchall()]
                    for agent_id in existing_agents:
                        cr = self.conn.execute(
                            "SELECT balance FROM accounts WHERE agent_id = ? AND instrument = 'CR'", (agent_id,)
                        ).fetchone()
                        frag = self.conn.execute(
                            "SELECT balance FROM accounts WHERE agent_id = ? AND instrument = 'FRAG'", (agent_id,)
                        ).fetchone()
                        fuel = self.conn.execute(
                            "SELECT balance FROM accounts WHERE agent_id = ? AND instrument = 'FUEL'", (agent_id,)
                        ).fetchone()
                        station_row = self.conn.execute(
                            "SELECT station_id FROM vessel_locations WHERE agent_id = ?", (agent_id,)
                        ).fetchone()
                        self.conn.execute("""
                            INSERT OR IGNORE INTO fleet_roster
                                (agent_id, display_name, home_station, genesis_cr, genesis_frag, genesis_fuel)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (
                            agent_id,
                            FLEET_EQUITIES.get(agent_id, {}).get('name', agent_id),
                            station_row[0] if station_row else 'ceres',
                            cr[0] if cr else 0,
                            frag[0] if frag else 0,
                            fuel[0] if fuel else 0,
                        ))
                # Migration check: ensure filled_qty and station_id columns exist in orders table
                if 'orders' in tables:
                    cols = [r[1] for r in self.conn.execute("PRAGMA table_info(orders)").fetchall()]
                    if 'filled_qty' not in cols:
                        self.conn.execute("ALTER TABLE orders ADD COLUMN filled_qty INTEGER NOT NULL DEFAULT 0")
                    if 'station_id' not in cols:
                        self.conn.execute("ALTER TABLE orders ADD COLUMN station_id TEXT NOT NULL DEFAULT 'ceres'")
                    if 'vessel_id' not in cols:
                        self.conn.execute("ALTER TABLE orders ADD COLUMN vessel_id TEXT")

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
                            vessel_id       TEXT,
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
                if 'vessels' not in tables:
                    self.conn.execute("""
                        CREATE TABLE IF NOT EXISTS vessels (
                            vessel_id       TEXT PRIMARY KEY,
                            agent_id        TEXT NOT NULL,
                            name            TEXT NOT NULL,
                            station_id      TEXT NOT NULL,
                            docked_since    INTEGER NOT NULL DEFAULT 0,
                            bought_round    INTEGER NOT NULL DEFAULT 0,
                            cost            INTEGER NOT NULL DEFAULT 0,
                            status          TEXT NOT NULL DEFAULT 'docked',
                            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                        )
                    """)
                # vessel_locations was a table until #175 PR 2; it is now a
                # view of ship 1's `vessels` row. A pre-PR-2 database copies
                # anything only the old table knew into `vessels` first.
                self._migrate_vessel_locations_view(tables)

                # Migration: book_events.kind CHECK constraint pre-dated 'cancel', 'news', 'transit', 'borrow', 'distress', 'rescue', 'salvage', 'burst' support
                if 'book_events' in tables:
                    row = self.conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name='book_events'"
                    ).fetchone()
                    existing_sql = row[0] if row else ''
                    if existing_sql and ("'borrow'" not in existing_sql or "'cancel'" not in existing_sql or "'news'" not in existing_sql or "'transit'" not in existing_sql or "'distress'" not in existing_sql or "'rescue'" not in existing_sql or "'salvage'" not in existing_sql or "'circuit_breaker_halt'" not in existing_sql or "'burst'" not in existing_sql):
                        self.conn.executescript("""
                            CREATE TABLE book_events_new (
                                seq         INTEGER PRIMARY KEY,
                                kind        TEXT NOT NULL CHECK (kind IN ('order','trade','floor_open','floor_close','cancel','news','transit','transit_arrived','borrow','loan_closed','liquidation','distress','rescue','salvage','circuit_breaker_halt','circuit_breaker_reopen','burst')),
                                payload     TEXT NOT NULL,
                                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                            );
                            INSERT INTO book_events_new SELECT * FROM book_events;
                            DROP TABLE book_events;
                            ALTER TABLE book_events_new RENAME TO book_events;
                        """)

                # Ensure default vessels exist for baseline fleet
                self.conn.execute("""
                    INSERT OR IGNORE INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status)
                    SELECT agent_id || '/1', agent_id, agent_id || ' Ship 1', home_station, 0, 'docked'
                    FROM fleet_roster
                    WHERE agent_id NOT LIKE 'depot_%' AND agent_id != 'SYSTEM'
                """)

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

            # Ensure ticker_state table exists unconditionally (durable
            # desired-state for the background TickerEngine, Issue #63:
            # survives a container restart so a Railway bounce doesn't
            # silently leave the world clock paused in the dark).
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS ticker_state (
                    id                  INTEGER PRIMARY KEY CHECK (id = 1),
                    desired_state       TEXT NOT NULL DEFAULT 'stopped' CHECK (desired_state IN ('running', 'paused', 'stopped')),
                    quiet_round_count   INTEGER NOT NULL DEFAULT 0,
                    last_tick_at        TEXT,
                    lease_owner         TEXT,
                    lease_expires_at    TEXT,
                    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
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
                self._seed_genesis_equities()

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
                if 'vessel_id' not in t_cols:
                    self.conn.execute("ALTER TABLE transits ADD COLUMN vessel_id TEXT")
                    self.conn.execute("UPDATE transits SET vessel_id = agent_id || '/1' WHERE vessel_id IS NULL")

            # #175 PR 2: a corp's goods and FUEL live on its ships' accounts.
            # Moves whatever a pre-PR-2 database still holds on '<corp>'.
            self._migrate_goods_to_ships()

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

    def _migrate_vessel_locations_view(self, tables: List[str]) -> None:
        """Turn a pre-#175-PR-2 vessel_locations table into the view in
        db/schema.sql. Caller is inside the _init_db transaction."""
        kind = self.conn.execute("SELECT type FROM sqlite_master WHERE name = 'vessel_locations'").fetchone()
        if kind and kind[0] == 'view':
            return
        if kind and kind[0] == 'table':
            # Ship 1 of every fleet the old table knew, where the old table
            # says it is (it was written alongside `vessels` since PR 1, so
            # this only matters for rows PR 1 never saw). Depot rows are not
            # ships and are dropped.
            self.conn.execute("""
                INSERT OR IGNORE INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status)
                SELECT agent_id || '/1', agent_id, agent_id || ' Ship 1', station_id, docked_since,
                       CASE WHEN station_id = 'in_transit' THEN 'in_transit' ELSE 'docked' END
                FROM vessel_locations WHERE agent_id NOT LIKE 'depot_%' AND agent_id != 'SYSTEM'
            """)
            self.conn.execute("DROP TABLE vessel_locations")
        for stmt in VESSEL_LOCATIONS_VIEW:
            self.conn.execute(stmt)

    def _migrate_goods_to_ships(self) -> None:
        """Move goods and FUEL a roster corp holds on its own account onto
        its ship 1 ('<corp>/1'), one balanced transaction per corp. Idempotent:
        a corp with nothing left on '<corp>' is skipped. Caller is inside the
        _init_db transaction."""
        corps = [r[0] for r in self.conn.execute("SELECT agent_id FROM fleet_roster")]
        marks = ','.join('?' for _ in GOODS)
        seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM book_events").fetchone()[0]
        for corp in corps:
            rows = self.conn.execute(
                f"SELECT instrument, balance FROM accounts WHERE agent_id = ? AND instrument IN ({marks}) AND balance != 0",
                (corp, *sorted(GOODS))).fetchall()
            if not rows:
                continue
            ship = f"{corp}/1"
            self.conn.execute(
                "INSERT OR IGNORE INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status) "
                "SELECT ?, agent_id, agent_id || ' Ship 1', home_station, 0, 'docked' FROM fleet_roster WHERE agent_id = ?",
                (ship, corp))
            txn = f"ship-migrate-{corp}-{seq}"
            for r in rows:
                for acct, d in ((corp, -r['balance']), (ship, r['balance'])):
                    self.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)",
                                      (acct, r['instrument']))
                    self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                                      (d, acct, r['instrument']))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                                      (txn, seq, acct, r['instrument'], d))

    def _migrate_rng_bags(self) -> None:
        """#175: a trip's luck is its ship's. Bags a pre-ships database keyed
        by corp move to that corp's ship 1 (hazard delay/loss, fight escape).
        Raid luck used to be one credit per corp; raids now draw from bags per
        ship, protection level and trip conditions (PiracyDesk.raid_key), so
        the old credits are dropped, as are sabotage-trace bags keyed by the
        saboteur alone (now per saboteur and target ship). Idempotent."""
        with self.lock, self.conn:
            if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'rng_bags'").fetchone():
                return
            roster = "(SELECT agent_id FROM fleet_roster)"
            for ns, events in (('hazards', ('delay', 'loss')), ('piracy', ('escape',))):
                marks = ','.join('?' for _ in events)
                self.conn.execute(f"UPDATE OR IGNORE rng_bags SET fleet = fleet || '/1' "
                                  f"WHERE ns = ? AND event IN ({marks}) AND fleet IN {roster}", (ns, *events))
                self.conn.execute(f"DELETE FROM rng_bags WHERE ns = ? AND event IN ({marks}) AND fleet IN {roster}",
                                  (ns, *events))
            self.conn.execute("DELETE FROM rng_bags WHERE ns = 'piracy' AND event = 'raid' AND fleet NOT LIKE '%|%'")
            self.conn.execute(f"DELETE FROM rng_bags WHERE ns = 'covert' AND event = 'sabotage_trace' AND fleet IN {roster}")

    def _unload_overflow_all(self) -> None:
        """Every docked ship over its hold capacity unloads the excess into its
        corp's hold at that station (agora/fleet.py). A no-op without a limit."""
        if not self.ship_hold:
            return
        with self.lock, self.conn:
            for (vid,) in self.conn.execute("SELECT vessel_id FROM vessels WHERE status = 'docked'").fetchall():
                self.fleet.unload_overflow_locked(vid)

    def _seed_genesis_from_roster(self) -> None:
        """
        Turn fleet_roster rows into real accounts/ledger_entries/vessel_locations
        rows at seq 0, SYSTEM treasury included, so the conservation invariant
        holds from genesis. This is the only place that mints CR/FRAG/FUEL for a
        fleet -- fleet_roster is the single source of truth for who exists and
        what they start with; a new fleet or a clean reset is a fleet_roster
        change plus a re-run of this method, not a code change.
        """
        roster = self.conn.execute(
            "SELECT agent_id, home_station, genesis_cr, genesis_frag, genesis_fuel FROM fleet_roster"
        ).fetchall()
        if not roster:
            return
        totals = {'CR': 0, 'FRAG': 0, 'FUEL': 0}
        for r in roster:
            totals['CR'] += r['genesis_cr']
            totals['FRAG'] += r['genesis_frag']
            totals['FUEL'] += r['genesis_fuel']
        txn_ids = {'CR': 'genesis-cr', 'FRAG': 'genesis-frag', 'FUEL': 'genesis-fuel'}
        for instrument, total in totals.items():
            self.conn.execute(
                "INSERT INTO accounts (agent_id, instrument, balance) VALUES ('SYSTEM', ?, ?)",
                (instrument, -total)
            )
            self.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, 0, 'SYSTEM', ?, ?)",
                (txn_ids[instrument], instrument, -total)
            )
        for r in roster:
            # CR on the corp; its goods and FUEL aboard its first ship (#175).
            ship = f"{r['agent_id']}/1"
            for acct, instrument, amount in ((r['agent_id'], 'CR', r['genesis_cr']), (ship, 'FRAG', r['genesis_frag']),
                                             (ship, 'FUEL', r['genesis_fuel'])):
                self.conn.execute(
                    "INSERT INTO accounts (agent_id, instrument, balance) VALUES (?, ?, ?)",
                    (acct, instrument, amount)
                )
                self.conn.execute(
                    "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, 0, ?, ?, ?)",
                    (txn_ids[instrument], acct, instrument, amount)
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status) VALUES (?, ?, ?, ?, 0, 'docked')",
                (f"{r['agent_id']}/1", r['agent_id'], f"{r['agent_id']} Ship 1", r['home_station'])
            )
        # Genesis goods beyond one hold wait in the corp's hold at home (#95).
        for r in roster:
            self.fleet.unload_overflow_locked(f"{r['agent_id']}/1", txn=f"genesis-hold-{r['agent_id']}")

    def _seed_genesis_equities(self) -> None:
        """Mint each fleet's synthetic equity (FLEET_EQUITIES) at genesis."""
        # Each rival starts with rival_shares of every other fleet's
        # stock and the issuer keeps the rest, so there are holders on both
        # sides from round 0 and a reason to trade.
        for issuer_id, conf in FLEET_EQUITIES.items():
            sym = conf["symbol"]
            total_shares = conf["total_shares"]
            rivals = [a for a in FLEET_EQUITIES if a != issuer_id] if self.rival_shares else []
            per = min(self.rival_shares, total_shares // (len(rivals) + 1)) if rivals else 0
            xs = min(getattr(self, 'exchange_shares', 0), total_shares - per * len(rivals))
            grants = [(issuer_id, total_shares - per * len(rivals) - xs)] + [(a, per) for a in rivals]
            if xs:
                grants.append((EXCHANGE_ID, xs))
            txn = f"genesis-{sym.lower()}"
            self.conn.execute("INSERT INTO accounts (agent_id, instrument, balance) VALUES ('SYSTEM', ?, ?)",
                              (sym, -total_shares))
            self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, 0, 'SYSTEM', ?, ?)",
                              (txn, sym, -total_shares))
            for holder, qty in grants:
                self.conn.execute("INSERT INTO accounts (agent_id, instrument, balance) VALUES (?, ?, ?)",
                                  (holder, sym, qty))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, 0, ?, ?, ?)",
                                  (txn, holder, sym, qty))
        if getattr(self, 'exchange_shares', 0):
            for acct, inst, d in self.exchange.genesis_cr_legs():
                self.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)",
                                  (acct, inst))
                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                                  (d, acct, inst))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) "
                                  "VALUES ('genesis-exchange-cr', 0, ?, ?, ?)", (acct, inst, d))

    def _wipe_trading_state(self, note: str) -> None:
        """Shared by reset_to_genesis() and new_game(): clears every trading/
        ledger table and re-seeds fleet_roster's genesis balances + equities.
        Leaves fleet_roster and the in-memory engines untouched -- callers
        decide what those become next."""
        with self.lock, self.conn:
            for table in (
                'accounts', 'ledger_entries', 'book_events', 'station_prices',
                'transits', 'vessels', 'equity_loans', 'distress_beacons',
                'rescue_rfqs', 'rescue_quotes', 'salvage_claims',
                'circuit_breaker_halts', 'price_history', 'orders', 'station_escrow', 'station_contracts', 'transit_hazards', 'corp_status', 'corp_events', 'fleet_upgrades',
                'piracy_raids', 'piracy_privateers', 'rng_bags', *STANDING_TABLES,
            ):
                self.conn.execute(f"DELETE FROM {table}")
            self.standing.reset_locked()
            self.conn.execute(
                "INSERT INTO book_events (seq, kind, payload) VALUES "
                "(0, 'floor_open', ?)",
                (json.dumps({"note": note}),)
            )
            self._seed_genesis_from_roster()
            self._seed_genesis_equities()

        self.current_round = 0
        self.last_price = None
        self.last_qty = None
        self.last_prices = {}
        self.last_quantities = {}
        self.floor = 'open'
        self.books = {
            st: {comm: OrderBook(instrument=comm) for comm in (*COMMODITIES, 'BANANA', *EQUITY_SYMBOLS)}
            for st in STATIONS
        }
        self.book = self.books['ceres'][self.default_instrument if self.default_instrument in self.books['ceres'] else 'FRAG']
        self.equity = SyndicateEquityEngine(self.conn, self)
        self.salvage = DerelictSalvageEngine(self.conn, self)
        self.circuit_breaker = CircuitBreakerEngine(self.conn, self, band_pct=self.band_pct)
        self.lobbying = LobbyingDesk(self)

    def reset_to_genesis(
        self,
        depots: Optional[bool] = None,
        asymmetric: Optional[bool] = None,
        spawn_map: Optional[Dict[str, str]] = None,
        depot_model: Optional[str] = None,
        band_pct: Optional[float] = None,
        shelf_skew: Optional[float] = None,
        peer_trades: Optional[bool] = None,
        fog: Any = None,
        idle_fee: Optional[int] = None,
        rival_shares: Optional[int] = None,
        exchange_shares: Optional[int] = None,
        exchange_vol: Optional[float] = None,
        contracts: Optional[bool] = None,
        hazards: Any = None,
        corporate: Optional[bool] = None,
        upgrades: Optional[bool] = None,
        piracy: Any = None,
        events: Optional[bool] = None,
        order_flow: Optional[bool] = None,
        standing: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Full clean-slate reset, callable live via POST /referee/admin/reset:
        wipes every trading/ledger table and re-seeds from fleet_roster.
        If asymmetric=True or spawn_map is provided, updates fleet_roster
        home_station prior to re-seeding.
        """
        if depot_model in DEPOT_MODELS:
            self.depot_model = depot_model
        if band_pct is not None and 0 < float(band_pct) < 1:
            self.band_pct = float(band_pct)
        if shelf_skew is not None:
            self.shelf_skew = float(shelf_skew)
        if depots is not None:
            self.depots_enabled = depots
        if peer_trades is not None:
            self.peer_trades = bool(peer_trades)
        if idle_fee is not None:
            self.idle_fee = max(0, int(idle_fee))
        if rival_shares is not None:
            self.rival_shares = max(0, int(rival_shares))
        if exchange_shares is not None:
            self.exchange_shares = clamp_shares(exchange_shares)
        if exchange_vol is not None:
            self.exchange.vol = max(0.0, float(exchange_vol))
        if contracts is not None:
            self.contracts_enabled = bool(contracts)
        if hazards is not None:
            self.hazards.odds = parse_hazards(hazards)
        if corporate is not None:
            self.corporate_enabled = bool(corporate)
        if upgrades is not None:
            self.upgrades_enabled = bool(upgrades)
        if piracy is not None:
            self.piracy.odds = parse_piracy(piracy)
        if events is not None:
            self.events_enabled = bool(events)
        if order_flow is not None:
            self.order_flow.enabled = bool(order_flow)
        if standing is not None:
            self.standing.enabled = bool(standing)
        self._active_this_round = set()
        self.asymmetric_enabled = asymmetric
        if asymmetric or spawn_map:
            mapping = spawn_map or ASYMMETRIC_SPAWN_LOCATIONS
            with self.lock, self.conn:
                for agent_id, home_station in mapping.items():
                    self.conn.execute(
                        "UPDATE fleet_roster SET home_station = ? WHERE agent_id = ?",
                        (home_station, agent_id)
                    )
        self._wipe_trading_state("reset via POST /referee/admin/reset")
        self.galnet = GalNetEngine()
        self.spatial = StationPriceEngine()
        if self.depots_enabled:
            self.seed_depots()
        self._configure_fog(fog, seed=0)
        self._start_exchange(seed=0)
        self.contract_desk.reset(0)
        self.hazards.reset(0)
        self.piracy.reset(0)
        self.events.reset(0)
        self.covert.reset(0)
        self.order_flow.reset(0)
        stock_marks = self._stock_marks()
        self.history_engine.record_genesis(self.spatial.get_prices(), stock_marks=stock_marks)

        return {'seq': 0, 'floor': self.floor, 'fleets': [r['agent_id'] for r in
                self.conn.execute("SELECT agent_id FROM fleet_roster").fetchall()]}

    def new_game(
        self,
        seed: Optional[int] = None,
        warmup_rounds: Optional[int] = None,
        depots: Optional[bool] = None,
        asymmetric: Optional[bool] = None,
        spawn_map: Optional[Dict[str, str]] = None,
        depot_model: Optional[str] = None,
        band_pct: Optional[float] = None,
        shelf_skew: Optional[float] = None,
        peer_trades: Optional[bool] = None,
        fog: Any = None,
        idle_fee: Optional[int] = None,
        rival_shares: Optional[int] = None,
        exchange_shares: Optional[int] = None,
        exchange_vol: Optional[float] = None,
        contracts: Optional[bool] = None,
        hazards: Any = None,
        corporate: Optional[bool] = None,
        upgrades: Optional[bool] = None,
        piracy: Any = None,
        events: Optional[bool] = None,
        order_flow: Optional[bool] = None,
        standing: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Wipes the board exactly like reset_to_genesis(), but rolls a genuinely
        random opening market instead of the flat, deterministic one that
        method leaves behind.
        If asymmetric=True or spawn_map is provided, updates fleet_roster
        home_station prior to re-seeding.
        """
        if depot_model in DEPOT_MODELS:
            self.depot_model = depot_model
        if band_pct is not None and 0 < float(band_pct) < 1:
            self.band_pct = float(band_pct)
        if shelf_skew is not None:
            self.shelf_skew = float(shelf_skew)
        if depots is not None:
            self.depots_enabled = depots
        if peer_trades is not None:
            self.peer_trades = bool(peer_trades)
        if idle_fee is not None:
            self.idle_fee = max(0, int(idle_fee))
        if rival_shares is not None:
            self.rival_shares = max(0, int(rival_shares))
        if exchange_shares is not None:
            self.exchange_shares = clamp_shares(exchange_shares)
        if exchange_vol is not None:
            self.exchange.vol = max(0.0, float(exchange_vol))
        if contracts is not None:
            self.contracts_enabled = bool(contracts)
        if hazards is not None:
            self.hazards.odds = parse_hazards(hazards)
        if corporate is not None:
            self.corporate_enabled = bool(corporate)
        if upgrades is not None:
            self.upgrades_enabled = bool(upgrades)
        if piracy is not None:
            self.piracy.odds = parse_piracy(piracy)
        if events is not None:
            self.events_enabled = bool(events)
        if order_flow is not None:
            self.order_flow.enabled = bool(order_flow)
        if standing is not None:
            self.standing.enabled = bool(standing)
        self._active_this_round = set()
        self.asymmetric_enabled = asymmetric
        if asymmetric or spawn_map:
            mapping = spawn_map or ASYMMETRIC_SPAWN_LOCATIONS
            with self.lock, self.conn:
                for agent_id, home_station in mapping.items():
                    self.conn.execute(
                        "UPDATE fleet_roster SET home_station = ? WHERE agent_id = ?",
                        (home_station, agent_id)
                    )
        self._wipe_trading_state("new game via POST /referee/admin/new_game")
        self.galnet = GalNetEngine()

        roll_seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**31)
        engine = StationPriceEngine(seed=roll_seed)
        rounds_to_roll = (
            warmup_rounds if warmup_rounds is not None
            else random.SystemRandom().randint(3, 8)
        )
        for r in range(1, rounds_to_roll + 1):
            if hasattr(self, 'galnet') and self.galnet is not None:
                self.galnet.step_round(r)
            engine.step_round(r, galnet_engine=self.galnet)
        self.spatial = engine

        opening_prices = self.spatial.get_prices()
        with self.lock, self.conn:
            for station_id, commodities in opening_prices.items():
                for commodity, spot_price in commodities.items():
                    self.conn.execute("""
                        INSERT OR REPLACE INTO station_prices
                            (station_id, commodity, round, base_price, drift_bias, spot_price, updated_at)
                        VALUES (?, ?, 0, ?, 0.0, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    """, (station_id, commodity, BASE_PRICES[station_id][commodity], spot_price))

        if self.depots_enabled:
            self.seed_depots()
        self._configure_fog(fog, seed=roll_seed)
        self._start_exchange(seed=roll_seed)
        self.contract_desk.reset(roll_seed)
        self.hazards.reset(roll_seed)
        self.piracy.reset(roll_seed)
        self.events.reset(roll_seed)
        self.covert.reset(roll_seed)
        self.order_flow.reset(roll_seed)
        stock_marks = self._stock_marks()
        self.history_engine.record_genesis(opening_prices, stock_marks=stock_marks)

        return {
            'seq': 0,
            'floor': self.floor,
            'seed': roll_seed,
            'fog': None if not self.fog else {'lag': self.fog.lag, 'noise': self.fog.noise},
            'warmup_rounds': rounds_to_roll,
            'opening_prices': opening_prices,
            'fleets': [r['agent_id'] for r in self.conn.execute("SELECT agent_id FROM fleet_roster").fetchall()],
        }

    def _rehydrate_book(self):
        """Rehydrate resting orders from database into in-memory order book in price-time priority."""
        cur = self.conn.cursor()
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(orders)").fetchall()]
        has_station_col = 'station_id' in cols
        has_vessel_col = 'vessel_id' in cols

        query = f"""
            SELECT order_id, agent_id, instrument, side, qty, limit_price, seq_seen, filled_qty
                   {', station_id' if has_station_col else ''}{', vessel_id' if has_vessel_col else ''}
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
            if r['instrument'] in GOODS and self.fleet.is_corp(r['agent_id']):
                order.vessel_id = (r['vessel_id'] if has_vessel_col else None) or f"{r['agent_id']}/1"
                order.acct = order.vessel_id
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
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COALESCE(MAX(seq), 0) FROM book_events")
            row = cur.fetchone()
            return row[0] if row else 0

    def mark_active(self, agent_id: Optional[str]) -> None:
        """Record that a fleet acted this round (exempts it from the idle fee)."""
        if agent_id:
            self._active_this_round.add(str(agent_id))

    def _charge_idle_fees_locked(self, round_num: int) -> Dict[str, int]:
        """Charge idle_fee CR (capped at what the fleet holds) to every docked
        fleet that did nothing since the last round, provided at least one
        other fleet did. A fleet in transit is busy flying and pays nothing.
        Balanced ledger entries to SYSTEM."""
        charged: Dict[str, int] = {}
        fleets = [r[0] for r in self.conn.execute("SELECT agent_id FROM fleet_roster")]
        # The live ticker keeps stepping rounds when nobody is playing (every
        # 60 s, overnight included). Charge only in rounds where some fleet
        # did act, so an empty server never bleeds everyone's cash.
        if not self.idle_fee or not (self._active_this_round & set(fleets)):
            self._active_this_round = set()
            return charged
        seq = self._get_next_seq()
        for agent in fleets:
            if agent in self._active_this_round:
                continue
            if any(l.get('status') != 'docked' for l in self.fleet_locations(agent)):
                continue  # a ship in flight: the fleet is working
            if hasattr(self, 'lobbying') and self.lobbying and self.lobbying.is_idle_exempt(agent):
                continue
            fee = min(self.idle_fee, max(0, self.get_balance(agent, 'CR')))
            if fee <= 0:
                continue
            txn = f"idle-fee-{agent}-{round_num}"
            for acct, d in ((agent, -fee), ('SYSTEM', fee)):
                self.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, 'CR', 0)", (acct,))
                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (d, acct))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                                  (txn, seq, acct, d))
            charged[agent] = fee
        self._active_this_round = set()
        return charged

    def _start_exchange(self, seed: int) -> None:
        """Reseed the exchange's price noise from the game seed and post its
        opening quotes."""
        self.exchange.reset(seed)
        if self.exchange_shares:
            with self.lock, self.conn:
                self.exchange.refresh_locked()

    def _configure_fog(self, fog: Any, seed: int) -> None:
        """fog=None keeps the current setting (re-seeded for the new game);
        False turns it off; True or {"lag", "noise"} turns it on."""
        if fog is None:
            cfg = (self.fog.lag, self.fog.noise) if self.fog else None
        else:
            cfg = parse_fog(fog)
        self.fog = FogEngine(cfg[0], cfg[1], seed) if cfg else None
        if self.fog:
            self.fog.record(self)

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

    def get_ticker_state(self) -> Optional[Dict[str, Any]]:
        """
        Read the durable ticker desired-state record (Issue #63). Returns
        None if no state has ever been persisted (fresh database).
        """
        with self.lock:
            row = self.conn.execute(
                "SELECT desired_state, quiet_round_count, last_tick_at, lease_owner, lease_expires_at, updated_at "
                "FROM ticker_state WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def set_ticker_state(
        self,
        desired_state: str,
        quiet_round_count: Optional[int] = None,
        last_tick_at: Optional[str] = None,
        lease_owner: Optional[str] = None,
        lease_expires_at: Optional[str] = None,
    ) -> None:
        """
        Upsert the durable ticker desired-state record. Called on start/
        pause/stop and periodically on tick so a process restart can
        reconcile against what the ticker was actually doing, not wake up
        cold. `lease_owner`/`lease_expires_at` fence against two server
        instances (e.g. a Railway blue/green rollover) both stepping the
        referee concurrently — a process only ticks while it holds the lease.
        """
        if desired_state not in ('running', 'paused', 'stopped'):
            raise ValueError(f"invalid desired_state '{desired_state}'")
        with self.lock:
            existing = self.conn.execute("SELECT 1 FROM ticker_state WHERE id = 1").fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO ticker_state (id, desired_state, quiet_round_count, last_tick_at, lease_owner, lease_expires_at) "
                    "VALUES (1, ?, ?, ?, ?, ?)",
                    (desired_state, quiet_round_count or 0, last_tick_at, lease_owner, lease_expires_at)
                )
                return
            # lease_owner/lease_expires_at are always written to whatever was
            # passed (None included) — the caller (TickerEngine._persist_state)
            # always passes them explicitly, and None is the meaningful "this
            # process no longer holds a lease" state on pause/stop. Only
            # quiet_round_count/last_tick_at are conditionally-updated, since
            # other callers may legitimately omit them.
            fields = [
                "desired_state = ?",
                "lease_owner = ?",
                "lease_expires_at = ?",
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            ]
            params: list = [desired_state, lease_owner, lease_expires_at]
            if quiet_round_count is not None:
                fields.append("quiet_round_count = ?")
                params.append(quiet_round_count)
            if last_tick_at is not None:
                fields.append("last_tick_at = ?")
                params.append(last_tick_at)
            self.conn.execute(f"UPDATE ticker_state SET {', '.join(fields)} WHERE id = 1", params)

    def record_burst_event(self, phase: str, payload_extra: Optional[Dict[str, Any]] = None) -> int:
        """
        Records a burst-run lifecycle event ('start', 'tick', 'conclude') into
        book_events. Terminal Web HUD clients pick these up for free via the
        existing /ws/terminal 'ticks' diff stream (TerminalDiffEngine.get_diffs),
        no separate broadcast plumbing required.
        """
        with self.lock:
            next_seq = self.current_seq + 1
            payload = {'phase': phase, 'round': self.current_round}
            if payload_extra:
                payload.update(payload_extra)
            self.conn.execute(
                "INSERT INTO book_events (seq, kind, payload) VALUES (?, 'burst', ?)",
                (next_seq, json.dumps(payload))
            )
            return next_seq

    def get_balance(self, agent_id: str, instrument: str) -> int:
        """One account's balance. For a roster corp and a good (#175), the
        corp total: every ship's hold plus its station holds. Code that
        debits goods reads the one account it debits instead
        (available_account, or get_balance on the vessel_id)."""
        with self.lock:
            if instrument in GOODS and '/' not in (agent_id or '') and self.fleet.is_corp(agent_id):
                return self.corp_goods(agent_id, instrument)
            return self._account_balance(agent_id, instrument)

    def corp_goods(self, corp: str, instrument: str) -> int:
        """A corp's total of one good across its ships and station holds."""
        with self.lock:
            return sum(self._account_balance(a, instrument) for a in [corp] + self.fleet.accounts_of(corp))

    def _account_balance(self, agent_id: str, instrument: str) -> int:
        with self.lock:
            return self._account_balance_locked(agent_id, instrument)

    def _account_balance_locked(self, agent_id: str, instrument: str) -> int:
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

    def committed(self, agent_id: str, instrument: str, acct: Optional[str] = None) -> int:
        """What resting orders already commit: CR for every bid of the
        corp (any ship), goods for the asks settling on `acct`."""
        with self.lock:
            return self._committed_locked(agent_id, instrument, acct)

    def _committed_locked(self, agent_id: str, instrument: str, acct: Optional[str] = None) -> int:
        n = 0
        for books in self.books.values():
            for comm, book in books.items():
                if instrument == 'CR':
                    n += sum(o.remaining_qty * o.limit_price for o in book.bids if o.agent_id == agent_id)
                elif comm == instrument:
                    n += sum(o.remaining_qty for o in book.asks
                             if (o.acct or o.agent_id) == (acct or agent_id))
        return n

    def available_account(self, acct: str, instrument: str) -> int:
        """One goods account's balance less the asks that settle on it."""
        with self.lock:
            return self._account_balance(acct, instrument) - self.committed(corp_of(acct), instrument, acct)

    def available(self, agent_id: str, instrument: str, vessel_id: Optional[str] = None) -> int:
        """Balance less what resting orders commit. CR is the corp's; goods
        are one ship's (ship 1 unless vessel_id says otherwise)."""
        with self.lock:
            if instrument in GOODS:
                acct, err = self.fleet.goods_account(agent_id, vessel_id)
                if err:
                    return 0
                return self.available_account(acct, instrument)
            return self._account_balance(agent_id, instrument) - self.committed(agent_id, instrument)

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

    def get_vessel_location(self, agent_id: str, vessel_id: Optional[str] = None) -> Dict[str, Any]:
        """Where one of a fleet's ships is: ship 1 unless vessel_id names
        another (#175). Takes the lock itself: it is called from GET
        handlers, fog and the briefing, and for a never-seen agent_id it
        INSERTs (#197)."""
        with self.lock:
            return self._get_vessel_location_locked(agent_id, vessel_id)

    def vessel_location(self, vessel_id: str) -> Dict[str, Any]:
        """Location of a ship by its vessel_id ('<corp>/<n>')."""
        with self.lock:
            return self._get_vessel_location_locked(corp_of(vessel_id), vessel_id)

    def _get_vessel_location_locked(self, agent_id: str, vessel_id: Optional[str] = None) -> Dict[str, Any]:
        vid = vessel_id or f"{agent_id}/1"
        cur = self.conn.cursor()
        cur.execute("""
            SELECT transit_id, origin, destination, departure_round, arrival_round, commodity, cargo_qty, fuel_burned,
                   perishable, decay_rate, decayed_qty
            FROM transits
            WHERE vessel_id = ? AND status = 'in_transit'
            ORDER BY departure_round DESC LIMIT 1
        """, (vid,))
        tx = cur.fetchone()
        if tx:
            # decayed_qty is only written on arrival (see step_round); while still
            # in_transit it sits at its INSERT default of 0. A live meter needs a
            # projection, so estimate it from elapsed rounds * decay_rate — same
            # formula step_round uses at settlement, just evaluated early.
            total_rounds = max(1, tx['arrival_round'] - tx['departure_round'])
            elapsed_rounds = max(0, min(total_rounds, self.current_round - tx['departure_round']))
            decay_rate = tx['decay_rate'] or 0.0
            cargo_qty = tx['cargo_qty'] or 0
            projected_decayed_qty = (
                min(cargo_qty, int(round(cargo_qty * decay_rate * elapsed_rounds)))
                if tx['perishable'] else 0
            )
            return {
                'agent_id': agent_id,
                'vessel_id': vid,
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
                    'fuel_burned': tx['fuel_burned'],
                    'perishable': bool(tx['perishable']),
                    'decay_rate': decay_rate,
                    'decayed_qty': tx['decayed_qty'] or 0,
                    'projected_decayed_qty': projected_decayed_qty
                }
            }

        cur.execute("SELECT station_id, docked_since FROM vessels WHERE vessel_id = ?", (vid,))
        row = cur.fetchone()
        if not row:
            if agent_id.startswith('depot_') and agent_id[len('depot_'):] in STATIONS:
                return {'agent_id': agent_id, 'vessel_id': None, 'station_id': agent_id[len('depot_'):],
                        'status': 'docked', 'docked_since': 0, 'transit': None}
            # A roster fleet's ship 1 is seeded at genesis; if it is gone the
            # fleet was taken over (#164) and has no ships. Never recreate it.
            if (vid != f"{agent_id}/1" or '/' in agent_id or agent_id == 'SYSTEM'
                    or self.fleet.is_corp(agent_id)):
                return {'agent_id': agent_id, 'vessel_id': vid, 'station_id': None, 'status': 'no_ship',
                        'docked_since': None, 'transit': None}
            roster_row = cur.execute("SELECT home_station FROM fleet_roster WHERE agent_id = ?", (agent_id,)).fetchone()
            home = roster_row['home_station'] if roster_row else 'ceres'
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status) VALUES (?, ?, ?, ?, 0, 'docked')",
                    (vid, agent_id, f"{agent_id} Ship 1", home)
                )
            return {
                'agent_id': agent_id,
                'vessel_id': vid,
                'station_id': home,
                'status': 'docked',
                'docked_since': 0,
                'transit': None
            }
        return {
            'agent_id': agent_id,
            'vessel_id': vid,
            'station_id': row['station_id'],
            'status': 'docked',
            'docked_since': row['docked_since'],
            'transit': None
        }

    def get_all_vessel_locations(self) -> List[Dict[str, Any]]:
        """Ship 1 of every fleet (the pre-#175 one-row-per-fleet shape);
        GET /referee/vessels lists every ship."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT DISTINCT agent_id FROM vessels WHERE agent_id NOT LIKE 'depot_%'
                UNION
                SELECT DISTINCT agent_id FROM accounts WHERE agent_id != 'SYSTEM' AND agent_id NOT LIKE 'depot_%'
                    AND agent_id NOT LIKE '%/%'
                ORDER BY agent_id ASC
            """)
            agents = [r[0] for r in cur.fetchall()]
            return [self._get_vessel_location_locked(a) for a in agents]

    def fleet_locations(self, agent_id: str) -> List[Dict[str, Any]]:
        """Every active ship of a fleet, with its location."""
        with self.lock:
            return [self._get_vessel_location_locked(agent_id, v['vessel_id']) for v in self.fleet.ships(agent_id)]

    def docked_stations(self, agent_id: str) -> List[str]:
        """Stations where at least one of the fleet's ships is docked."""
        return sorted({l['station_id'] for l in self.fleet_locations(agent_id) if l.get('status') == 'docked'})

    def get_vessels(self, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return list of registered fleet vessels, optionally filtered by agent_id."""
        with self.lock:
            return self._get_vessels_locked(agent_id)

    def _get_vessels_locked(self, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        if agent_id:
            cur.execute("""
                SELECT vessel_id, agent_id, name, station_id, docked_since, bought_round, cost, status, created_at
                FROM vessels
                WHERE agent_id = ?
                ORDER BY vessel_id ASC
            """, (agent_id,))
        else:
            cur.execute("""
                SELECT vessel_id, agent_id, name, station_id, docked_since, bought_round, cost, status, created_at
                FROM vessels
                ORDER BY agent_id ASC, vessel_id ASC
            """)
        return [dict(r) for r in cur.fetchall()]

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

    def fleet_out(self, agent_id: Optional[str]) -> Optional[str]:
        """Why a bankrupt or taken-over corp cannot act, or None."""
        if not agent_id or not getattr(self, 'corporate_enabled', False):
            return None
        with self.lock:
            return self.corporate.out_reason(agent_id)

    def upsert_fleet_roster(self, agent_id: str, display_name: str, home_station: str,
                            genesis_cr: Any, genesis_frag: Any, genesis_fuel: Any) -> None:
        """POST /referee/admin/fleets. Takes effect on the next reset. Used to
        run on the shared connection with no lock and commit, which could
        commit another thread's half-done transaction (#197)."""
        with self.lock, self.conn:
            self.conn.execute("""
                INSERT INTO fleet_roster (agent_id, display_name, home_station, genesis_cr, genesis_frag, genesis_fuel)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    home_station = excluded.home_station,
                    genesis_cr = excluded.genesis_cr,
                    genesis_frag = excluded.genesis_frag,
                    genesis_fuel = excluded.genesis_fuel
            """, (agent_id, display_name, home_station, genesis_cr, genesis_frag, genesis_fuel))

    def _dock_vessel_locked(self, agent_id: str, station_id: str, vessel_id: Optional[str] = None) -> None:
        """Dock one of a fleet's ships at `station_id`, as of the current round. For a path that ends a transit
        without it arriving (a salvage claim, a corp going out): without this
        the fleet stays 'in_transit' with no in_transit transit, reads as
        docked at a station called "in_transit", and every later MOVE is
        rejected invalid_route (#198).

        Caller holds self.lock and is inside its own transaction; this only
        executes, it never commits (a nested commit is the #197 bug class)."""
        vessel_id = vessel_id or f"{agent_id}/1"
        rnd = self.current_round
        # A ship marked for scrapping stays marked until it is scrapped.
        self.conn.execute("""
            INSERT INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status)
            VALUES (?, ?, ?, ?, ?, 'docked')
            ON CONFLICT(vessel_id) DO UPDATE SET
                station_id = ?,
                docked_since = ?,
                status = CASE WHEN status = 'scrap_pending' THEN status ELSE 'docked' END
        """, (vessel_id, agent_id, f"{agent_id} Ship {vessel_id.rsplit('/', 1)[-1]}", station_id, rnd, station_id, rnd))
        row = self.conn.execute("SELECT status FROM vessels WHERE vessel_id = ?", (vessel_id,)).fetchone()
        if row and row['status'] == 'scrap_pending':
            self.fleet.scrap_locked(vessel_id, "absorbed over the owner's ship cap; scrapped on landing")

    def initiate_transit(self, agent_id: str, destination: str, commodity: str = 'FRAG', cargo_qty: int = 0, perishable: Optional[bool] = None,
                         escort: bool = False, vessel_id: Optional[str] = None) -> Dict[str, Any]:
        """Fly one ship (ship 1 unless vessel_id names another, #175). Each
        ship makes one trip at a time; the others are unaffected. FUEL and
        cargo come out of that ship's hold, the toll and escort out of the
        corp's CR."""
        self.mark_active(agent_id)
        out = self.fleet_out(agent_id)
        if out:
            return {'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'fleet_out', 'detail': out}}
        with self.lock:
            dest = destination.lower().strip()
            if dest not in STATIONS:
                return {
                    'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'invalid_station', 'detail': f"Unknown destination station '{destination}'. Valid stations: {STATIONS}"}
                }

            vid, err = self.fleet.resolve(agent_id, vessel_id)
            if err:
                return dict(err, reply='optional', floor=self.floor)
            acct = vid if self.fleet.is_corp(agent_id) else agent_id
            loc = self._get_vessel_location_locked(agent_id, vid)
            if loc['status'] == 'in_transit':
                return {
                    'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                    'payload': {'reason': 'already_in_transit', 'detail': f"Ship '{vid}' is already in transit to '{loc['transit']['destination']}'"}
                }
            if loc['status'] != 'docked':
                return {'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                        'payload': {'reason': 'invalid_vessel', 'detail': f"Ship '{vid}' is not docked anywhere"}}

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

            # Engines tier 2 cuts the burn by 40% (agora/upgrades.py, #189).
            required_fuel = self.upgrades.engine_fuel(agent_id, route['fuel'])
            fuel_bal = self._account_balance(acct, 'FUEL')
            committed_fuel = self.committed(agent_id, 'FUEL', acct)
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
                committed_cr = self.committed(agent_id, 'CR')
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
                comm_bal = self._account_balance(acct, comm)
                committed_comm = self.committed(agent_id, comm, acct)
                avail_comm = comm_bal - committed_comm
                if avail_comm < cargo_qty:
                    return {
                        'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                        'payload': {'reason': 'insufficient_cargo', 'detail': f"Required {cargo_qty} {comm}, available {avail_comm} (balance {comm_bal} - committed {committed_comm})"}
                    }
                # FUEL shipped as cargo and the trip's burn both come out of
                # the same FUEL balance, so they must fit together (#160).
                if comm == 'FUEL' and avail_fuel < required_fuel + cargo_qty:
                    return {
                        'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                        'payload': {'reason': 'insufficient_fuel', 'detail': f"Route {origin}->{dest} burns {required_fuel} FUEL and you are shipping {cargo_qty} FUEL as cargo: needs {required_fuel + cargo_qty}, available {avail_fuel} (balance {fuel_bal} - committed {committed_fuel})"}
                    }

            # Piracy escort (agora/piracy.py): paid at departure, on top of
            # any toll. Ignored when piracy is off or there is no cargo.
            escort = bool(escort) and self.piracy.enabled and cargo_qty > 0
            escort_fee = self.piracy.escort_fee(comm, cargo_qty) if escort else 0
            if escort_fee > 0:
                cr_bal = self.get_balance(agent_id, 'CR')
                committed_cr = self.committed(agent_id, 'CR')
                avail_cr = cr_bal - committed_cr
                if avail_cr < toll_required + escort_fee:
                    return {
                        'v': 1, 'kind': 'reject', 'reply': 'optional', 'floor': self.floor,
                        'payload': {'reason': 'insufficient_credits_for_escort', 'detail': f"An escort for {cargo_qty} {comm} costs {escort_fee} CR ({int(PIRACY_ESCORT_PCT * 100)}% of the cargo's value){f' plus the {toll_required} CR toll' if toll_required else ''}; available {avail_cr} CR. Move without an escort, or raise cash first."}
                    }

            # Cancel the departing ship's resting orders (every one of them
            # rests at its origin); other ships' orders stay up. A non-corp
            # agent has one location, so all its orders at the origin go.
            if origin in self.books:
                for b in self.books[origin].values():
                    for o in list(b.bids) + list(b.asks):
                        if o.agent_id != agent_id or o.instrument in EQUITY_SYMBOLS:
                            continue  # stocks trade from anywhere (#146); a departure leaves them up
                        if o.acct is None or o.acct == acct:
                            # Already inside self.lock: use the _locked body.
                            self._cancel_order_locked(agent_id, o.order_id)

            transit_id = f"tx-{agent_id}-{time.time_ns()}"
            dep_round = self.current_round
            # Bad luck (agora/hazards.py): rolled once at departure and told
            # to the fleet now, so it can react. Lost cargo stays with SYSTEM.
            hz_delay, hz_lost, hz_note = self.hazards.roll(
                cargo_qty if cargo_qty > 0 else 0,
                delay_factor=self.upgrades.factor(agent_id, 'shielding'),
                loss_factor=self.upgrades.factor(agent_id, 'hold'),
                loss_size_factor=self.upgrades.loss_size_factor(agent_id), agent_id=vid)
            # Engines upgrade: tier 1 cuts a round off trips of 3+ rounds
            # (agora/upgrades.py ENGINE_CUTS); tier 2 cut the fuel above.
            base_rounds = route['rounds'] - self.upgrades.engine_cut(agent_id, route['rounds'])
            arr_round = dep_round + base_rounds + hz_delay

            with self.conn:
                next_seq = self._get_next_seq()
                # 1. Fuel debit (the ship's hold -> SYSTEM)
                self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'FUEL'", (required_fuel, acct))
                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'SYSTEM' AND instrument = 'FUEL'", (required_fuel,))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'FUEL', ?)", (f"fuel-{transit_id}", next_seq, acct, -required_fuel))
                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'FUEL', ?)", (f"fuel-{transit_id}", next_seq, required_fuel))

                # 2. Belt toll debit (agent -> SYSTEM)
                if toll_required > 0:
                    self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'CR'", (toll_required, agent_id))
                    self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'SYSTEM' AND instrument = 'CR'", (toll_required,))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)", (f"toll-{transit_id}", next_seq, agent_id, -toll_required))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'CR', ?)", (f"toll-{transit_id}", next_seq, toll_required))

                # 3. Cargo escrow (if cargo_qty > 0)
                if cargo_qty > 0:
                    self.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES ('SYSTEM', ?, 0)", (comm,))
                    self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?", (cargo_qty, acct, comm))
                    self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'SYSTEM' AND instrument = ?", (cargo_qty, comm))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (f"escrow-{transit_id}", next_seq, acct, comm, -cargo_qty))
                    self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', ?, ?)", (f"escrow-{transit_id}", next_seq, comm, cargo_qty))

                # 4. Transits record
                vessel_id = vid
                self.conn.execute("""
                    INSERT INTO transits (transit_id, agent_id, vessel_id, origin, destination, departure_round, arrival_round, commodity, cargo_qty, fuel_burned, status, perishable, decay_rate, decayed_qty, toll_paid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_transit', ?, ?, 0, ?)
                """, (transit_id, agent_id, vessel_id, origin, dest, dep_round, arr_round, comm, cargo_qty - hz_lost, required_fuel, int(is_perishable), decay_rate, toll_required))
                self.hazards.record(transit_id, agent_id, dep_round, hz_delay, hz_lost, comm, hz_note)
                if hz_lost and self.events_enabled:
                    lost_cr = piracy_cargo_value(comm, hz_lost)
                    self.events.record_locked('hazard_loss', 'public', victim=agent_id, amount=lost_cr,
                                              detail=f"{agent_id} lost {hz_lost} {comm} in flight (worth {lost_cr} CR)")

                # Piracy: escort fee, then the raid roll on what is still aboard.
                self.piracy.charge_escort_locked(transit_id, agent_id, escort_fee)
                piracy = self.piracy.roll_departure_locked(
                    transit_id, agent_id, origin, dest, toll_required > 0, comm,
                    max(0, cargo_qty - hz_lost), escort, escort_fee, dep_round, vessel_id=vid)

                # 5. The ship
                self.conn.execute("""
                    INSERT INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status)
                    VALUES (?, ?, ?, 'in_transit', ?, 'in_transit')
                    ON CONFLICT(vessel_id) DO UPDATE SET
                        station_id = 'in_transit',
                        docked_since = ?,
                        status = 'in_transit'
                """, (vessel_id, agent_id, f"{agent_id} Ship 1", dep_round, dep_round))

                # 6. Book event tick. Ticks are public (GET /referee/ticks), so
                # with secrecy on (#153) the tick carries the public view of a
                # pirate demand, not the victim's, and no odds (a privateer
                # contract adds to them).
                tick_piracy = piracy
                if piracy and self.events_enabled:
                    tick_piracy = {k: v for k, v in piracy.items() if k != 'odds'}
                    if piracy.get('demand'):
                        tick_piracy['demand'] = self.piracy.public_raid(self.piracy._row(transit_id))
                self.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'transit', ?)", (
                    next_seq,
                    json.dumps({
                        'transit_id': transit_id,
                        'agent_id': agent_id,
                        'vessel_id': vessel_id,
                        'origin': origin,
                        'destination': dest,
                        'departure_round': dep_round,
                        'arrival_round': arr_round,
                        'rounds_duration': base_rounds,
                        'commodity': comm,
                        'cargo_qty': cargo_qty,
                        'fuel_burned': required_fuel,
                        'is_aligned': route.get('is_aligned', False),
                        'window_name': route.get('window_name'),
                        'toll_paid': toll_required,
                        'perishable': is_perishable,
                        'decay_rate': decay_rate,
                        'hazard': {'delay': hz_delay, 'lost_qty': hz_lost, 'note': hz_note} if hz_note else None,
                        'piracy': tick_piracy,
                    })
                ))

            return {
                'v': 1,
                'kind': 'status',
                'status': 'in_transit',
                'payload': {
                    'transit_id': transit_id,
                    'agent_id': agent_id,
                    'vessel_id': vessel_id,
                    'origin': origin,
                    'destination': dest,
                    'departure_round': dep_round,
                    'arrival_round': arr_round,
                    'rounds_duration': base_rounds,
                    'commodity': comm,
                    'cargo_qty': cargo_qty,
                    'fuel_burned': required_fuel,
                    'is_aligned': route.get('is_aligned', False),
                    'window_name': route.get('window_name'),
                    'toll_paid': toll_required,
                    'perishable': is_perishable,
                    'decay_rate': decay_rate,
                    'hazard': {'delay': hz_delay, 'lost_qty': hz_lost, 'note': hz_note} if hz_note else None,
                    'piracy': piracy,
                }
            }

    def step_round(self, round_num: Optional[int] = None) -> Dict[str, Any]:
        with self.lock:
            new_round = round_num if round_num is not None else self.current_round + 1
            # Idle fee for the round that is ending, before anything moves.
            with self.conn:
                idle_fees = self._charge_idle_fees_locked(self.current_round)
                # Crew and berth for every ship beyond a corp's first (#175).
                ship_upkeep = self.fleet.upkeep_locked(self.current_round)
                # Station order flow (agora/order_flow.py): NPC buyers and
                # sellers fill the fleet orders left resting this round,
                # against the depot quotes the fleets saw, before prices move.
                order_flow_report = self.order_flow.step_locked(self.current_round)
            self.current_round = new_round

            # Advance GalNet news & exogenous shocks (#123)
            if hasattr(self, 'galnet') and self.galnet is not None:
                self.galnet.step_round(new_round)

            # Advance prices
            spot_prices = self.spatial.step_round(new_round, galnet_engine=self.galnet)
            with self.conn:
                for p in spot_prices:
                    self.conn.execute("""
                        INSERT OR REPLACE INTO station_prices (station_id, commodity, round, base_price, drift_bias, spot_price, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    """, (p.station_id, p.commodity, p.round, p.base_price, p.drift_bias, p.spot_price))

                if self.depots_enabled:
                    self._refresh_depot_orders_locked()
                stock_marks = self._stock_marks()
                self.history_engine.record_round_start(new_round, spot_prices, stock_marks=stock_marks)
                # Not inside _refresh_depot_orders_locked: the reactive model
                # returns early from it, and reactive is the live default.
                if self.exchange_shares:
                    self.exchange.refresh_locked()

                # Piracy: unanswered demands are fought now, before arrivals
                # settle, so a fight's loss or delay applies to this trip.
                piracy_report = self.piracy.step_locked(new_round)

                # Settle arriving transits
                cur = self.conn.cursor()
                cur.execute("""
                    SELECT transit_id, agent_id, vessel_id, origin, destination, commodity, cargo_qty, arrival_round, departure_round, perishable, decay_rate
                    FROM transits
                    WHERE status = 'in_transit' AND arrival_round <= ?
                """, (new_round,))
                arrivals = cur.fetchall()
                arrived_list = []
                for a in arrivals:
                    t_id = a['transit_id']
                    ag_id = a['agent_id']
                    v_id = a['vessel_id'] or f"{ag_id}/1"
                    # The goods land in the ship's own hold (#175).
                    hold = v_id if self.fleet.is_corp(ag_id) else ag_id
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
                            self.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (hold, comm))
                            self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (deliver_qty, hold, comm))
                            self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = 'SYSTEM' AND instrument = ?", (deliver_qty, comm))
                            self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)", (f"release-{t_id}", next_seq, hold, comm, deliver_qty))
                            self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', ?, ?)", (f"release-{t_id}", next_seq, comm, -deliver_qty))

                    self.conn.execute("UPDATE transits SET status = 'arrived', decayed_qty = ? WHERE transit_id = ?", (decay_qty, t_id))
                    if hasattr(self, 'lobbying') and self.lobbying and dest:
                        tariff = self.lobbying.get_docking_tariff(ag_id, dest)
                        if tariff > 0:
                            actual_tariff = min(tariff, max(0, self.get_balance(ag_id, 'CR')))
                            if actual_tariff > 0:
                                t_seq = self._get_next_seq()
                                self.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = 'CR'", (actual_tariff, ag_id))
                                self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'SYSTEM' AND instrument = 'CR'", (actual_tariff,))
                                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)", (f"tariff-dock-{t_id}", t_seq, ag_id, -actual_tariff))
                                self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, 'SYSTEM', 'CR', ?)", (f"tariff-dock-{t_id}", t_seq, actual_tariff))
                    # Docks the ship (current_round is already new_round); a
                    # hull absorbed over its owner's cap is scrapped here (#164).
                    self._dock_vessel_locked(ag_id, dest, v_id)
                    # Normally a no-op: only a ship that left before the hold
                    # limit (or with a bigger one) lands over it (#95).
                    if hold != ag_id:
                        self.fleet.unload_overflow_locked(v_id)

                    arrival_payload = {
                        'transit_id': t_id,
                        'agent_id': ag_id,
                        'vessel_id': v_id,
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

                # Peer escrow: buyers who are now docked collect; overdue pickups refund.
                peer_report = self.peer.step_locked(new_round) if self.peer_trades else None
                contract_report = self.contract_desk.step_locked(new_round) if self.contracts_enabled else None
                corporate_report = self.corporate.step_locked(new_round) if self.corporate_enabled else None
                # GalNet story when a locked upgrade goes on sale (agora/upgrades.py).
                if self.upgrades_enabled:
                    self.upgrades.step_locked(new_round)
                # Leak rolls for secrets and 20% stake disclosures (agora/events.py).
                events_report = self.events.step_locked(new_round) if self.events_enabled else None
                # Earned standing from this round's realized P&L by lane (agora/standing.py).
                standing_report = self.standing.step_locked(new_round)

            # Distribute bilateral borrow fees & audit maintenance margin
            borrow_fee_reports = self.equity.step_borrow_fees(new_round)

            # Audit active circuit breaker halts and execute call auction reopens
            reopen_reports = self.circuit_breaker.step_round(new_round)

            if self.fog:
                self.fog.record(self)

            return {
                'status': 'ok',
                'round': new_round,
                'prices': self.spatial.get_prices(),
                'arrived_transits': arrived_list,
                'borrow_fee_reports': borrow_fee_reports,
                'circuit_breaker_reopens': reopen_reports,
                'peer_escrow': peer_report,
                'contracts': contract_report,
                'corporate': corporate_report,
                'piracy': piracy_report,
                'events': events_report,
                'idle_fees': idle_fees,
                'ship_upkeep': ship_upkeep,
                'order_flow': order_flow_report,
                'standing': standing_report,
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

    def _stock_marks(self) -> Dict[str, float]:
        if hasattr(self, 'exchange') and self.exchange and getattr(self.exchange, 'price', None):
            return {s: round(float(p), 2) for s, p in self.exchange.price.items()}
        return {s: 20.0 for s in EQUITY_SYMBOLS}

    def get_price_history(
        self,
        station_id: str,
        instrument: str,
        rounds: int = 20,
        viewer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve OHLCV price history for station/instrument respecting fog of war (#122)."""
        return self.history_engine.get_history(
            ref=self,
            station_id=station_id,
            instrument=instrument,
            rounds=rounds,
            viewer=viewer
        )

    def get_circuit_breaker_bands(self, station_id: Optional[str] = None, instrument: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns LULD bands, VWAPs, and halt status."""
        if station_id and instrument:
            return [self.circuit_breaker.get_bands(station_id, instrument)]
        return self.circuit_breaker.get_all_bands()

    def get_circuit_breaker_halts(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns active or historical circuit breaker halts."""
        return self.circuit_breaker.get_halts(status=status)

    def trigger_circuit_breaker_halt(self, station_id: str, instrument: str, trigger_price: float, reason: str = "manual_halt") -> Dict[str, Any]:
        """Manually trigger a 2-round circuit breaker halt."""
        with self.lock:
            return self.circuit_breaker.trigger_halt(
                station_id=station_id,
                instrument=instrument,
                trigger_price=trigger_price,
                reason=reason,
                current_round=self.current_round
            )

    def reopen_circuit_breaker_auction(self, station_id: str, instrument: str) -> Dict[str, Any]:
        """Manually triggers call auction reopen matching for a halted book."""
        with self.lock:
            return self.circuit_breaker.execute_auction_reopen(
                station_id=station_id,
                instrument=instrument,
                round_num=self.current_round
            )

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
        """One agent's rows, or every row. A roster corp's goods are shown
        as one corp-total row per good (#175: they live on its ships; see
        get_ship_accounts for each ship's hold)."""
        if agent_id and self.fleet.is_corp(agent_id):
            with self.lock:
                bare = {r['instrument']: r['balance'] for r in self.conn.execute(
                    "SELECT instrument, balance FROM accounts WHERE agent_id = ?", (agent_id,))}
                insts = set(bare)
                for acct in self.fleet.accounts_of(agent_id):
                    insts |= {r[0] for r in self.conn.execute("SELECT instrument FROM accounts WHERE agent_id = ?", (acct,))}
                return [{'agent_id': agent_id, 'instrument': i,
                         'balance': self.corp_goods(agent_id, i) if i in GOODS else bare.get(i, 0)}
                        for i in sorted(insts)]
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

    def get_ship_accounts(self, agent_id: str) -> Dict[str, Dict[str, int]]:
        """{account: {instrument: balance}} for each of a corp's ships and
        station holds."""
        with self.lock:
            out = {}
            for acct in self.fleet.accounts_of(agent_id):
                out[acct] = {r[0]: r[1] for r in self.conn.execute(
                    "SELECT instrument, balance FROM accounts WHERE agent_id = ? ORDER BY instrument", (acct,))}
            return out

    def submit_envelope(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        self.mark_active((envelope.get('payload') or {}).get('agent_id') if isinstance(envelope, dict) else None)
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

        out = self.fleet_out(agent_id)
        if out:
            return self._reject_envelope(order_id, agent_id, 'fleet_out', out)

        if instrument not in COMMODITIES and instrument != 'BANANA' and instrument not in EQUITY_SYMBOLS:
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

        # 2b. Spatial Locality & Docking Audit. Goods are bought and sold by
        # one ship (payload vessel_id, ship 1 by default), which must be
        # docked at the order's station; its goods settle on that ship's
        # hold (#175).
        is_stock = instrument in EQUITY_SYMBOLS
        order_vessel = None
        order_acct = None
        if '/' in str(agent_id):
            return self._reject_envelope(order_id, agent_id, 'invalid_format',
                                         "agent_id is a fleet, not one of its ships; name the ship in vessel_id")
        if not is_stock:
            order_vessel, err = self.fleet.resolve(agent_id, payload.get('vessel_id'))
            if err:
                return self._reject_envelope(order_id, agent_id, err['payload']['reason'], err['payload']['detail'])
            order_acct = order_vessel if self.fleet.is_corp(agent_id) else None
            vessel = self._get_vessel_location_locked(agent_id, order_vessel)
        # Fleet stocks trade on one exchange book, from anywhere, in transit
        # included (Ryan, #agent-chat 2026-09-22 22:44).
        if is_stock:
            vessel = {'status': 'docked', 'station_id': STOCK_EXCHANGE_STATION}
            payload = dict(payload, station_id=STOCK_EXCHANGE_STATION)
        if vessel['status'] == 'in_transit':
            dest_station = vessel.get('transit', {}).get('destination', 'destination')
            return self._reject_envelope(
                order_id, agent_id, 'vessel_in_transit',
                f"Ship '{order_vessel}' is currently in transit to '{dest_station}' and cannot place orders until docked."
            )
        if vessel['status'] != 'docked':
            return self._reject_envelope(order_id, agent_id, 'invalid_vessel', f"Ship '{order_vessel}' is not docked anywhere")

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
                    f"Ship '{order_vessel or agent_id}' is docked at '{docked_station}', cannot place orders at '{order_station}'. Local trading only."
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
            committed_funds = self.committed(agent_id, 'CR')
            buyer_balance = self.get_balance(agent_id, currency)
            available_funds = buyer_balance - committed_funds
            if available_funds < max_cost:
                return self._reject_envelope(
                    order_id, agent_id, 'insufficient_balance',
                    f"Account '{agent_id}' available {currency} balance {available_funds} "
                    f"(balance {buyer_balance} - committed {committed_funds}) insufficient for bid requirement {max_cost}"
                )
            # The ship must have room for the goods, beside what its other
            # resting bids already keep (agora/fleet.py): a fill can then
            # never overflow the hold (#95).
            room = self.fleet.room(order_acct, instrument) if order_acct else None
            if room is not None and qty > room:
                h = self.fleet.hold_status(order_acct)
                return self._reject_envelope(
                    order_id, agent_id, 'hold_full',
                    f"Ship '{order_acct}' has room for {room} more {instrument}, not {qty}: hold capacity "
                    f"{h['hold_capacity']}, used {h['hold_used']}, kept for resting bids {h['hold_reserved']}"
                    + (f" (FUEL up to {h['fuel_tank']} rides in the tank)" if instrument == 'FUEL' else '')
                    + ". Sell or transfer cargo, or bid for less."
                )
        elif side == 'ask':
            goods_acct = order_acct or agent_id
            committed_commodity = self.committed(agent_id, instrument, goods_acct)
            seller_balance = self._account_balance(goods_acct, instrument)
            available_commodity = seller_balance - committed_commodity
            if available_commodity < qty:
                return self._reject_envelope(
                    order_id, agent_id, 'insufficient_balance',
                    f"Account '{goods_acct}' available {instrument} balance {available_commodity} "
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

        # 3c. Resting fleet orders on the other side that their owner can no
        # longer pay for (CR or goods taken since they were placed, e.g. by a
        # debt payment or a contract penalty) are cancelled before matching:
        # the book never re-checks funding at fill time, and a stale bid was
        # filled into a negative balance (#162 sim, styles_novice seed 20).
        self._prune_unfunded_locked(target_book, 'ask' if side == 'bid' else 'bid')

        # 4. Matching & Atomic Ledger Settlement
        order = Order(
            order_id=order_id,
            agent_id=agent_id,
            instrument=instrument,
            side=side,
            qty=qty,
            limit_price=limit_price,
            seq_seen=seq_seen,
            acct=order_acct,
            vessel_id=order_vessel,
        )
        if order_vessel:
            payload = dict(payload, vessel_id=order_vessel)

        # Check if station book is currently halted by circuit breaker (commodities only)
        is_commodity = (instrument.upper() in COMMODITIES or instrument.upper() == 'BANANA') and not instrument.upper().startswith('EQ_')
        if is_commodity and self.circuit_breaker.is_halted(order_station, instrument):
            halt_info = self.circuit_breaker.get_active_halt(order_station, instrument)
            with self.conn:
                next_seq = self.current_seq + 1
                if order.side == 'bid':
                    target_book.bids.append(order)
                    target_book.bids.sort(key=lambda o: (-o.limit_price, o.submitted_at))
                else:
                    target_book.asks.append(order)
                    target_book.asks.sort(key=lambda o: (o.limit_price, o.submitted_at))

                self.conn.execute("""
                    INSERT INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, resolved_seq, filled_qty, station_id, vessel_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, 0, ?, ?)
                """, (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, order_station, order_vessel))

                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload)
                    VALUES (?, 'order', ?)
                """, (next_seq, json.dumps(payload)))

            return {
                'v': 1,
                'kind': 'market_tick',
                'reply': 'optional',
                'floor': 'halted',
                'scope': 'channel',
                'subject': 'agent-collaborative-project',
                'payload': {
                    'seq': self.current_seq,
                    'station_id': order_station,
                    'instrument': instrument,
                    'best_bid': target_book.best_bid(),
                    'best_ask': target_book.best_ask(),
                    'last_price': self.get_last_price(order_station, instrument),
                    'last_qty': self.get_last_qty(order_station, instrument),
                    'status': 'halted',
                    'trades_count': 0,
                    'auction_resting': True,
                    'reopen_round': halt_info.get('reopen_round') if halt_info else None
                }
            }

        # Inspect if crossing order would breach rolling +-10% LULD bands
        # Circuit breaker applies to station commodities where a station is explicitly targeted
        # or prior trades have established an active rolling market.
        has_station_specified = bool(payload.get('station_id'))
        has_trades = self.circuit_breaker.has_prior_trades(order_station, instrument)
        should_check_luld = is_commodity and (has_station_specified or has_trades)

        breach = False
        breach_price = None
        if should_check_luld:
            bands = self.circuit_breaker.get_bands(order_station, instrument)
            lower_limit = bands['lower_limit']
            upper_limit = bands['upper_limit']

            if order.side == 'bid':
                for ask in target_book.asks:
                    if ask.limit_price <= order.limit_price:
                        if ask.limit_price < lower_limit or ask.limit_price > upper_limit:
                            breach = True
                            breach_price = ask.limit_price
                            break
            elif order.side == 'ask':
                for bid in target_book.bids:
                    if bid.limit_price >= order.limit_price:
                        if bid.limit_price < lower_limit or bid.limit_price > upper_limit:
                            breach = True
                            breach_price = bid.limit_price
                            break

        if breach:
            # Trigger discrete 2-round station trading halt
            halt_res = self.circuit_breaker.trigger_halt(
                station_id=order_station,
                instrument=instrument,
                trigger_price=breach_price,
                reason=f"LULD breach: trade price {breach_price} outside [{lower_limit}, {upper_limit}] (VWAP: {bands['vwap']})",
                current_round=self.current_round
            )

            # Order rests in the halted book for the upcoming auction call
            with self.conn:
                next_seq = self.current_seq + 1
                if order.side == 'bid':
                    target_book.bids.append(order)
                    target_book.bids.sort(key=lambda o: (-o.limit_price, o.submitted_at))
                else:
                    target_book.asks.append(order)
                    target_book.asks.sort(key=lambda o: (o.limit_price, o.submitted_at))

                self.conn.execute("""
                    INSERT INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, resolved_seq, filled_qty, station_id, vessel_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, 0, ?, ?)
                """, (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, order_station, order_vessel))

                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload)
                    VALUES (?, 'order', ?)
                """, (next_seq, json.dumps(payload)))

            return {
                'v': 1,
                'kind': 'status',
                'status': 'circuit_breaker_halted',
                'floor': 'halted',
                'payload': {
                    'station_id': order_station,
                    'instrument': instrument,
                    'halt_round': self.current_round,
                    'reopen_round': halt_res['reopen_round'],
                    'trigger_price': breach_price,
                    'lower_limit': lower_limit,
                    'upper_limit': upper_limit,
                    'vwap': bands['vwap'],
                    'action': 'order_resting_for_auction'
                }
            }

        book_snapshot = copy.deepcopy(target_book)
        try:
            with self.conn:
                next_seq = self.current_seq + 1

                # Match against target book
                trades, resting = target_book.add_order(order, current_seq=next_seq)

                # Record submission in orders table
                status = 'filled' if order.is_filled else 'open'
                self.conn.execute("""
                    INSERT INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, resolved_seq, filled_qty, station_id, vessel_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, next_seq if order.is_filled else None, order.filled_qty, order_station, order_vessel))

                # Record book event
                self.conn.execute("""
                    INSERT INTO book_events (seq, kind, payload)
                    VALUES (?, 'order', ?)
                """, (next_seq, json.dumps(payload)))

                # Settle each executed trade atomically in ledger_entries and accounts
                for trade in trades:
                    self.circuit_breaker.record_trade(order_station, instrument, trade.price, trade.qty, self.current_round)
                    st_key = (order_station.lower(), instrument.upper())
                    self.last_prices[st_key] = trade.price
                    self.last_quantities[st_key] = trade.qty
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
                    # CR moves between the corps, goods between the ships (#175).
                    buyer_goods = trade.buyer_acct or trade.buyer_id
                    seller_goods = trade.seller_acct or trade.seller_id

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
                    """, (txn_id, next_seq, buyer_goods, commodity_inst, trade.qty))
                    self.conn.execute("""
                        INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta)
                        VALUES (?, ?, ?, ?, ?)
                    """, (txn_id, next_seq, seller_goods, commodity_inst, -trade.qty))

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
                        (buyer_goods, commodity_inst)
                    )
                    cur = self.conn.execute(
                        "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                        (trade.qty, buyer_goods, commodity_inst)
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Failed to credit {buyer_goods} {commodity_inst}: rowcount {cur.rowcount} != 1")

                    cur = self.conn.execute(
                        "UPDATE accounts SET balance = balance - ? WHERE agent_id = ? AND instrument = ?",
                        (trade.qty, seller_goods, commodity_inst)
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Failed to debit {seller_goods} {commodity_inst}: rowcount {cur.rowcount} != 1")

                    # Record trade event in book_events
                    trade_seq = self.current_seq + 1
                    self.conn.execute("""
                        INSERT INTO book_events (seq, kind, payload)
                        VALUES (?, 'trade', ?)
                    """, (trade_seq, json.dumps({
                        'trade_id': trade.trade_id,
                        'buyer_id': trade.buyer_id,
                        'seller_id': trade.seller_id,
                        'buyer_vessel': trade.buyer_acct if trade.buyer_acct != trade.buyer_id else None,
                        'seller_vessel': trade.seller_acct if trade.seller_acct != trade.seller_id else None,
                        'price': trade.price,
                        'qty': trade.qty,
                        'cost': cost,
                        'station_id': order_station,
                        'instrument': instrument
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
        order_status = 'filled' if order.is_filled else ('partially_filled' if order.filled_qty > 0 else 'resting')
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
                'last_price': self.get_last_price(order_station, instrument),
                'last_qty': self.get_last_qty(order_station, instrument),
                'status': self.floor,
                'trades_count': len(trades),
                'order_id': order_id,
                'order_status': order_status,
                'filled_qty': order.filled_qty,
                'remaining_qty': order.remaining_qty
            }
        }

    def cancel_order(self, agent_id: str, order_id: str) -> Dict[str, Any]:
        self.mark_active(agent_id)
        """
        Cancel one resting order across all station order books.
        """
        with self.lock:
            return self._cancel_order_locked(agent_id, order_id)

    def _prune_unfunded_locked(self, book: OrderBook, side: str) -> List[str]:
        """Cancel resting fleet orders on `side` of `book` whose owner's
        balance no longer covers everything it has committed on that side
        (all its bids' CR, or all its asks of that good). Depots and SYSTEM
        are left alone: their quotes are re-funded at every refresh."""
        pruned = []
        room: Dict[str, Optional[int]] = {}  # per ship: hold room left for this book's bids, in priority order
        for o in list(book.bids if side == 'bid' else book.asks):
            a = o.agent_id
            if a == 'SYSTEM' or a.startswith('depot_'):
                continue
            if side == 'bid':
                need = sum(x.remaining_qty * x.limit_price for st_books in self.books.values()
                           for b in st_books.values() for x in b.bids if x.agent_id == a)
                have = self.get_balance(a, self.get_currency_instrument(a))
                # A bid its ship can no longer hold (#95): normally never, as
                # placement keeps room for every resting bid.
                acct = getattr(o, 'acct', None)
                if acct and have >= need and o.instrument in GOODS:
                    if acct not in room:
                        room[acct] = self.fleet.room(acct, o.instrument, reserved=False)
                    if room[acct] is not None:
                        if o.remaining_qty > room[acct]:
                            self._cancel_order_locked(a, o.order_id)
                            pruned.append(o.order_id)
                            continue
                        room[acct] -= o.remaining_qty
            else:
                need = self.committed(a, o.instrument, o.goods_acct)
                have = self._account_balance(o.goods_acct, o.instrument)
            if have < need:
                self._cancel_order_locked(a, o.order_id)
                pruned.append(o.order_id)
        return pruned

    def _cancel_order_locked(self, agent_id: str, order_id: str) -> Dict[str, Any]:
        """
        cancel_order() body. Caller must already hold self.lock.

        The DB write comes first and the order leaves the in-memory book only
        once it has committed. The other way round, a write that threw left
        the order 'open' in the orders table but resting in no book: it could
        never match or be cancelled, and came back on restart (#197).
        """
        removed = None
        removed_from = None
        for st_books in self.books.values():
            for b in st_books.values():
                for o in (*b.bids, *b.asks):
                    if o.order_id == order_id and o.agent_id == agent_id:
                        removed, removed_from = o, b
                        break
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
        removed_from.remove_order(order_id, agent_id)

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
        self.mark_active(agent_id)
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

        # Invariant 4 (#175): a roster corp's goods live on its ships and
        # station holds, never on '<corp>' itself (goods with no location
        # would trade at whichever station a ship happened to be).
        marks = ','.join('?' for _ in GOODS)
        cur.execute(f"""
            SELECT a.agent_id, a.instrument, a.balance FROM accounts a JOIN fleet_roster f ON f.agent_id = a.agent_id
            WHERE a.instrument IN ({marks}) AND a.balance != 0
        """, sorted(GOODS))
        for row in cur.fetchall():
            errors.append(f"Goods off ship: {row['agent_id']} holds {row['balance']} {row['instrument']} on the corp account")

        # Invariant 5 (#175): a ship is 'in_transit' exactly when it has one
        # in_transit trip, and never more than one.
        cur.execute("""
            SELECT v.vessel_id, v.station_id,
                   (SELECT COUNT(*) FROM transits t WHERE t.vessel_id = v.vessel_id AND t.status = 'in_transit') AS n
            FROM vessels v
        """)
        for row in cur.fetchall():
            flying = row['station_id'] == 'in_transit'
            if row['n'] > 1 or flying != (row['n'] == 1):
                errors.append(f"Vessel breach: {row['vessel_id']} at '{row['station_id']}' with {row['n']} in_transit trips")
        cur.execute("""
            SELECT transit_id, vessel_id FROM transits WHERE status = 'in_transit'
              AND (vessel_id IS NULL OR vessel_id NOT IN (SELECT vessel_id FROM vessels))
        """)
        for row in cur.fetchall():
            errors.append(f"Vessel breach: trip {row['transit_id']} flies unknown ship {row['vessel_id']}")

        # Invariant 6 (#95): no docked ship holds more cargo than its hold
        # carries. (A ship that took off before the limit existed may fly
        # over it; it unloads the excess when it lands.)
        if self.ship_hold:
            cur.execute("SELECT vessel_id FROM vessels WHERE status = 'docked'")
            for (vid,) in cur.fetchall():
                h = self.fleet.hold_status(vid)
                if h['hold_capacity'] is not None and h['hold_used'] > h['hold_capacity']:
                    errors.append(f"Hold breach: {vid} carries {h['hold_used']} cargo units, capacity {h['hold_capacity']}")

        return len(errors) == 0, errors

    def get_leaderboard(self) -> List[Dict[str, Any]]:
        """
        Calculate Net Worth = Balance(Credits/CR) + Qty(Cargo) * Local Spot Price
        + Upgrades + Ships + Stocks.
        All commodities (FRAG, FOOD, ORE) are marked at the spot price of the
        station they are at (#196): each ship's hold where that ship is docked
        (or the departure station while it flies), each station hold at its
        station (#175). Bought ships count at half their price.
        """
        station_marks: Dict[str, Dict[str, int]] = {}
        for st in STATIONS:
            station_marks[st] = {
                comm: int(round(self.spatial.get_station_price(st, comm))) if self.spatial else int(round(BASE_PRICES[st][comm]))
                for comm in ('FRAG', 'FOOD', 'ORE')
            }

        with self.lock:
            cur = self.conn.cursor()
            # One row per corp, its ships' and station holds' accounts
            # ('<corp>/...') folded in; goods valued where each account is.
            per: Dict[str, Dict[str, Any]] = {}
            where_cache: Dict[str, str] = {}
            for r in cur.execute("""
                SELECT agent_id, instrument, balance FROM accounts
                WHERE agent_id != 'SYSTEM' AND agent_id NOT LIKE 'depot_%'
            """).fetchall():
                corp = corp_of(r['agent_id'])
                d = per.setdefault(corp, {'agent_id': corp, 'liquid': 0, 'frags': 0, 'fuel': 0, 'food': 0, 'ore': 0,
                                          'cargo_val': 0})
                inst, bal = r['instrument'], r['balance']
                if inst in ('CR', 'CREDITS', 'CASH'):
                    d['liquid'] += bal
                    continue
                if inst not in ('FRAG', 'BANANA', 'FUEL', 'FOOD', 'ORE'):
                    continue
                key = {'FRAG': 'frags', 'BANANA': 'frags', 'FUEL': 'fuel', 'FOOD': 'food', 'ORE': 'ore'}[inst]
                d[key] += bal
                if inst == 'FUEL' or not bal:
                    continue
                acct = r['agent_id']
                if acct not in where_cache:
                    where_cache[acct] = (self.fleet.mark_station(acct) if is_ship_account(acct)
                                         else self._fleet_mark_station_locked(acct))
                comm_key = 'FRAG' if inst == 'BANANA' else inst
                d['cargo_val'] += bal * station_marks[where_cache[acct]][comm_key]
            rows = list(per.values())

            board = []
            # Goods and CR held in peer escrow still count toward whoever owns them.
            # Goods in escrow stay at their escrow station until collected, and are
            # valued at that station's spot price (#196).
            peer_escrow = self.peer.holdings_adjustment_by_station() if getattr(self, 'peer', None) else {}
            escrow_cr: Dict[str, int] = {}
            escrow_goods_val: Dict[str, int] = {}
            for agent, data in peer_escrow.items():
                escrow_cr[agent] = data.get('CR', 0)
                for st, inst, qty in data.get('goods', []):
                    st_key = st if st in STATIONS else 'ceres'
                    comm_key = 'FRAG' if inst in ('FRAG', 'BANANA') else inst
                    if comm_key in ('FRAG', 'FOOD', 'ORE'):
                        mark = station_marks.get(st_key, {}).get(comm_key, 0)
                        escrow_goods_val[agent] = escrow_goods_val.get(agent, 0) + qty * mark

            # Contract deposits are still the owner's money.
            bonds = self.contract_desk.holdings_adjustment() if getattr(self, 'contract_desk', None) else {}
            for agent, cr in bonds.items():
                escrow_cr[agent] = escrow_cr.get(agent, 0) + cr
            upgrades = getattr(self, 'upgrades', None)

            # Cargo in transit (transits table, status='in_transit') is escrowed to
            # SYSTEM while under way. It still belongs to the fleet and is counted
            # toward net worth, net of projected perishable decay, valued at its
            # origin station spot (#195, #196).
            transit_cargo: Dict[str, Dict[str, int]] = {}
            transit_val: Dict[str, int] = {}
            tables = [t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if 'transits' in tables:
                for tx in cur.execute("""
                    SELECT agent_id, origin, destination, commodity, cargo_qty, perishable, decay_rate, departure_round
                    FROM transits
                    WHERE status = 'in_transit'
                """).fetchall():
                    c_qty = tx['cargo_qty'] or 0
                    if c_qty <= 0:
                        continue
                    comm = (tx['commodity'] or '').upper().strip()
                    if not comm:
                        continue
                    dep_round = tx['departure_round'] if tx['departure_round'] is not None else self.current_round
                    elapsed = max(0, self.current_round - dep_round)
                    rate = tx['decay_rate'] or 0.0
                    decay = min(c_qty, int(round(c_qty * rate * elapsed))) if (tx['perishable'] and rate > 0) else 0
                    net_qty = max(0, c_qty - decay)
                    transit_cargo.setdefault(tx['agent_id'], {})
                    transit_cargo[tx['agent_id']][comm] = transit_cargo[tx['agent_id']].get(comm, 0) + net_qty

                    tx_orig = tx['origin'].lower().strip() if tx['origin'] and tx['origin'].lower().strip() in STATIONS else 'ceres'
                    comm_key = 'FRAG' if comm == 'BANANA' else comm
                    t_mark = station_marks.get(tx_orig, {}).get(comm_key, 0)
                    transit_val[tx['agent_id']] = transit_val.get(tx['agent_id'], 0) + net_qty * t_mark

            for r in rows:
                in_flight = transit_cargo.get(r['agent_id'], {})
                in_flight_val = transit_val.get(r['agent_id'], 0)

                # Ship 1's station: the board's headline station and marks.
                st_id = self._fleet_mark_station_locked(r['agent_id'])
                fleet_marks = station_marks[st_id]
                docked_cargo_val = r['cargo_val']

                # Fitted ship upgrades at half their cost (agora/upgrades.py,
                # #151), and bought ships at half their price (#175); nothing
                # for a corp that is out of the game.
                is_out = self.fleet_out(r['agent_id'])
                fitted = upgrades.book_value(r['agent_id']) if upgrades and not is_out else 0
                ships_value = self.fleet.book_value(r['agent_id']) if not is_out else 0
                net_worth = (r['liquid']
                             + escrow_cr.get(r['agent_id'], 0)
                             + docked_cargo_val
                             + escrow_goods_val.get(r['agent_id'], 0)
                             + in_flight_val
                             + fitted
                             + ships_value)
                board.append({
                    'agent_id': r['agent_id'],
                    'net_worth': net_worth,
                    'liquid': r['liquid'],
                    'frags': r['frags'],
                    'fuel': r['fuel'],
                    'food': r['food'] if 'food' in r.keys() else 0,
                    'ore': r['ore'] if 'ore' in r.keys() else 0,
                    'bananas': r['frags'],  # backward compatibility alias
                    'mark_price': fleet_marks['FRAG'],
                    'commodity_marks': fleet_marks,
                    'station_id': st_id,
                    'upgrades_value': fitted,
                    'ships': len(self.fleet.ships(r['agent_id'])),
                    'ships_value': ships_value,
                    'in_transit_cargo': in_flight,
                })
            if getattr(self, 'corporate_enabled', False):
                for b in board:
                    b['status'] = self.corporate.status(b['agent_id'])
                    try:
                        # 1. Tender offer escrow (remaining unspent escrow)
                        t_row = self.conn.execute(
                            "SELECT COALESCE(SUM((shares_wanted - shares_filled) * price), 0) AS escrow "
                            "FROM corp_tender_offers WHERE raider = ? AND status = 'open'",
                            (b['agent_id'],)).fetchone()
                        if t_row and t_row['escrow']:
                            b['net_worth'] += int(t_row['escrow'])

                        # 2. Loan offers escrowed in SYSTEM
                        l_escrow = self.conn.execute(
                            "SELECT COALESCE(SUM(principal), 0) AS escrow "
                            "FROM corp_loan_offers WHERE lender = ? AND status = 'open'",
                            (b['agent_id'],)).fetchone()
                        if l_escrow and l_escrow['escrow']:
                            b['net_worth'] += int(l_escrow['escrow'])

                        # 3. Active predatory loans: lender receivable (+) and borrower payable (-)
                        rec = self.conn.execute(
                            "SELECT COALESCE(SUM(due_amount), 0) AS receivable "
                            "FROM corp_predatory_loans WHERE lender = ? AND status = 'active'",
                            (b['agent_id'],)).fetchone()
                        if rec and rec['receivable']:
                            b['net_worth'] += int(rec['receivable'])

                        pay = self.conn.execute(
                            "SELECT COALESCE(SUM(due_amount), 0) AS payable "
                            "FROM corp_predatory_loans WHERE borrower = ? AND status = 'active'",
                            (b['agent_id'],)).fetchone()
                        if pay and pay['payable']:
                            b['net_worth'] -= int(pay['payable'])

                        # 4. Referee corporate debt (#220)
                        # Subtract referee debt so debt_buy does not move a free net-worth hit onto rivals.
                        c_row = self.conn.execute(
                            "SELECT debt FROM corp_status WHERE agent_id = ?",
                            (b['agent_id'],)).fetchone()
                        if c_row and c_row['debt']:
                            b['net_worth'] -= int(c_row['debt'])
                            b['debt'] = int(c_row['debt'])
                    except Exception:
                        pass

            # Rival stocks count at their mark. A fleet's own shares do not count
            # toward its own net worth (that would be circular), and NAV is built
            # from the net worth above, before any stock holdings (#164, #220).
            base = {b['agent_id']: b['net_worth'] for b in board}
            marks = self.stock_marks(base)
            holdings: Dict[str, Dict[str, int]] = {}
            for row in cur.execute("SELECT agent_id, instrument, balance FROM accounts WHERE instrument LIKE 'EQ_%' "
                                   "AND agent_id != 'SYSTEM' AND agent_id NOT LIKE 'depot_%'"):
                holdings.setdefault(row['agent_id'], {})[row['instrument']] = row['balance']
            for b in board:
                own = FLEET_EQUITIES.get(b['agent_id'], {}).get('symbol')
                held = {sym: q for sym, q in holdings.get(b['agent_id'], {}).items() if sym != own and q}
                value = int(round(sum(q * marks[sym]['mark'] for sym, q in held.items() if sym in marks)))
                b['stocks'] = held
                b['stocks_value'] = value
                b['net_worth'] += value
            board.sort(key=lambda x: x['net_worth'], reverse=True)
            return board
    def _fleet_mark_station_locked(self, agent_id: str) -> str:
        """Ship 1's station (its trip's origin while it flies), for a fleet's
        headline marks and for goods on an account that is not a ship."""
        vessel = self._get_vessel_location_locked(agent_id)
        if vessel.get('status') == 'in_transit' and vessel.get('transit'):
            st_id = (vessel['transit'].get('origin') or 'ceres').lower().strip()
        else:
            st_id = (vessel.get('station_id') or 'ceres').lower().strip()
        return st_id if st_id in STATIONS else 'ceres'

    def get_live_shares(self, sym: str) -> int:
        """Returns the current circulating float of an equity symbol (#164).
        Genesis float is 1,000 shares plus any defensive rights exercised."""
        try:
            from agora.equity import FLEET_EQUITIES
            target = next((fleet for fleet, conf in FLEET_EQUITIES.items() if conf.get("symbol") == sym), None)
            if target:
                row = self.conn.execute(
                    "SELECT COALESCE(SUM(rights_exercised), 0) AS extra FROM corp_poison_pills WHERE target = ?",
                    (target,)).fetchone()
                extra = int(row['extra']) if row and row['extra'] else 0
                return 1000 + extra
        except Exception:
            pass
        return 1000

    def stock_marks(self, base_net_worth: Dict[str, float]) -> Dict[str, Dict[str, Any]]:
        """Per fleet stock: NAV per share from the issuer's net worth before
        stock holdings, and the mark used on the leaderboard: the exchange
        book's mid if both sides rest, else the last trade, else NAV."""
        out = {}
        for issuer, conf in FLEET_EQUITIES.items():
            sym = conf["symbol"]
            total_shares = self.get_live_shares(sym)
            nav = max(1.0, round(base_net_worth.get(issuer, 0) / total_shares, 2))
            book = self.books.get(STOCK_EXCHANGE_STATION, {}).get(sym)
            bid = book.best_bid() if book else None
            ask = book.best_ask() if book else None
            if bid is not None and ask is not None:
                mark, basis = (bid + ask) / 2, 'mid'
            elif (STOCK_EXCHANGE_STATION, sym) in self.last_prices:
                mark, basis = float(self.last_prices[(STOCK_EXCHANGE_STATION, sym)]), 'last'
            else:
                mark, basis = nav, 'nav'
            out[sym] = {'symbol': sym, 'issuer': issuer, 'nav': nav, 'mark': mark, 'basis': basis,
                        'best_bid': bid, 'best_ask': ask, 'total_shares': total_shares}
        return out

    def seed_depots(self, initial_cr: int = 1000000, initial_qty: int = 100000) -> None:
        """
        Seed station depot accounts and continuous resting liquidity pools
        for all Sol stations (Issue #71).
        """
        with self.lock, self.conn:
            self.depots_enabled = True
            self._reset_reactive_state()
            for st in STATIONS:
                depot_id = f"depot_{st}"
                # A depot is not a ship: get_vessel_location('depot_<st>')
                # answers 'docked at <st>' without a vessels row (#175).

                cur = self.conn.cursor()
                cur.execute("SELECT balance FROM accounts WHERE agent_id = ? AND instrument = 'CR'", (depot_id,))
                row = cur.fetchone()
                if not row:
                    txn_id = f"genesis-depot-{st}"
                    for inst, amt in [('CR', initial_cr), *[(c, initial_qty) for c in COMMODITIES]]:
                        self.conn.execute(
                            "INSERT INTO accounts (agent_id, instrument, balance) VALUES ('SYSTEM', ?, 0) ON CONFLICT(agent_id, instrument) DO NOTHING",
                            (inst,)
                        )
                        self.conn.execute(
                            "UPDATE accounts SET balance = balance - ? WHERE agent_id = 'SYSTEM' AND instrument = ?",
                            (amt, inst)
                        )
                        self.conn.execute(
                            "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, 0, 'SYSTEM', ?, ?)",
                            (txn_id, inst, -amt)
                        )
                        self.conn.execute(
                            "INSERT INTO accounts (agent_id, instrument, balance) VALUES (?, ?, ?)",
                            (depot_id, inst, amt)
                        )
                        self.conn.execute(
                            "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, 0, ?, ?, ?)",
                            (txn_id, depot_id, inst, amt)
                        )
            self._refresh_depot_orders_locked()

    def refresh_depot_liquidity(self) -> None:
        """Public thread-safe entrypoint to refresh depot liquidity quotes."""
        with self.lock, self.conn:
            self._refresh_depot_orders_locked()

    def _refresh_depot_orders_locked(self) -> None:
        """
        Refresh two-sided continuous depot resting liquidity across all stations
        for FRAG and FUEL based on current spatial spot prices.
        """
        if getattr(self, 'depot_model', 'static') == 'reactive':
            return self._refresh_reactive_depots_locked()
        round_num = getattr(self, 'current_round', 0)
        for st in STATIONS:
            depot_id = f"depot_{st}"
            # Depot quotes must be fundable. Bids used to be re-posted at a
            # fixed 500/1000 every round whatever the depot held, so sustained
            # hauling drove depot CR negative and verify_ledger_invariants()
            # failed ("Insolvency breach: depot_earth CR balance = -109025",
            # tools/economy_sim.py haulers4/tolerant, round ~180-217). Cap
            # total bid notional at the depot's CR and each ask at its stock.
            cr_budget = max(0, self.get_balance(depot_id, 'CR'))
            for comm in COMMODITIES:
                # 1. Clear existing open depot orders for this station and commodity
                if st in self.books and comm in self.books[st]:
                    book = self.books[st][comm]
                    book.bids = [o for o in book.bids if o.agent_id != depot_id]
                    book.asks = [o for o in book.asks if o.agent_id != depot_id]
                else:
                    if st not in self.books:
                        self.books[st] = {}
                    self.books[st][comm] = OrderBook(instrument=comm)
                    book = self.books[st][comm]

                self.conn.execute("""
                    DELETE FROM orders
                    WHERE agent_id = ? AND station_id = ? AND instrument = ? AND status = 'open'
                """, (depot_id, st, comm))

                # 2. Get current spot price
                spot = self.spatial.get_station_price(st, comm) if self.spatial else BASE_PRICES[st][comm]

                # 3. Compute continuous two-sided prices
                # #71 spec quotes at the two ends of the system, as offsets
                # from the station's base price (on the pre-#162 surface
                # exactly: Earth FRAG 10/11, FUEL 8/9, FOOD 10/11, ORE 29/31;
                # Ceres FRAG 21/22, FUEL 25/26, FOOD 29/31, ORE 10/11), so they
                # follow BASE_PRICES when it changes.
                offs = STATIC_SPEC_OFFSETS.get((st, comm))
                if offs:
                    b0 = int(round(BASE_PRICES[st][comm]))
                    bid_1, ask_1 = b0 + offs[0], b0 + offs[1]
                else:
                    mid = int(round(spot))
                    bid_1 = max(1, mid - 1)
                    ask_1 = max(bid_1 + 1, mid + 1)

                base_p = BASE_PRICES[st][comm]
                drift_offset = int(round(spot - base_p))
                if drift_offset != 0:
                    bid_1 = max(1, bid_1 + drift_offset)
                    ask_1 = max(bid_1 + 1, ask_1 + drift_offset)

                if hasattr(self, 'circuit_breaker') and self.circuit_breaker:
                    try:
                        bands = self.circuit_breaker.get_bands(st, comm)
                        if bands:
                            low = bands.get('lower_limit')
                            high = bands.get('upper_limit')
                            if low is not None and high is not None:
                                max_p = int(math.floor(high))
                                min_p = int(math.ceil(low))
                                if max_p >= min_p:
                                    ask_1 = min(ask_1, max_p)
                                    bid_1 = min(bid_1, ask_1 - 1)
                                    bid_1 = max(min_p, bid_1)
                                    if ask_1 <= bid_1:
                                        ask_1 = bid_1 + 1
                    except Exception:
                        pass

                bid_2 = max(1, bid_1 - 1)
                ask_2 = ask_1 + 1

                levels = [
                    ('bid', bid_1, 500, 1),
                    ('bid', bid_2, 1000, 2),
                    ('ask', ask_1, 500, 1),
                    ('ask', ask_2, 1000, 2),
                ]

                seq = self.current_seq
                stock = max(0, self.get_balance(depot_id, comm))
                for side, price, qty, lvl in levels:
                    if side == 'bid' and price < 1:
                        continue
                    if side == 'bid':
                        qty = min(qty, cr_budget // price)
                        cr_budget -= qty * price
                    else:
                        qty = min(qty, stock)
                        stock -= qty
                    if qty <= 0:
                        continue
                    oid = f"{depot_id}-{comm.lower()}-{side}-r{round_num}-l{lvl}"
                    order = Order(
                        order_id=oid,
                        agent_id=depot_id,
                        instrument=comm,
                        side=side,
                        qty=qty,
                        limit_price=price,
                        seq_seen=seq
                    )
                    if side == 'bid':
                        book._insert_bid(order)
                    else:
                        book._insert_ask(order)

                    self.conn.execute("""
                        INSERT OR REPLACE INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, resolved_seq, filled_qty, station_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, 0, ?)
                    """, (oid, depot_id, comm, side, qty, price, seq, st))

    def _reset_reactive_state(self) -> None:
        """Fresh shelves and holds; called whenever depots are (re)seeded."""
        shelf, hold, prod, cons = {}, {}, {}, {}
        for c in COMMODITIES:
            cheap = min(STATIONS, key=lambda st: BASE_PRICES[st][c])
            dear = max(STATIONS, key=lambda st: BASE_PRICES[st][c])
            for st in STATIONS:
                shelf[(st, c)] = REACTIVE_TARGET // 2
                hold[(st, c)] = 0
                prod[(st, c)] = REACTIVE_MAIN_DRIP if st == cheap else REACTIVE_SIDE_DRIP
                cons[(st, c)] = REACTIVE_MAIN_DRIP if st == dear else REACTIVE_SIDE_DRIP
        row = self.conn.execute("SELECT COALESCE(MAX(entry_id), 0) FROM ledger_entries").fetchone()
        self._reactive = {"shelf": shelf, "hold": hold, "prod": prod, "cons": cons,
                          "ledger_cursor": row[0], "refreshed_round": None}

    def _depot_transfer_locked(self, txn_id: str, depot_id: str, inst: str, qty: int) -> None:
        """Balanced SYSTEM <-> depot transfer (qty > 0 credits the depot)."""
        next_seq = self.current_seq
        for acct, d in ((depot_id, qty), ('SYSTEM', -qty)):
            self.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
            self.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
            self.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                              (txn_id, next_seq, acct, inst, d))

    def _refresh_reactive_depots_locked(self) -> None:
        """
        Reactive depots. Caller holds self.lock.

        Gross depot flows since the last refresh come from the ledger (trade
        and auction settlements only), so a depot that bought and sold the
        same good in one round moves both its shelf and its hold. Once per
        round the shelf restocks and the hold drains by the drip rates.
        Quotes: one level each side; the ask rises as the shelf empties, the
        bid falls as the hold fills. Bid notional is capped at depot CR and
        ask size at depot stock, as in the static model.
        """
        if not self._reactive:
            self._reset_reactive_state()
        rx = self._reactive
        rows = self.conn.execute(
            "SELECT entry_id, agent_id, instrument, delta FROM ledger_entries "
            "WHERE entry_id > ? AND agent_id LIKE 'depot_%' "
            "AND (txn_id LIKE 'trade-%' OR txn_id LIKE 'auction-%')",
            (rx["ledger_cursor"],)).fetchall()
        for r in rows:
            st = r["agent_id"][len("depot_"):]
            key = (st, r["instrument"])
            if key in rx["shelf"]:
                if r["delta"] < 0:
                    rx["shelf"][key] = max(0, rx["shelf"][key] + r["delta"])
                else:
                    rx["hold"][key] += r["delta"]
        top = self.conn.execute("SELECT COALESCE(MAX(entry_id), 0) FROM ledger_entries").fetchone()[0]
        rx["ledger_cursor"] = max(rx["ledger_cursor"], top)

        round_num = getattr(self, 'current_round', 0)
        new_round = rx["refreshed_round"] != round_num
        rx["refreshed_round"] = round_num

        for st in STATIONS:
            depot_id = f"depot_{st}"
            cr_budget = max(0, self.get_balance(depot_id, 'CR'))
            for comm in COMMODITIES:
                key = (st, comm)
                if new_round:
                    # Production: the station adds real goods to its depot.
                    produced = min(rx["prod"][key], REACTIVE_TARGET - rx["shelf"][key])
                    if produced > 0:
                        rx["shelf"][key] += produced
                        self._depot_transfer_locked(f"depot-produce-{st}-{comm}-r{round_num}",
                                                    depot_id, comm, produced)
                    # Consumption: the station uses up goods its depot bought
                    # and pays the depot base price for them. This is what
                    # returns cash to importing depots; without it Earth and
                    # Ceres drained to ~0 CR by round ~1,100 and trading stopped
                    # (tools/economy_sim.py, 2,000-round run, 2026-09-22).
                    # Paid at the depot's own current bid, so a depot recovers
                    # roughly what it spent: paying base price made depots
                    # accumulate ~5M CR over 2,000 rounds.
                    consumed = min(rx["cons"][key], rx["hold"][key], max(0, self.get_balance(depot_id, comm)))
                    if consumed > 0:
                        spot_now = self.spatial.get_station_price(st, comm) if self.spatial else BASE_PRICES[st][comm]
                        unit = max(1, int(round(spot_now * 0.97 * (REACTIVE_TARGET / (REACTIVE_TARGET + rx["hold"][key])) ** REACTIVE_SKEW)))
                        txn = f"depot-consume-{st}-{comm}-r{round_num}"
                        self._depot_transfer_locked(txn, depot_id, comm, -consumed)
                        self._depot_transfer_locked(txn, depot_id, 'CR', consumed * unit)
                    rx["hold"][key] = max(0, rx["hold"][key] - rx["cons"][key])

                book = self.books.setdefault(st, {}).setdefault(comm, OrderBook(instrument=comm))
                book.bids = [o for o in book.bids if o.agent_id != depot_id]
                book.asks = [o for o in book.asks if o.agent_id != depot_id]
                self.conn.execute(
                    "DELETE FROM orders WHERE agent_id = ? AND station_id = ? AND instrument = ? AND status = 'open'",
                    (depot_id, st, comm))

                spot = self.spatial.get_station_price(st, comm) if self.spatial else BASE_PRICES[st][comm]
                shelf_ratio = REACTIVE_TARGET / max(rx["shelf"][key], REACTIVE_TARGET * 0.05)
                shelf_skew = getattr(self, "shelf_skew", REACTIVE_SHELF_SKEW)
                ask = max(2, int(round(spot * 1.03 * min(3.0, shelf_ratio ** shelf_skew))))
                bid = int(round(spot * 0.97 * (REACTIVE_TARGET / (REACTIVE_TARGET + rx["hold"][key])) ** REACTIVE_SKEW))
                bid = max(1, min(ask - 1, bid))

                if self.reactive_bands and hasattr(self, 'circuit_breaker') and self.circuit_breaker:
                    bands = self.circuit_breaker.get_bands(st, comm)
                    lo, hi = bands.get('lower_limit'), bands.get('upper_limit')
                    if lo is not None and hi is not None:
                        min_p, max_p = int(math.ceil(lo)), int(math.floor(hi))
                        if max_p > min_p:
                            ask = max(min_p + 1, min(ask, max_p))
                            bid = max(min_p, min(bid, ask - 1))

                ask_qty = min(rx["shelf"][key], max(0, self.get_balance(depot_id, comm)))
                bid_qty = min(max(0, REACTIVE_TARGET - rx["hold"][key]), cr_budget // bid if bid > 0 else 0)
                cr_budget -= bid_qty * bid

                seq = self.current_seq
                for side, price, qty in (('bid', bid, bid_qty), ('ask', ask, ask_qty)):
                    if qty <= 0:
                        continue
                    oid = f"{depot_id}-{comm.lower()}-{side}-r{round_num}-rx"
                    order = Order(order_id=oid, agent_id=depot_id, instrument=comm, side=side,
                                  qty=qty, limit_price=price, seq_seen=seq)
                    if side == 'bid':
                        book._insert_bid(order)
                    else:
                        book._insert_ask(order)
                    self.conn.execute("""
                        INSERT OR REPLACE INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, resolved_seq, filled_qty, station_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, 0, ?)
                    """, (oid, depot_id, comm, side, qty, price, seq, st))

    def get_depot_summary(self) -> Dict[str, Any]:
        """Return summary of all station depot quotes and depths."""
        res = {'depots_enabled': self.depots_enabled, 'depot_model': self.depot_model,
               'band_pct': self.band_pct, 'shelf_skew': getattr(self, 'shelf_skew', REACTIVE_SHELF_SKEW),
               'stations': {}}
        for st in STATIONS:
            res['stations'][st] = {}
            for comm in COMMODITIES:
                spot = self.spatial.get_station_price(st, comm) if self.spatial else BASE_PRICES[st][comm]
                book = self.books.get(st, {}).get(comm)
                depot_bids = [o for o in (book.bids if book else []) if o.agent_id == f"depot_{st}"]
                depot_asks = [o for o in (book.asks if book else []) if o.agent_id == f"depot_{st}"]
                res['stations'][st][comm] = {
                    'spot_price': spot,
                    'best_bid': depot_bids[0].limit_price if depot_bids else None,
                    'best_ask': depot_asks[0].limit_price if depot_asks else None,
                    'bid_depth': sum(o.remaining_qty for o in depot_bids),
                    'ask_depth': sum(o.remaining_qty for o in depot_asks),
                }
        return res