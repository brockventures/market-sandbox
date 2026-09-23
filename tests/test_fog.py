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
        ref.conn.execute("INSERT INTO vessel_locations (agent_id, station_id, docked_since) VALUES (?, ?, 0) "
                         "ON CONFLICT(agent_id) DO UPDATE SET station_id = excluded.station_id", (agent, st))


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

    def test_terminal_stream_blocked(self):
        self.assertEqual(self._get('/ws/terminal')[0], 403)


if __name__ == '__main__':
    unittest.main()
