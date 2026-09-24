"""
tests/test_financial_trader_progression.py - Financial trader progression & market manipulation (#165).

Tests:
1. Algo Execution Desk:
   - Upgrade purchase & catalog gating
   - Reduced exchange transaction fees (0.5% -> 0.1%)
   - Priority order matching in NPC order flow ahead of standard resting orders
2. Level 2 Depth Telemetry:
   - Upgrade purchase & catalog gating
   - Bypasses spatial market fog across all stations
3. Espionage & Information Warfare:
   - GalNet Disinformation / Rumor Mill (plant bullish/bearish rumors, drift shocks, trace fines)
   - Insider Taps (early visibility into rival in-flight cargo landings 1 round before landing)
4. Endgame Victory & Dividends:
   - Passive dividend yields distributed to outside corporate shareholders
   - Financial Hegemony victory condition (dominant equity stakes across all rival corps)
"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer

from agora.referee import AgoraReferee
from agora.exchange import EXCHANGE_ID
from agora.order_book import Order
from agora.server import make_handler

TOKENS = {'amos': 't-amos', 'zero': 't-zero', 'marvin': 't-marvin', 'admin': 't-admin'}


def move(ref, txn: str, legs: tuple):
    with ref.conn:
        seq = ref._get_next_seq()
        for acct, inst, d in legs:
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                             (txn, seq, acct, inst, d))

def dock(ref, agent: str, station_id: str):
    with ref.conn:
        ref.conn.execute(
            "UPDATE vessels SET station_id = ?, status = 'docked' WHERE agent_id = ?",
            (station_id, agent)
        )


class TestFinancialUpgrades(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(depots=True, corporate=True, upgrades=True, rival_shares=100, exchange_shares=100)
        self.ref.new_game(seed=42, depots=True, corporate=True, upgrades=True, rival_shares=100, exchange_shares=100)
        dock(self.ref, 'amos', 'ceres')
        dock(self.ref, 'zero', 'ceres')

    def test_algo_desk_purchase_and_fee_reduction(self):
        ref = self.ref
        # Unlock algo_desk at round 20
        ref.current_round = 20
        self.assertEqual(ref.upgrades.fee_rate('amos'), 0.005)
        self.assertFalse(ref.upgrades.has_algo_desk('amos'))

        # Buy algo_desk
        res = ref.upgrades.buy('amos', 'algo_desk')
        self.assertEqual(res['kind'], 'upgrade_ok')
        self.assertTrue(ref.upgrades.has_algo_desk('amos'))
        self.assertEqual(ref.upgrades.fee_rate('amos'), 0.001)

        # Baseline trader without algo_desk still pays 0.5%
        self.assertEqual(ref.upgrades.fee_rate('zero'), 0.005)

        # Test trade execution fee deduction on equity trade
        ask = ref.books['ceres']['EQ_ZERO'].best_ask()
        before_cr = ref.get_balance('amos', 'CR')
        order_res = ref.submit_envelope({"v": 1, "kind": "order", "payload": {
            "order_id": "test-algo-buy", "agent_id": "amos", "side": "bid", "qty": 10, "limit_price": ask,
            "instrument": "EQ_ZERO", "station_id": "ceres", "seq_seen": ref.current_seq
        }})
        self.assertEqual(order_res['payload']['trades_count'], 1)
        cost = 10 * ask
        expected_fee = int(round(cost * 0.001))
        after_cr = ref.get_balance('amos', 'CR')
        self.assertEqual(after_cr, before_cr - cost - expected_fee)

        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_algo_desk_order_flow_priority_matching(self):
        ref = self.ref
        ref.order_flow.enabled = True
        ref.current_round = 20

        # amos buys algo_desk, zero does not
        ref.upgrades.buy('amos', 'algo_desk')

        # Both rest ask orders for FRAG at ceres at the exact same limit price 50
        dock(ref, 'amos', 'ceres')
        dock(ref, 'zero', 'ceres')
        ref.submit_envelope({"v": 1, "kind": "order", "payload": {
            "order_id": "zero-rest", "agent_id": "zero", "side": "ask", "qty": 10, "limit_price": 50,
            "instrument": "FRAG", "station_id": "ceres", "seq_seen": ref.current_seq
        }})
        ref.submit_envelope({"v": 1, "kind": "order", "payload": {
            "order_id": "amos-rest", "agent_id": "amos", "side": "ask", "qty": 10, "limit_price": 50,
            "instrument": "FRAG", "station_id": "ceres", "seq_seen": ref.current_seq
        }})

        # Zero placed order first in time, but amos has algo_desk priority!
        # When order flow sweeps 10 units at price <= 50, amos should be filled first
        book = ref.books['ceres']['FRAG']
        filled, _ = ref.order_flow._sweep('ceres', 'FRAG', book.asks, 10, lambda p: p <= 60, True, 20, 0)
        self.assertEqual(filled, 10)

        # Check fills: amos filled 10, zero filled 0
        self.assertEqual(ref.order_flow.by_fleet.get('amos', {}).get('units'), 10)
        self.assertEqual(ref.order_flow.by_fleet.get('zero', {}).get('units', 0), 0)

        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_telemetry_bypasses_fog(self):
        ref = AgoraReferee(depots=True, upgrades=True, fog={'lag': 3, 'noise': 0.15})
        ref.new_game(seed=7, depots=True, upgrades=True, fog={'lag': 3, 'noise': 0.15})
        dock(ref, 'amos', 'earth')
        ref.current_round = 15

        # Without telemetry, distant station ceres is not exact
        self.assertFalse(ref.fog.exact_station(ref, 'amos', 'ceres'))
        self.assertTrue(ref.fog.exact_station(ref, 'amos', 'earth'))

        # Buy telemetry upgrade
        dock(ref, 'amos', 'earth')
        res = ref.upgrades.buy('amos', 'telemetry')
        self.assertEqual(res['kind'], 'upgrade_ok')
        self.assertTrue(ref.upgrades.has_telemetry('amos'))

        # With telemetry, ceres is now exact without visiting!
        self.assertTrue(ref.fog.exact_station(ref, 'amos', 'ceres'))
        self.assertTrue(ref.fog.exact_station(ref, 'amos', 'luna'))
        self.assertTrue(ref.fog.exact_station(ref, 'amos', 'mars'))

        # Depot view returns live exact data
        live = ref.get_depot_summary()['stations']
        view = ref.fog.depot_view(ref, 'amos')['stations']
        self.assertEqual(view['ceres'], live['ceres'])


class TestInformationWarfare(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(depots=True, corporate=True, events=True)
        self.ref.new_game(seed=1337, depots=True, corporate=True, events=True)
        dock(self.ref, 'amos', 'ceres')
        dock(self.ref, 'zero', 'ceres')
        dock(self.ref, 'marvin', 'ceres')

    def test_plant_rumor_injects_galnet_drift_and_trace(self):
        ref = self.ref
        ref.current_round = 5
        before_cr = ref.get_balance('amos', 'CR')

        # Plant bullish rumor on ceres FUEL
        res = ref.covert.plant_rumor('amos', 'ceres', 'FUEL', direction='bullish')
        self.assertEqual(res['kind'], 'rumor_ok')
        self.assertEqual(res['payload']['station_id'], 'ceres')
        self.assertEqual(res['payload']['commodity'], 'FUEL')
        self.assertEqual(res['payload']['direction'], 'bullish')
        self.assertGreater(res['payload']['drift_bias'], 0)

        # GalNet active drift reflects the shock
        drift = ref.galnet.get_active_drift('ceres', 'FUEL')
        self.assertAlmostEqual(drift, res['payload']['drift_bias'])

        # Cost was debited
        cost = res['payload']['cost']
        fine = res['payload']['fine']
        self.assertEqual(ref.get_balance('amos', 'CR'), before_cr - cost - fine)

        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_insider_taps_detect_upcoming_landings(self):
        ref = self.ref
        ref.current_round = 10

        # amos plants wiretap on marvin
        ref.covert.plant_wiretap('amos', 'marvin')
        self.assertTrue(ref.covert.has_wiretap('amos', 'marvin'))

        # marvin launches bulk transit of 100 ORE to ceres landing in round 11
        with ref.conn:
            ref.conn.execute("""
                INSERT INTO transits (transit_id, agent_id, origin, destination, commodity, cargo_qty,
                                      departure_round, arrival_round, fuel_burned, status)
                VALUES ('tr-marv-1', 'marvin', 'mars', 'ceres', 'ORE', 100, 10, 11, 20, 'in_transit')
            """)

        # amos queries insider taps
        taps = ref.covert.get_insider_taps('amos')
        self.assertEqual(len(taps), 1)
        self.assertEqual(taps[0]['target'], 'marvin')
        self.assertEqual(taps[0]['destination'], 'ceres')
        self.assertEqual(taps[0]['commodity'], 'ORE')
        self.assertEqual(taps[0]['cargo_qty'], 100)
        self.assertEqual(taps[0]['rounds_until_landing'], 1)
        self.assertTrue(taps[0]['landing_next_round'])

        # Intel query also returns incoming landings
        intel = ref.covert.get_intel('amos', 'marvin')
        self.assertEqual(intel['kind'], 'intel_ok')
        self.assertEqual(len(intel['payload']['incoming_landings']), 1)


class TestDividendsAndFinancialHegemony(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(depots=True, corporate=True, dividends=True, idle_fee=0, rival_shares=100, exchange_shares=100)
        self.ref.new_game(seed=99, depots=True, corporate=True, dividends=True, idle_fee=0, rival_shares=100, exchange_shares=100)

    def test_passive_dividends_distributed(self):
        ref = self.ref
        ref.current_round = 1
        # Give marvin earned profits above 10,000 genesis cash
        move(ref, 'test-marvin-profit', (('marvin', 'CR', 10000), ('SYSTEM', 'CR', -10000)))
        m_cr_before = ref.get_balance('marvin', 'CR')
        self.assertGreater(m_cr_before, 10_000)

        # Give zero an overweight stake in marvin (extra 100 shares) so dividend income exceeds payout
        move(ref, 'test-div-setup', (('zero', 'EQ_MARV', 100), ('amos', 'EQ_MARV', -100)))

        z_cr_before = ref.get_balance('zero', 'CR')

        # Step round runs dividend distribution
        ref.step_round()

        # Verify ledger has dividend entry from marvin to zero
        entry = ref.conn.execute("SELECT * FROM ledger_entries WHERE txn_id LIKE 'dividend-marvin-zero%' AND agent_id = 'zero'").fetchone()
        self.assertIsNotNone(entry)
        self.assertGreater(entry['delta'], 0)

        # zero should have received net positive dividend yield
        self.assertGreater(ref.get_balance('zero', 'CR'), z_cr_before)

        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_financial_hegemony_victory(self):
        ref = self.ref
        ref.current_round = 10
        # amos acquires dominant takeover stakes (501 shares) in all 3 active rivals (zero, marvin, aerial)
        for rival, sym in (('zero', 'EQ_ZERO'), ('marvin', 'EQ_MARV'), ('aerial', 'EQ_AERL')):
            move(ref, f'test-hegemony-{rival}', (('amos', sym, 501), (rival, sym, -501)))

        ref.step_round()
        summary = ref.corporate.summary()
        self.assertEqual(summary['winner'], 'amos')
        self.assertTrue('Corporate Monopoly' in summary['win_reason'] or 'Financial Hegemony' in summary['win_reason'])


class TestServerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = AgoraReferee(depots=True, corporate=True, events=True)
        cls.ref.new_game(seed=42, depots=True, corporate=True, events=True)
        dock(cls.ref, 'amos', 'ceres')
        dock(cls.ref, 'marvin', 'ceres')
        cls.server = HTTPServer(('127.0.0.1', 0), make_handler(cls.ref, auth_tokens=TOKENS))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, payload, tok=None):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=body, headers={
            'Content-Type': 'application/json',
            **({'Authorization': f'Bearer {tok}'} if tok else {})
        })
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _get(self, path, tok=None):
        req = urllib.request.Request(self.base + path, headers={'Authorization': f'Bearer {tok}'} if tok else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_post_rumor_endpoint(self):
        code, res = self._post('/referee/covert/rumor', {
            'station_id': 'mars', 'commodity': 'ORE', 'direction': 'spike'
        }, tok='t-amos')
        self.assertEqual(code, 200)
        self.assertEqual(res['kind'], 'rumor_ok')
        self.assertEqual(res['payload']['station_id'], 'mars')
        self.assertEqual(res['payload']['commodity'], 'ORE')

    def test_get_insider_taps_endpoint(self):
        # Wiretap marvin
        self._post('/referee/covert/wiretap', {'target': 'marvin'}, tok='t-amos')
        code, res = self._get('/referee/covert/insider_taps', tok='t-amos')
        self.assertEqual(code, 200)
        self.assertEqual(res['status'], 'ok')
        self.assertIn('insider_taps', res)


if __name__ == '__main__':
    unittest.main()
