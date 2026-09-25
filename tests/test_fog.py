"""Fog of war on market data (docs/fleet-market-spec.md section 3)."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from agora.referee import AgoraReferee
from agora.server import make_handler

TOKENS = {'amos': 'ta', 'zero': 'tz', 'combine': 'tc', 'admin': 'tadm'}


def dock(ref, agent, st):
    with ref.conn:
        # vessel_locations is a view of ship 1 since #175; its INSTEAD OF
        # INSERT trigger upserts, and an UPSERT cannot target a view.
        ref.conn.execute("INSERT INTO vessel_locations (agent_id, station_id, docked_since) VALUES (?, ?, 0)",
                         (agent, st))


class TestFogEngine(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(depots=True)
        self.ref.new_game(seed=7, depots=True, fog={'lag': 3, 'noise': 0.15})
        dock(self.ref, 'amos', 'earth')
        for _ in range(6):
            self.ref.step_round()

    def test_exact_where_docked_fogged_elsewhere(self):
        live = self.ref.get_depot_summary()['stations']
        view = self.ref.fog.depot_view(self.ref, 'amos')
        self.assertEqual(view['stations']['earth'], live['earth'])
        self.assertEqual(view['fog']['exact_station'], 'earth')
        self.assertIsNone(view['stations']['ceres']['ORE']['ask_depth'])
        diffs = sum(view['stations'][st][c]['best_ask'] != live[st][c]['best_ask']
                    for st in ('luna', 'mars', 'ceres') for c in live[st])
        self.assertGreater(diffs, 0)

    def test_views_differ_by_fleet_and_are_stable(self):
        a1 = self.ref.fog.depot_view(self.ref, 'amos')['stations']['ceres']
        a2 = self.ref.fog.depot_view(self.ref, 'amos')['stations']['ceres']
        z = self.ref.fog.depot_view(self.ref, 'zero')['stations']['ceres']
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, z)

    def test_public_view_has_no_exact_station(self):
        view = self.ref.fog.depot_view(self.ref, None)
        self.assertIsNone(view['fog']['exact_station'])

    def test_leaderboard_view_fogs_non_docked_stations(self):
        raw_lb = self.ref.get_leaderboard()
        amos_view = self.ref.fog.leaderboard_view(self.ref, "amos", raw_lb)
        amos_row = next(e for e in amos_view if e["agent_id"] == "amos")
        zero_row = next(e for e in amos_view if e["agent_id"] == "zero")
        raw_amos = next(e for e in raw_lb if e["agent_id"] == "amos")
        raw_zero = next(e for e in raw_lb if e["agent_id"] == "zero")

        # Amos is docked at Earth: Earth marks match raw exact marks
        self.assertEqual(amos_row["commodity_marks"], raw_amos["commodity_marks"])
        self.assertEqual(amos_row["mark_price"], raw_amos["mark_price"])

        # Zero is docked at Ceres: Ceres marks must be fogged for Amos
        self.assertNotEqual(zero_row["commodity_marks"], raw_zero["commodity_marks"])

        # Public view fogs all stations
        pub_view = self.ref.fog.leaderboard_view(self.ref, None, raw_lb)
        pub_amos = next(e for e in pub_view if e["agent_id"] == "amos")
        self.assertNotEqual(pub_amos["commodity_marks"], raw_amos["commodity_marks"])

    def test_leaderboard_view_fogs_net_worth_preventing_price_leak(self):
        # Repro for #261: verify exact remote commodity prices cannot be solved from net_worth
        raw_lb = self.ref.get_leaderboard()
        amos_view = self.ref.fog.leaderboard_view(self.ref, "amos", raw_lb)
        zero_row = next(e for e in amos_view if e["agent_id"] == "zero")
        raw_zero = next(e for e in raw_lb if e["agent_id"] == "zero")
        amos_row = next(e for e in amos_view if e["agent_id"] == "amos")
        raw_amos = next(e for e in raw_lb if e["agent_id"] == "amos")

        # Amos sees own exact net_worth
        self.assertEqual(amos_row["net_worth"], raw_amos["net_worth"])

        # Zero is docked at Ceres; net_worth must be fogged (recomputed with jittered marks)
        self.assertNotEqual(zero_row["commodity_marks"], raw_zero["commodity_marks"])
        if zero_row["mark_price"] != raw_zero["mark_price"]:
            self.assertNotEqual(zero_row["net_worth"], raw_zero["net_worth"])

        # Algebraic solve: (net_worth - liquid) / frags must yield jittered mark, NOT raw exact mark
        solved_mark = (zero_row["net_worth"] - zero_row["liquid"]) / zero_row["frags"]
        self.assertEqual(solved_mark, zero_row["mark_price"])
        if zero_row["mark_price"] != raw_zero["mark_price"]:
            self.assertNotEqual(solved_mark, raw_zero["mark_price"])

        # Multi-commodity test with FOOD
        ref2 = AgoraReferee(depots=True)
        ref2.new_game(seed=7, depots=True, fog={'lag': 3, 'noise': 0.15})
        dock(ref2, 'amos', 'earth')
        dock(ref2, 'zero', 'ceres')
        with ref2.conn:
            ref2.conn.execute("INSERT OR REPLACE INTO accounts (agent_id, instrument, balance) VALUES ('zero', 'FOOD', 500)")
        for _ in range(6):
            ref2.step_round()
        raw2 = ref2.get_leaderboard()
        raw2_zero = next(e for e in raw2 if e["agent_id"] == "zero")
        view2 = ref2.fog.leaderboard_view(ref2, "amos", raw2)
        view2_zero = next(e for e in view2 if e["agent_id"] == "zero")

        expected_fogged_nw = (view2_zero['liquid'] +
                              view2_zero['frags'] * view2_zero['commodity_marks']['FRAG'] +
                              view2_zero['food'] * view2_zero['commodity_marks']['FOOD'] +
                              view2_zero['ore'] * view2_zero['commodity_marks']['ORE'] +
                              view2_zero['machinery'] * view2_zero['commodity_marks']['MACHINERY'] +
                              view2_zero['upgrades_value'] + view2_zero['ships_value'] + view2_zero['stocks_value'])
        self.assertEqual(view2_zero["food"], 500)
        self.assertEqual(view2_zero["net_worth"], expected_fogged_nw)
        self.assertNotEqual(view2_zero["net_worth"], raw2_zero["net_worth"])

    def test_off_by_default(self):
        self.assertIsNone(AgoraReferee().fog)


class TestFogEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = AgoraReferee(depots=True)
        cls.ref.new_game(seed=7, depots=True, fog=True)
        dock(cls.ref, 'amos', 'earth')
        for _ in range(5):
            cls.ref.step_round()
        cls.server = HTTPServer(('127.0.0.1', 0), make_handler(cls.ref, auth_tokens=TOKENS))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path, tok=None):
        req = urllib.request.Request(self.base + path, headers={'Authorization': f'Bearer {tok}'} if tok else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read().decode()
                return r.status, (json.loads(body) if body.startswith('{') else body)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_book_exact_only_where_docked(self):
        self.assertEqual(self._get('/referee/book?station_id=earth&instrument=ORE', 'ta')[0], 200)
        code, r = self._get('/referee/book?station_id=ceres&instrument=ORE', 'ta')
        self.assertEqual((code, r['payload']['reason']), (403, 'fogged'))
        self.assertEqual(self._get('/referee/book?station_id=earth&instrument=ORE', 'tc')[0], 403)
        self.assertEqual(self._get('/referee/book?station_id=ceres&instrument=ORE', 'tadm')[0], 200)

    def test_depots_and_prices_are_viewer_scoped(self):
        live = self.ref.get_depot_summary()['stations']
        _, mine = self._get('/referee/depots', 'ta')
        _, public = self._get('/referee/depots')
        _, via_combine = self._get('/referee/depots', 'tc')
        self.assertEqual(mine['depots']['stations']['earth'], live['earth'])
        self.assertNotEqual(public['depots']['stations']['earth'], live['earth'])
        self.assertEqual(public['depots'], via_combine['depots'])
        _, p = self._get('/stations/prices?station_id=earth', 'ta')
        self.assertEqual(p['data']['prices'], self.ref.spatial.get_prices()['earth'])

    def test_briefing_says_who_you_are(self):
        _, body = self._get('/referee/briefing', 'ta')
        self.assertIn('You are amos', body)
        _, body = self._get('/referee/briefing')
        self.assertIn('public view', body)

    def test_terminal_stream_info_says_public(self):
        code, r = self._get('/ws/terminal')
        self.assertEqual((code, r['fog']), (200, 'public'))

    def test_leaderboard_endpoint_viewer_scoped(self):
        _, amos_lb = self._get('/referee/leaderboard', 'ta')
        _, public_lb = self._get('/referee/leaderboard')
        _, admin_lb = self._get('/referee/leaderboard', 'tadm')

        raw_lb = self.ref.get_leaderboard()
        raw_amos = next(e for e in raw_lb if e['agent_id'] == 'amos')
        raw_zero = next(e for e in raw_lb if e['agent_id'] == 'zero')

        amos_entry = next(e for e in amos_lb['leaderboard'] if e['agent_id'] == 'amos')
        zero_entry = next(e for e in amos_lb['leaderboard'] if e['agent_id'] == 'zero')
        pub_amos_entry = next(e for e in public_lb['leaderboard'] if e['agent_id'] == 'amos')
        adm_zero_entry = next(e for e in admin_lb['leaderboard'] if e['agent_id'] == 'zero')

        # Amos at Earth sees exact Earth marks
        self.assertEqual(amos_entry['commodity_marks'], raw_amos['commodity_marks'])
        # But Ceres marks on Zero's row are fogged
        self.assertNotEqual(zero_entry['commodity_marks'], raw_zero['commodity_marks'])
        # Public view fogs Earth too
        self.assertNotEqual(pub_amos_entry['commodity_marks'], raw_amos['commodity_marks'])
        # Admin sees exact Ceres marks
        self.assertEqual(adm_zero_entry['commodity_marks'], raw_zero['commodity_marks'])

        # Net worth on Zero's row is fogged for Amos and solves to jittered mark (#261)
        solved_endpoint_mark = (zero_entry['net_worth'] - zero_entry['liquid']) / zero_entry['frags']
        self.assertEqual(solved_endpoint_mark, zero_entry['mark_price'])
        if zero_entry['mark_price'] != raw_zero['mark_price']:
            self.assertNotEqual(zero_entry['net_worth'], raw_zero['net_worth'])
            self.assertNotEqual(solved_endpoint_mark, raw_zero['mark_price'])

        # Admin sees exact net_worth for all fleets
        self.assertEqual(adm_zero_entry['net_worth'], raw_zero['net_worth'])


class TestFoggedTerminalStream(unittest.TestCase):
    def test_public_snapshot_hides_exact_goods_prices(self):
        from agora.websocket import TerminalDiffEngine
        ref = AgoraReferee(depots=True)
        ref.new_game(seed=5, depots=True, fog=True)
        for _ in range(5):
            ref.step_round()
        snap = TerminalDiffEngine(ref, public_fog=True).get_snapshot('ceres', 'ORE')
        self.assertTrue(snap['book']['fogged'])
        self.assertIsNone(snap['last_price'])
        self.assertEqual(snap['circuit']['bands'], [])
        self.assertEqual(set(snap['depots']['stations']), {'earth', 'luna', 'mars', 'ceres'})
        self.assertIsNone(snap['depots']['fog']['exact_station'])
        self.assertEqual(snap['fog']['view'], 'public')
        self.assertEqual(snap['depots'], ref.fog.depot_view(ref, None))
        stock = TerminalDiffEngine(ref, public_fog=True).get_snapshot('ceres', 'EQ_AMOS')
        self.assertNotIn('fogged', stock['book'])
        exact = TerminalDiffEngine(ref, public_fog=False).get_snapshot('ceres', 'ORE')
        self.assertNotIn('fogged', exact['book'])

        # Leaderboard marks in public terminal snapshot must be fogged for non-docked stations
        raw_lb = ref.get_leaderboard()
        raw_first = raw_lb[0]
        snap_first = snap['leaderboard'][0]
        self.assertNotEqual(snap_first['commodity_marks'], raw_first['commodity_marks'])


if __name__ == '__main__':
    unittest.main()
