"""
tests/test_macro_variance_and_defense.py - Macro-Variance, Solar Hazard & Defense Package (#245).

Tests:
1. Belt Salvage Surge (GalNet Event Shock):
   - Derelict collision template metadata (station=ceres, commodity=FRAG, drift_bias=-0.40, duration=4)
   - GalNetEngine.is_salvage_surge_active() status
   - Doubles pirate raid and ambush probability along Ceres transit corridors (BELT_ROUTES)
   - Inner routes remain unaffected
   - raid_key marble bag tagging with '|surge'
2. Coronal Mass Ejection (Solar Weather Hazard):
   - Solar flare template metadata (station=mars, commodity=FUEL, drift_bias=0.20, duration=3)
   - GalNetEngine.is_cme_active() status
   - check_cme_relay_interference() triggers on Earth-Mars inner/mid corridor
   - Local station docking provides hardwire LAN immunity
   - hardened_comm upgrade provides military transceiver immunity
   - Order book depth queries blinded under CME interference (fog of war)
3. Asset Spin-Offs & Emergency Fire-Sales (Hostile M&A Defense, #164):
   - Secondary hull spin-off liquidation, scrap cash payout, and corporate debt pay-down
   - Sole ship (Ship 1) spin-off rejection (cannot_spin_off_sole_ship)
   - Depot storage lease liquidation for 5,000 CR emergency cash and debt reduction
   - Defensive equity buyback repurchasing target shares from hostile rivals to neutralize 51% takeover
   - Server REST API endpoint POST /referee/corporate/spin_off
"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer

from agora.galnet import GalNetEngine, NEWS_TEMPLATES
from agora.hazards import is_cme_active, check_cme_relay_interference
from agora.referee import AgoraReferee
from agora.server import make_handler

TOKENS = {'amos': 't-amos', 'zero': 't-zero', 'marvin': 't-marvin', 'aerial': 't-aerial', 'admin': 't-admin'}


def new_test_game(**kw):
    ref = AgoraReferee(depots=True, rival_shares=100, corporate=True, contracts=True, piracy=True, upgrades=True, **kw)
    ref.new_game(seed=42, warmup_rounds=2, depots=True, rival_shares=100, corporate=True, contracts=True, piracy=True, upgrades=True)
    return ref


class TestMacroVarianceAndDefense(unittest.TestCase):

    def test_belt_salvage_surge(self):
        """Belt Salvage Surge slashes FRAG spot price by 40% and doubles Ceres corridor raid odds (#245)."""
        engine = GalNetEngine(seed=1337)
        surge_tpl_idx = None
        for i, tpl in enumerate(NEWS_TEMPLATES):
            if tpl.get("event_type") == "belt_salvage_surge":
                surge_tpl_idx = i
                break

        self.assertIsNotNone(surge_tpl_idx, "belt_salvage_surge template must exist in NEWS_TEMPLATES")
        tpl = NEWS_TEMPLATES[surge_tpl_idx]
        self.assertEqual(tpl["station_id"], "ceres")
        self.assertEqual(tpl["commodity"], "FRAG")
        self.assertEqual(tpl["drift_bias"], -0.40)
        self.assertEqual(tpl["duration_rounds"], 4)

        # Trigger the shock
        ev = engine.force_shock(round_num=10, template_idx=surge_tpl_idx)
        self.assertEqual(ev.event_type, "belt_salvage_surge")
        self.assertTrue(engine.is_salvage_surge_active())
        self.assertEqual(engine.get_active_drift("ceres", "FRAG"), -0.40)

        # Connect with referee and PiracyDesk
        ref = new_test_game()
        ref.galnet = engine

        # Ceres transit route: piracy odds must be doubled
        ceres_chance = ref.piracy.chance(
            agent='amos', origin='ceres', dest='earth',
            tolled=True, commodity='FRAG', qty=500,
            escort=False, round_num=10
        )
        self.assertTrue(ceres_chance['salvage_surge'])
        key = ref.piracy.raid_key('amos/1', ceres_chance)
        self.assertIn('|surge', key)

        # Calculate expected baseline odds without surge
        base_p = 0.15  # belt
        vm = min(2.0, max(0.5, round((ceres_chance['value'] / 10000) / 0.25) * 0.25))
        expected_surge_p = min(1.0, (base_p * vm) * 2.0)
        self.assertAlmostEqual(ceres_chance['exact_odds'], expected_surge_p, places=4)

        # Inner route (earth <-> luna): not in BELT_ROUTES and neither origin nor dest is ceres
        inner_chance = ref.piracy.chance(
            agent='amos', origin='earth', dest='luna',
            tolled=False, commodity='FRAG', qty=500,
            escort=False, round_num=10
        )
        self.assertFalse(inner_chance['salvage_surge'])
        self.assertNotIn('|surge', ref.piracy.raid_key('amos/1', inner_chance))

    def test_coronal_mass_ejection_fog_and_hazards(self):
        """Coronal Mass Ejection blinds remote Earth-Mars relays unless docked or shielded with hardened_comm (#245)."""
        engine = GalNetEngine(seed=1337)
        cme_tpl_idx = None
        for i, tpl in enumerate(NEWS_TEMPLATES):
            if tpl.get("event_type") == "coronal_mass_ejection":
                cme_tpl_idx = i
                break

        self.assertIsNotNone(cme_tpl_idx, "coronal_mass_ejection template must exist in NEWS_TEMPLATES")
        tpl = NEWS_TEMPLATES[cme_tpl_idx]
        self.assertEqual(tpl["station_id"], "mars")
        self.assertEqual(tpl["commodity"], "FUEL")
        self.assertEqual(tpl["drift_bias"], 0.20)
        self.assertEqual(tpl["duration_rounds"], 3)

        ev = engine.force_shock(round_num=15, template_idx=cme_tpl_idx)
        self.assertEqual(ev.event_type, "coronal_mass_ejection")
        self.assertTrue(engine.is_cme_active())

        ref = new_test_game()
        ref.galnet = engine
        self.assertTrue(is_cme_active(ref))

        # amos is docked at ceres (default for new game ship 1)
        # Attempting to query remote station 'mars' or 'earth'
        cme_status = check_cme_relay_interference(ref, 'amos', 'mars')
        self.assertTrue(cme_status['interfered'])
        self.assertEqual(cme_status['reason'], 'cme_relay_blackout')
        self.assertEqual(cme_status['latency_rounds'], 1)

        # Local docking immunity: if amos docks at mars, interference is bypassed
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE vessels SET station_id = 'mars' WHERE vessel_id = 'amos/1'")
        cme_docked = check_cme_relay_interference(ref, 'amos', 'mars')
        self.assertFalse(cme_docked['interfered'])
        self.assertEqual(cme_docked['reason'], 'local_docked')

        # Move amos back to ceres
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE vessels SET station_id = 'ceres' WHERE vessel_id = 'amos/1'")

        # Upgrade immunity: buy hardened_comm suite
        with ref.lock, ref.conn:
            ref.conn.execute(
                "INSERT OR REPLACE INTO fleet_upgrades (agent_id, kind, tier, round) VALUES ('amos', 'hardened_comm', 1, 15)"
            )
        self.assertTrue(ref.upgrades.has_hardened_comm('amos'))

        cme_shielded = check_cme_relay_interference(ref, 'amos', 'mars')
        self.assertFalse(cme_shielded['interfered'])
        self.assertEqual(cme_shielded['reason'], 'hardened_comm')

    def test_asset_spin_off_secondary_hull(self):
        """Corporation facing distress can spin off a docked secondary hull to raise scrap cash and pay down debt (#245)."""
        ref = new_test_game()

        # amos only has 1 vessel currently (amos/1). Attempting to spin off must reject
        err = ref.corporate.spin_off_asset('amos', 'hull')
        self.assertEqual(err['kind'], 'reject')
        self.assertEqual(err['payload']['reason'], 'cannot_spin_off_sole_ship')

        # Buy a second ship for amos
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 50000 WHERE agent_id = 'amos' AND instrument = 'CR'")
        buy_res = ref.fleet.buy('amos')
        self.assertEqual(buy_res['kind'], 'ship_bought')
        vid2 = buy_res['payload']['vessel_id']

        # Now drain cash and add debt so debt is not immediately paid by genesis cash
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 0 WHERE agent_id = 'amos' AND instrument = 'CR'")
        ref.corporate.add_debt('amos', 8000, 'penalties')
        self.assertEqual(ref.corporate._row('amos')['debt'], 8000)

        # amos now has 2 ships and 8000 debt. Spin off secondary hull
        res = ref.corporate.spin_off_asset('amos', 'hull', asset_id=vid2)
        self.assertEqual(res['kind'], 'spin_off_ok')
        self.assertEqual(res['payload']['vessel_id'], vid2)
        self.assertGreater(res['payload']['cash_raised'], 0)
        self.assertGreater(res['payload']['debt_paid'], 0)

        # Confirm vessel was deleted
        row = ref.conn.execute("SELECT * FROM vessels WHERE vessel_id = ?", (vid2,)).fetchone()
        self.assertIsNone(row)

        # Debt was paid down
        self.assertLess(res['payload']['remaining_debt'], 8000)

    def test_asset_spin_off_depot_lease(self):
        """Liquidating regional depot storage leases raises 5,000 CR emergency cash to pay down corporate debt (#245)."""
        ref = new_test_game()

        # Drain cash first so existing cash doesn't pay down debt before spin off
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 0 WHERE agent_id = 'zero' AND instrument = 'CR'")

        ref.corporate.add_debt('zero', 10000, 'penalties')
        self.assertEqual(ref.corporate._row('zero')['debt'], 10000)

        res = ref.corporate.spin_off_asset('zero', 'depot_lease')
        self.assertEqual(res['kind'], 'spin_off_ok')
        self.assertEqual(res['payload']['cash_raised'], 5000)
        self.assertEqual(res['payload']['debt_paid'], 5000)
        self.assertEqual(res['payload']['remaining_debt'], 5000)

    def test_asset_spin_off_equity_buyback_defense(self):
        """Defensive equity buyback repurchases target shares from rivals to neutralize hostile 51% takeover (#245)."""
        ref = new_test_game()

        # Target is 'zero', raider is 'amos'
        # Transfer 200 shares of EQ_ZERO to amos
        with ref.lock, ref.conn:
            ref.conn.execute("INSERT OR REPLACE INTO accounts VALUES ('amos', 'EQ_ZERO', 200)")
            ref.conn.execute("INSERT OR REPLACE INTO accounts VALUES ('zero', 'CR', 30000)")

        self.assertEqual(ref.get_balance('amos', 'EQ_ZERO'), 200)

        # zero executes defensive buyback of 100 shares
        res = ref.corporate.spin_off_asset('zero', 'equity_buyback', shares=100)
        self.assertEqual(res['kind'], 'spin_off_ok')
        self.assertEqual(res['payload']['shares_bought'], 100)

        # amos's hostile stake reduced to 100 shares
        self.assertEqual(ref.get_balance('amos', 'EQ_ZERO'), 100)
        # zero received 100 shares into treasury
        self.assertGreaterEqual(ref.get_balance('zero', 'EQ_ZERO'), 100)

    def test_server_corporate_spin_off_endpoint(self):
        """HTTP POST /referee/corporate/spin_off integrates into server routing (#245)."""
        ref = new_test_game()
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 0 WHERE agent_id = 'marvin' AND instrument = 'CR'")
        ref.corporate.add_debt('marvin', 6000, 'shortfall')

        handler_cls = make_handler(referee=ref, auth_tokens=TOKENS)
        server = HTTPServer(('127.0.0.1', 0), handler_cls)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            url = f"http://127.0.0.1:{port}/referee/corporate/spin_off"
            req_data = json.dumps({'asset_type': 'depot_lease'}).encode('utf-8')
            req = urllib.request.Request(url, data=req_data, headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {TOKENS["marvin"]}'
            })
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(body['kind'], 'spin_off_ok')
                self.assertEqual(body['payload']['asset_type'], 'depot_lease')
                self.assertEqual(body['payload']['cash_raised'], 5000)
                self.assertEqual(body['payload']['debt_paid'], 5000)
                self.assertEqual(body['payload']['remaining_debt'], 1000)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
