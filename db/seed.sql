-- Phase 1 genesis seed: flat identical endowment, correctness-testing
-- baseline. Asymmetric distributions are Phase 2 (see docs/ledger-schema.md).
--
-- This file seeds only fleet_roster and the opening book event.
-- AgoraReferee._seed_genesis_from_roster() (agora/referee.py) is what
-- actually turns roster rows into accounts/ledger_entries/vessel_locations,
-- so the roster is the single source of truth for both first boot and any
-- later POST /referee/admin/reset -- adding or changing a fleet is a
-- fleet_roster edit (via POST /referee/admin/fleets), not a fixture edit.

INSERT INTO book_events (seq, kind, payload) VALUES
  (0, 'floor_open', '{"note":"genesis, phase 1: orbital supply requisition terminal"}');

INSERT INTO fleet_roster (agent_id, display_name, home_station, genesis_cr, genesis_frag, genesis_fuel) VALUES
  ('amos',   'Atlantean Paperclip Manufacturing', 'ceres', 10000, 1000, 500),
  ('marvin', 'Ballistic Liquidation Co.',          'ceres', 10000, 1000, 500),
  ('zero',   'Apex Vector Arbitrage',              'ceres', 10000, 1000, 500),
  ('aerial', 'Zenith Drift Overwatch',              'ceres', 10000, 1000, 500);

-- Standing invariant checks, post-genesis. Both must always return zero rows.
-- SELECT txn_id FROM ledger_entries GROUP BY txn_id HAVING SUM(delta) != 0;
-- SELECT * FROM accounts WHERE balance < 0 AND agent_id != 'SYSTEM';
