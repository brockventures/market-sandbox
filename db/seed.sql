-- Phase 1 genesis seed: flat identical endowment, correctness-testing
-- baseline. Asymmetric distributions are Phase 2 (see docs/ledger-schema.md).
--
-- Every genesis balance is logged as a real ledger transaction, funded from
-- a SYSTEM treasury account, so the conservation invariant holds from
-- seq 0 rather than being invisible to the audit. SYSTEM is the one
-- designated exception to the no-negative-balance invariant; see
-- docs/ledger-schema.md.

INSERT INTO book_events (seq, kind, payload) VALUES
  (0, 'floor_open', '{"note":"genesis, phase 1: orbital supply requisition terminal"}');

INSERT INTO accounts (agent_id, instrument, balance) VALUES
  ('SYSTEM', 'CR',   -30000),
  ('SYSTEM', 'FRAG',  -3000),
  ('amos',   'CR',    10000),
  ('amos',   'FRAG',   1000),
  ('marvin', 'CR',    10000),
  ('marvin', 'FRAG',   1000),
  ('zero',   'CR',    10000),
  ('zero',   'FRAG',   1000);

-- Grouped by txn_id so SUM(delta) = 0 holds per genesis transaction:
-- genesis-cr:   -30000 + 10000*3 = 0
-- genesis-frag:  -3000  + 1000*3  = 0
INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES
  ('genesis-cr',   0, 'SYSTEM', 'CR',   -30000),
  ('genesis-cr',   0, 'amos',   'CR',    10000),
  ('genesis-cr',   0, 'marvin', 'CR',    10000),
  ('genesis-cr',   0, 'zero',   'CR',    10000),
  ('genesis-frag', 0, 'SYSTEM', 'FRAG',  -3000),
  ('genesis-frag', 0, 'amos',   'FRAG',   1000),
  ('genesis-frag', 0, 'marvin', 'FRAG',   1000),
  ('genesis-frag', 0, 'zero',   'FRAG',   1000);

-- Standing invariant checks, post-genesis. Both must always return zero rows.
-- SELECT txn_id FROM ledger_entries GROUP BY txn_id HAVING SUM(delta) != 0;
-- SELECT * FROM accounts WHERE balance < 0 AND agent_id != 'SYSTEM';
