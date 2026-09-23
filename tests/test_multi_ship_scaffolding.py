"""
Unit tests for Issue #175 Track 4 PR 1: Multi-Ship Fleets Scaffolding.
Verifies vessels DDL, transits.vessel_id column backfill,
fleet_roster agent_id '/' validation, and backward-compatible shims.
"""

import sqlite3
import unittest
from agora.referee import AgoraReferee


class TestMultiShipScaffolding(unittest.TestCase):
    def test_schema_vessels_and_constraints(self):
        """Verify vessels table schema and fleet_roster slash rejection constraint."""
        ref = AgoraReferee()
        cur = ref.conn.cursor()

        # Check vessels table columns
        cols = {r[1]: r[2] for r in cur.execute("PRAGMA table_info(vessels)").fetchall()}
        self.assertIn('vessel_id', cols)
        self.assertIn('agent_id', cols)
        self.assertIn('name', cols)
        self.assertIn('station_id', cols)
        self.assertIn('docked_since', cols)
        self.assertIn('bought_round', cols)
        self.assertIn('cost', cols)
        self.assertIn('status', cols)

        # Check transits vessel_id column
        t_cols = {r[1]: r[2] for r in cur.execute("PRAGMA table_info(transits)").fetchall()}
        self.assertIn('vessel_id', t_cols)

        # Check fleet_roster constraint rejects '/'
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("""
                INSERT INTO fleet_roster (agent_id, display_name, home_station, genesis_cr, genesis_frag, genesis_fuel)
                VALUES ('corp/1', 'Invalid Sub-Account Fleet', 'ceres', 10000, 1000, 500)
            """)

    def test_genesis_seeds_initial_vessels(self):
        """Every corp gets <corp>/1 seeded at genesis at its home station."""
        ref = AgoraReferee()
        vessels = ref.get_vessels()
        self.assertGreaterEqual(len(vessels), 4)

        v_by_agent = {v['agent_id']: v for v in vessels}
        for agent in ('amos', 'marvin', 'zero', 'aerial'):
            self.assertIn(agent, v_by_agent)
            v = v_by_agent[agent]
            self.assertEqual(v['vessel_id'], f"{agent}/1")
            self.assertEqual(v['station_id'], 'ceres')
            self.assertEqual(v['status'], 'docked')
            self.assertEqual(v['cost'], 0)

        # Filter by agent_id
        amos_vessels = ref.get_vessels('amos')
        self.assertEqual(len(amos_vessels), 1)
        self.assertEqual(amos_vessels[0]['vessel_id'], 'amos/1')

    def test_transit_lifecycle_updates_vessel(self):
        """Transit initiation sets vessel status to in_transit; arrival restores docked."""
        ref = AgoraReferee()
        # Seed fuel and frag for transit test
        ref.conn.execute("UPDATE accounts SET balance = 500 WHERE agent_id = 'zero' AND instrument = 'FUEL'")
        ref.conn.execute("UPDATE accounts SET balance = 100 WHERE agent_id = 'zero' AND instrument = 'FRAG'")
        ref.conn.commit()

        res = ref.initiate_transit('zero', 'mars', 'FRAG', 10)
        self.assertEqual(res['status'], 'in_transit')
        self.assertEqual(res['payload']['vessel_id'], 'zero/1')
        t_id = res['payload']['transit_id']

        # Transits table should store vessel_id
        row = ref.conn.execute("SELECT vessel_id, status FROM transits WHERE transit_id = ?", (t_id,)).fetchone()
        self.assertEqual(row['vessel_id'], 'zero/1')
        self.assertEqual(row['status'], 'in_transit')

        # Vessels table should reflect in_transit
        vessel = ref.get_vessels('zero')[0]
        self.assertEqual(vessel['status'], 'in_transit')
        self.assertEqual(vessel['station_id'], 'in_transit')

        # Step rounds until arrival
        for _ in range(10):
            ref.step_round()

        # Arrival settles vessel
        vessel_arrived = ref.get_vessels('zero')[0]
        self.assertEqual(vessel_arrived['status'], 'docked')
        self.assertEqual(vessel_arrived['station_id'], 'mars')

    def test_asymmetric_dispersal_and_reset_syncs_vessels(self):
        """Reset and asymmetric initialization correctly update vessels table."""
        ref = AgoraReferee(asymmetric=True)
        vessels = {v['agent_id']: v['station_id'] for v in ref.get_vessels()}
        self.assertEqual(vessels.get('zero'), 'earth')
        self.assertEqual(vessels.get('amos'), 'ceres')
        self.assertEqual(vessels.get('marvin'), 'mars')
        self.assertEqual(vessels.get('aerial'), 'luna')

        # Reset preserves vessel syncing
        ref.reset_to_genesis(asymmetric=True)
        vessels_reset = {v['agent_id']: v['station_id'] for v in ref.get_vessels()}
        self.assertEqual(vessels_reset.get('zero'), 'earth')
        self.assertEqual(vessels_reset.get('amos'), 'ceres')

        # Invariants hold
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid)
        self.assertEqual(len(errors), 0)


if __name__ == '__main__':
    unittest.main()
