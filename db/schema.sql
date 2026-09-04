-- AGORA ledger schema (SQLite).
-- Double-entry, single writer, gap-free referee-assigned sequence.
-- See docs/ledger-schema.md for invariants and rationale.

-- One row per (agent, instrument). Cash is an instrument like any other.
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
    kind        TEXT NOT NULL CHECK (kind IN ('order','trade','floor_open','floor_close')),
    payload     TEXT NOT NULL,        -- JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
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
