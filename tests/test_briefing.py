"""GET /referee/briefing: plain-text, live, readable without JavaScript."""
import threading
import unittest
import urllib.request
from http.server import HTTPServer

from agora.referee import AgoraReferee
from agora.server import make_handler
from agora.briefing import build_briefing


class TestBriefing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = AgoraReferee()
        cls.ref.seed_depots()
        cls.server = HTTPServer(('127.0.0.1', 0), make_handler(cls.ref, auth_tokens={'admin': 't'}))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _fetch(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.headers.get('Content-Type'), r.read().decode()

    def test_endpoint_and_aliases_serve_markdown(self):
        for p in ('/referee/briefing', '/briefing', '/llms.txt'):
            status, ctype, body = self._fetch(p)
            self.assertEqual(status, 200)
            self.assertIn('text/markdown', ctype)
            self.assertNotIn('<script', body)
            self.assertIn('# AGORA briefing, round', body)

    def test_contains_every_station_price_row_and_routes(self):
        body = build_briefing(self.ref)
        for st in ('Earth', 'Luna', 'Mars', 'Ceres'):
            self.assertIn(f'| {st} |', body)
        self.assertIn('| Ceres | Earth | 3 | 30 | 25 |', body)
        self.assertIn('MOVE TO EARTH WITH 200 ORE', body)
        self.assertIn('AT <your station>', body)

    def test_depot_quotes_come_from_live_book(self):
        q = self.ref.get_depot_summary()['stations']['ceres']['ORE']
        body = build_briefing(self.ref)
        self.assertIn(f"BUY 200 ORE @ {int(q['best_ask'])} AT CERES", body)

    def test_json_format(self):
        import json
        with urllib.request.urlopen(self.base + '/referee/briefing?format=json', timeout=5) as r:
            d = json.loads(r.read().decode())
        self.assertEqual(d['status'], 'ok')
        self.assertEqual(set(d['depots']['stations']), {'earth', 'luna', 'mars', 'ceres'})
        self.assertEqual(len(d['routes']), 12)
        self.assertTrue(all('location' in f for f in d['fleets']))


if __name__ == '__main__':
    unittest.main()
