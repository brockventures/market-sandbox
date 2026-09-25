"""
tests/test_moral_hazard_directives.py - tests for moral hazard, corporate negligence
directives, encrypted audit dossiers, and activist short-selling leaks (#248).
"""

import json
import unittest

from agora.referee import AgoraReferee
from agora.corporate import NEGLIGENCE_DIRECTIVES


def _make_referee(corporate: bool = True, dividends: bool = True):
    ref = AgoraReferee(depots=True, rival_shares=100, corporate=corporate, dividends=dividends, contracts=True, events=True)
    ref.new_game(seed=4, warmup_rounds=2, depots=True, rival_shares=100, corporate=corporate, dividends=dividends, contracts=True, events=True)
    return ref


class TestMoralHazardDirectives(unittest.TestCase):
    def test_negligence_directives_toggling(self):
        ref = _make_referee(corporate=True)
        corp = ref.corporate

        # Toggling directive on an active fleet
        res = corp.set_negligence_directive("amos", "atmosphere_optimization", enabled=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["directive_id"], "atmosphere_optimization")
        self.assertTrue(res["enabled"])

        # Check active directives
        info = corp.get_negligence_directives("amos")
        self.assertIn("atmosphere_optimization", info["active_directives"])
        self.assertEqual(info["fuel_discount"], 0.35)
        self.assertEqual(info["opex_discount"], 0.35)
        self.assertEqual(info["dividend_multiplier"], 1.5)

        # Toggle second directive
        res2 = corp.set_negligence_directive("amos", "agile_thrusters", enabled=True)
        self.assertTrue(res2["ok"])
        info2 = corp.get_negligence_directives("amos")
        self.assertEqual(info2["fuel_discount"], 0.40)  # max of 0.35 and 0.40

        # Toggle off
        res3 = corp.set_negligence_directive("amos", "atmosphere_optimization", enabled=False)
        self.assertTrue(res3["ok"])
        info3 = corp.get_negligence_directives("amos")
        self.assertNotIn("atmosphere_optimization", info3["active_directives"])
        self.assertIn("agile_thrusters", info3["active_directives"])

    def test_transit_fuel_and_toll_discounts(self):
        ref = _make_referee(corporate=True)

        # Give amos lots of fuel and cash at ceres
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 50000 WHERE agent_id = 'amos' AND instrument = 'CR'")
            ref.conn.execute("UPDATE accounts SET balance = 1000 WHERE agent_id = 'amos' AND instrument = 'FUEL'")

        # Baseline route fuel burn
        from agora.spatial import get_route
        route = get_route("ceres", "earth", ref.current_round)
        base_fuel = route["fuel"]

        # Without directives
        burn_normal = ref.upgrades.engine_fuel("amos", base_fuel)

        # Enable agile thrusters (40% discount)
        ref.corporate.set_negligence_directive("amos", "agile_thrusters", enabled=True)
        fuel_disc = ref.corporate.get_fuel_burn_discount("amos")
        self.assertEqual(fuel_disc, 0.40)

        expected_fuel = max(1, int(burn_normal * 0.60))

        # Initiate transit with vessel
        vessels = ref.fleet.ships("amos")
        self.assertTrue(len(vessels) > 0)
        v_id = vessels[0]["vessel_id"]

        tx = ref.initiate_transit("amos", destination="earth", commodity="FRAG", cargo_qty=10, vessel_id=v_id)
        self.assertEqual(tx.get("v"), 1)
        self.assertEqual(tx["payload"]["fuel_burned"], expected_fuel)

    def test_compounding_off_balance_sheet_liability_escrow(self):
        ref = _make_referee(corporate=True)
        corp = ref.corporate

        # Activate two directives: atmosphere_optimization (150 CR/rd) + agile_thrusters (200 CR/rd) = 350 CR/rd
        corp.set_negligence_directive("aerial", "atmosphere_optimization", enabled=True)
        corp.set_negligence_directive("aerial", "agile_thrusters", enabled=True)

        # Initial escrow is 0
        escrow0 = corp.get_negligence_directives("aerial")
        self.assertEqual(escrow0["liability_escrow_cr"], 0)
        self.assertEqual(escrow0["rounds_active"], 0)

        # Step rounds under lock
        with ref.lock, ref.conn:
            corp.step_locked(1)
        escrow1 = corp.get_negligence_directives("aerial")
        self.assertEqual(escrow1["liability_escrow_cr"], 350)
        self.assertEqual(escrow1["rounds_active"], 1)

        with ref.lock, ref.conn:
            corp.step_locked(2)
        escrow2 = corp.get_negligence_directives("aerial")
        self.assertEqual(escrow2["liability_escrow_cr"], 700)
        self.assertEqual(escrow2["rounds_active"], 2)

        # Liability escrow is off-balance-sheet: does NOT reduce equity summary NAV
        eq_summary = ref.get_equity_summary()
        self.assertGreater(eq_summary["EQ_AERL"]["spot_price"], 0)

    def test_audit_dossier_compilation(self):
        ref = _make_referee(corporate=True)
        corp = ref.corporate
        covert = ref.covert

        # Target sets directives
        corp.set_negligence_directive("marvin", "zero_cr_hazard", enabled=True)
        with ref.lock, ref.conn:
            corp.step_locked(1)

        # Actor has no wiretap and insufficient cash -> fails
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 0 WHERE agent_id = 'zero' AND instrument = 'CR'")
        d_fail = covert.compile_audit_dossier("zero", "marvin")
        self.assertEqual(d_fail.get("kind"), "reject")

        # Actor pays 500 CR audit fee
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 1000 WHERE agent_id = 'zero' AND instrument = 'CR'")
        d_ok = covert.compile_audit_dossier("zero", "marvin")
        self.assertTrue(d_ok["ok"])
        self.assertEqual(d_ok["actor"], "zero")
        self.assertEqual(d_ok["target"], "marvin")
        self.assertIn("zero_cr_hazard", d_ok["directives"])
        self.assertEqual(d_ok["liability_escrow_cr"], 180)
        self.assertEqual(d_ok["rounds_active"], 1)

        # Verify dossier listing
        dossiers = covert.get_dossiers("zero")
        self.assertEqual(len(dossiers), 1)
        self.assertEqual(dossiers[0]["dossier_id"], d_ok["dossier_id"])

    def test_whistleblower_leak_and_treble_fine(self):
        ref = _make_referee(corporate=True)
        corp = ref.corporate

        # Set directive and run 3 rounds of liability
        corp.set_negligence_directive("amos", "agile_thrusters", enabled=True)
        for r in range(1, 4):
            with ref.lock, ref.conn:
                corp.step_locked(r)

        # Compile dossier
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 5000 WHERE agent_id = 'zero' AND instrument = 'CR'")
        dossier = corp.compile_audit_dossier("zero", "amos")
        d_id = dossier["dossier_id"]
        base_liability = dossier["liability_escrow_cr"]  # 200 * 3 = 600

        # Target cash before leak
        amos_cr_before = ref.get_balance("amos", "CR")
        zero_cr_before = ref.get_balance("zero", "CR")

        # Leak dossier
        leak_res = corp.leak_audit_dossier("zero", d_id)
        self.assertTrue(leak_res["ok"])
        self.assertEqual(leak_res["target"], "amos")
        expected_treble = base_liability * 3  # 1800
        self.assertEqual(leak_res["treble_fine"], expected_treble)
        expected_bounty = int(expected_treble * 0.20)  # 360
        self.assertEqual(leak_res["whistleblower_bounty"], expected_bounty)

        # Cash balances updated correctly
        self.assertEqual(ref.get_balance("amos", "CR"), amos_cr_before - expected_treble)
        self.assertEqual(ref.get_balance("zero", "CR"), zero_cr_before + expected_bounty)

        # Dynamic stock shock: 3 rounds active -> -0.12 - 0.01 * 2 = -0.14 (-14%)
        self.assertAlmostEqual(leak_res["stock_shock_pct"], -0.14, delta=0.001)

        # Liability escrow reset
        escrow = corp.get_negligence_directives("amos")
        self.assertEqual(escrow["liability_escrow_cr"], 0)
        self.assertEqual(escrow["rounds_active"], 0)
        self.assertTrue(escrow["leaked"])

    def test_activist_short_selling_loop(self):
        ref = _make_referee(corporate=True)
        corp = ref.corporate

        # Aerial cuts corners on atmosphere optimization
        corp.set_negligence_directive("aerial", "atmosphere_optimization", enabled=True)
        for r in range(1, 6):
            with ref.lock, ref.conn:
                corp.step_locked(r)

        # Zero borrows EQ_AERL to initiate activist short
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = 20000 WHERE agent_id = 'zero' AND instrument = 'CR'")

        summary_before = ref.get_equity_summary()
        aerial_spot_before = summary_before["EQ_AERL"]["spot_price"]
        borrow_shares = 50

        loan_res = ref.borrow_equity("zero", "EQ_AERL", borrow_shares)
        self.assertTrue(loan_res["ok"])
        loan_id = loan_res["loan_id"]

        # Zero holds 50 borrowed shares of EQ_AERL
        self.assertEqual(ref.get_balance("zero", "EQ_AERL"), 100 + borrow_shares)

        # Zero sells the borrowed shares at spot price into market
        cash_from_short = int(borrow_shares * aerial_spot_before)
        with ref.lock, ref.conn:
            # Simulate market sell fill
            ref.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = 'zero' AND instrument = 'EQ_AERL'", (borrow_shares,))
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'zero' AND instrument = 'CR'", (cash_from_short,))

        # Compile audit dossier on Aerial
        dossier = corp.compile_audit_dossier("zero", "aerial")
        self.assertTrue(dossier["ok"])

        # Leak dossier to GalNet
        leak_res = corp.leak_audit_dossier("zero", dossier["dossier_id"])
        self.assertTrue(leak_res["ok"])

        # Price craters dynamically (5 rounds active -> -16% drop)
        self.assertAlmostEqual(leak_res["stock_shock_pct"], -0.16, delta=0.001)

        # In exchange, spot price drops
        from agora.exchange import EXCHANGE_STATION
        crashed_spot = ref.last_prices.get((EXCHANGE_STATION, "EQ_AERL"), aerial_spot_before * (1 + leak_res["stock_shock_pct"]))
        self.assertLess(crashed_spot, aerial_spot_before)

        # Zero buys back 50 shares at crashed price
        cost_to_cover = int(borrow_shares * crashed_spot)
        self.assertLess(cost_to_cover, cash_from_short)  # Profit on short!

        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE accounts SET balance = balance - ? WHERE agent_id = 'zero' AND instrument = 'CR'", (cost_to_cover,))
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = 'zero' AND instrument = 'EQ_AERL'", (borrow_shares,))

        # Zero closes loan by returning borrowed shares to lender
        close_res = ref.return_equity_loan("zero", loan_id)
        self.assertTrue(close_res["ok"])

        # Net trading profit
        short_profit = cash_from_short - cost_to_cover
        self.assertGreater(short_profit, 0)
        self.assertGreater(leak_res["whistleblower_bounty"], 0)

    def test_hull_scuttling_moral_hazard(self):
        ref = _make_referee(corporate=True)
        corp = ref.corporate

        vessels = ref.fleet.ships("amos")
        self.assertTrue(len(vessels) > 0)
        v_id = vessels[0]["vessel_id"]
        cost = vessels[0]["cost"] or 1000

        cr_before = ref.get_balance("amos", "CR")
        res = corp.scuttle_vessel("amos", v_id)
        self.assertTrue(res["ok"])
        self.assertEqual(res["payout_cr"], max(1500, int(cost * 1.5)))
        self.assertEqual(ref.get_balance("amos", "CR"), cr_before + res["payout_cr"])

        # Vessel is destroyed
        vessels_after = ref.fleet.ships("amos")
        self.assertFalse(any(v["vessel_id"] == v_id for v in vessels_after))

        # Liability escrow added for suspicious claim
        escrow = corp.get_negligence_directives("amos")
        self.assertEqual(escrow["liability_escrow_cr"], 300)

    def test_http_endpoints_moral_hazard(self):
        import threading
        import urllib.request
        from http.server import HTTPServer
        from agora.server import make_handler

        ref = _make_referee(corporate=True)
        auth_tokens = {'amos': 'tok-amos', 'zero': 'tok-zero', 'admin': 'tok-admin'}
        handler_class = make_handler(ref, auth_tokens=auth_tokens)
        server = HTTPServer(('127.0.0.1', 0), handler_class)
        port = server.server_port
        base_url = f'http://127.0.0.1:{port}'
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()

        try:
            def _post(path, data, token):
                req = urllib.request.Request(
                    f'{base_url}{path}',
                    data=json.dumps(data).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
                )
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode('utf-8'))

            def _get(path, token):
                req = urllib.request.Request(
                    f'{base_url}{path}',
                    headers={'Authorization': f'Bearer {token}'}
                )
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode('utf-8'))

            # 1. POST /referee/corporate/directive
            dir_res = _post('/referee/corporate/directive', {'directive_id': 'atmosphere_optimization', 'enabled': True}, 'tok-amos')
            self.assertTrue(dir_res['ok'])

            # 2. GET /referee/corporate/directives
            get_res = _get('/referee/corporate/directives', 'tok-amos')
            self.assertEqual(get_res['status'], 'ok')
            self.assertIn('atmosphere_optimization', get_res['active_directives'])

            # Step 2 rounds to accumulate liability
            with ref.lock, ref.conn:
                ref.corporate.step_locked(1)
                ref.corporate.step_locked(2)

            # Give zero credits for audit
            with ref.lock, ref.conn:
                ref.conn.execute("UPDATE accounts SET balance = 5000 WHERE agent_id = 'zero' AND instrument = 'CR'")

            # 3. POST /referee/covert/dossier
            dossier_res = _post('/referee/covert/dossier', {'target': 'amos'}, 'tok-zero')
            self.assertTrue(dossier_res['ok'])
            d_id = dossier_res['dossier_id']

            # 4. GET /referee/covert/dossiers
            dossiers_res = _get('/referee/covert/dossiers', 'tok-zero')
            self.assertEqual(dossiers_res['status'], 'ok')
            self.assertGreaterEqual(len(dossiers_res['dossiers']), 1)

            # 5. POST /referee/covert/leak
            leak_res = _post('/referee/covert/leak', {'dossier_id': d_id}, 'tok-zero')
            self.assertTrue(leak_res['ok'])
            self.assertGreater(leak_res['treble_fine'], 0)

            # 6. POST /referee/corporate/scuttle
            v_id = ref.fleet.ships('amos')[0]['vessel_id']
            scuttle_res = _post('/referee/corporate/scuttle', {'vessel_id': v_id}, 'tok-amos')
            self.assertTrue(scuttle_res['ok'])
            self.assertGreaterEqual(scuttle_res['payout_cr'], 1500)

        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
