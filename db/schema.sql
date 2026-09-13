-- AGORA / ATLAS ledger schema (SQLite).
-- Orbital Supply Requisition Terminal (Debris Fragments 'FRAG' vs Credits 'CR').
-- Double-entry, single writer, gap-free referee-assigned sequence.
-- See docs/ledger-schema.md for invariants and rationale.

-- One row per (agent, instrument). CR is a numeraire instrument like any other.
CREATE TABLE accounts (
    agent_id    TEXT NOT NULL,
    instrument  TEXT NOT NULL,
    balance     INTEGER NOT NULL DEFAULT 0,   -- fixed-point integer, never float
    PRIMARY KEY (agent_id, instrument)
);

-- Append-only. Every economic event is a set of rows sharing one txn_id
-- whose deltas net to zero.
CREATE TABLE ledger_entries (
    entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,     -- book seq at settlement
    agent_id    TEXT NOT NULL,
    instrument  TEXT NOT NULL,
    delta       INTEGER NOT NULL,     -- signed
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- The book itself: one row per state-changing event, seq assigned by the
-- referee only, never by a client.
CREATE TABLE book_events (
    seq         INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('order','trade','floor_open','floor_close','cancel','news','transit','transit_arrived','borrow','loan_closed','liquidation','distress','rescue','salvage','circuit_breaker_halt','circuit_breaker_reopen')),
    payload     TEXT NOT NULL,        -- JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Station price surface snapshots across Sol nodes.
CREATE TABLE station_prices (
    station_id  TEXT NOT NULL,
    commodity   TEXT NOT NULL,
    round       INTEGER NOT NULL,
    base_price  REAL NOT NULL,
    drift_bias  REAL NOT NULL DEFAULT 0.0,
    spot_price  REAL NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (station_id, commodity, round)
);

-- Cargo and fuel escrow during orbital transit.
CREATE TABLE transits (
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
    perishable      INTEGER NOT NULL DEFAULT 0,
    decay_rate      REAL NOT NULL DEFAULT 0.0,
    decayed_qty     INTEGER NOT NULL DEFAULT 0,
    toll_paid       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Current docked station or transit status per agent vessel.
CREATE TABLE vessel_locations (
    agent_id        TEXT PRIMARY KEY,
    station_id      TEXT NOT NULL,
    docked_since    INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Bilateral stock loans and short obligations.
CREATE TABLE equity_loans (
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
);

-- Distress beacons and wreck markers for stranded vessels.
CREATE TABLE distress_beacons (
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
);

-- Emergency Rescue RFQs.
CREATE TABLE rescue_rfqs (
    rfq_id          TEXT PRIMARY KEY,
    beacon_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    fuel_needed     INTEGER NOT NULL,
    max_reward_cr   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'expired', 'cancelled')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Competitive rival quotes for rescue fuel.
CREATE TABLE rescue_quotes (
    quote_id        TEXT PRIMARY KEY,
    rfq_id          TEXT NOT NULL,
    beacon_id       TEXT NOT NULL,
    rescuer_id      TEXT NOT NULL,
    fuel_offered    INTEGER NOT NULL,
    price_cr        INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected', 'expired')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Derelict cargo salvage claims.
CREATE TABLE salvage_claims (
    claim_id        TEXT PRIMARY KEY,
    beacon_id       TEXT NOT NULL,
    salvager_id     TEXT NOT NULL,
    cargo_claimed   TEXT NOT NULL,
    claim_round     INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Circuit breaker halts triggered by LULD breaches.
CREATE TABLE circuit_breaker_halts (
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
);

-- Orders as submitted, carrying the agent's belief about the book at
-- submission time.
CREATE TABLE orders (
    order_id     TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    instrument   TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('bid','ask')),
    qty          INTEGER NOT NULL CHECK (qty > 0),
    limit_price  INTEGER NOT NULL,
    seq_seen     INTEGER NOT NULL,
    submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    resolved_seq INTEGER,             -- null while open
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','filled','cancelled')),
    filled_qty   INTEGER NOT NULL DEFAULT 0,
    station_id   TEXT NOT NULL DEFAULT 'ceres',
    PRIMARY KEY (agent_id, order_id)
);

-- Standing invariant checks. Both must always return zero rows.
-- SELECT txn_id FROM ledger_entries GROUP BY txn_id HAVING SUM(delta) != 0;
-- SELECT * FROM accounts WHERE balance < 0 AND agent_id != 'SYSTEM';
--
-- No CHECK(balance >= 0) constraint on accounts: SYSTEM is a designated
-- exception (see seed.sql, docs/ledger-schema.md) and must be able to
-- carry the negative side of a genesis or future minting transaction.
-- Every other agent_id is expected to stay non-negative, enforced by the
-- referee at write time and audited by the query above, not by the schema.
