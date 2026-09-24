"""
tests/test_galnet_trends.py - GalNet News & Un-fogged Price Trend Inferences (#123).

Tests:
1. GalNetNewsEvent properties (trend, direction, pct_impact, expires_round, rounds_remaining).
2. Sol system template coverage across all stations (ceres, mars, luna, earth) and goods (FRAG, FUEL, FOOD, ORE).
3. GalNetEngine.infer_trend() trajectory calculations.
4. Automatic round advancement in AgoraReferee.step_round().
5. Briefing integration in build_briefing() exposing active shocks and un-fogged trajectories under Fog of War.
6. HTTP REST endpoint GET /galnet/trend.
7. Fog of War strategic inference: remote quote vs. true GalNet drift trajectory.
"""

import json
import threading
import unittest
import urllib.request
from http.server import HTTPServer

from agora.briefing import build_briefing
from agora.galnet import GalNetEngine, GalNetNewsEvent, NEWS_TEMPLATES
from agora.referee import AgoraReferee
from agora.server import make_handler
from agora.spatial import STATIONS, COMMODITIES, BASE_PRICES


class TestGalnetTrends(unittest.TestCase):
    def test_news_event_trend_metadata(self):
        """GalNetNewsEvent provides explicit direction, trend, and impact percentage (#123)."""
        ev_surge = GalNetNewsEvent(
            id="gn-1", round=5, timestamp=1000.0,
            station_id="mars", commodity="FRAG",
            headline="DEBRIS REQUISITION", body="Martian shipyards bidding high.",
            drift_bias=0.35, duration_rounds=3
        )
        self.assertEqual(ev_surge.direction, "up")
        self.assertEqual(ev_surge.trend, "SURGE")
        self.assertEqual(ev_surge.pct_impact, "+35%")
        self.assertEqual(ev_surge.expires_round, 8)
        self.assertEqual(ev_surge.rounds_remaining(5), 3)
        self.assertEqual(ev_surge.rounds_remaining(7), 1)
        self.assertEqual(ev_surge.rounds_remaining(8), 0)

        d = ev_surge.to_dict(current_round=6)
        self.assertEqual(d["direction"], "up")
        self.assertEqual(d["trend"], "SURGE")
        self.assertEqual(d["pct_impact"], "+35%")
        self.assertEqual(d["expires_round"], 8)
        self.assertEqual(d["rounds_remaining"], 2)

        ev_drop = GalNetNewsEvent(
            id="gn-2", round=2, timestamp=1000.0,
            station_id="ceres", commodity="ORE",
            headline="ORE GLUT", body="Refineries overwhelmed.",
            drift_bias=-0.30, duration_rounds=4
        )
        self.assertEqual(ev_drop.direction, "down")
        self.assertEqual(ev_drop.trend, "DROP")
        self.assertEqual(ev_drop.pct_impact, "-30%")
        self.assertEqual(ev_drop.expires_round, 6)

    def test_template_coverage_across_stations_and_goods(self):
        """NEWS_TEMPLATES provides lore-aligned shocks for all stations and commodities (#123)."""
        pairs = set()
        for tpl in NEWS_TEMPLATES:
            st = tpl["station_id"].lower()
            comm = tpl["commodity"].upper()
            pairs.add((st, comm))
            self.assertIn(st, STATIONS)
            self.assertIn(comm, COMMODITIES)
            self.assertNotEqual(tpl["drift_bias"], 0.0)
            self.assertGreater(tpl["duration_rounds"], 0)
            self.assertTrue(len(tpl["headline"]) > 0)
            self.assertTrue(len(tpl["body"]) > 0)

        # Every station must have stories
        for st in STATIONS:
            st_stories = [p for p in pairs if p[0] == st]
            self.assertGreaterEqual(len(st_stories), 2, f"Station {st} lacks sufficient story coverage")

    def test_infer_trend_calculation(self):
        """GalNetEngine.infer_trend() calculates active drift and direction (#123)."""
        engine = GalNetEngine(seed=42)
        # Without shocks, trend is flat
        flat = engine.infer_trend("mars", "FRAG", fogged_spot=15.0)
        self.assertEqual(flat["trend"], "FLAT")
        self.assertEqual(flat["direction"], "neutral")
        self.assertEqual(flat["net_drift"], 0.0)
        self.assertEqual(len(flat["active_stories"]), 0)

        # Force positive shock on mars FRAG (+0.35, 3 rounds)
        engine.force_shock(round_num=1, template_idx=1)
        up = engine.infer_trend("mars", "FRAG", fogged_spot=15.0)
        self.assertEqual(up["trend"], "SURGE")
        self.assertEqual(up["direction"], "up")
        self.assertEqual(up["net_drift"], 0.35)
        self.assertEqual(len(up["active_stories"]), 1)
        self.assertIn("Upward", up["description"])

    def test_referee_step_round_advances_galnet(self):
        """AgoraReferee.step_round() advances GalNet round only when galnet_auto_step=True (#123)."""
        # Default: auto-step is off to preserve economic balance
        ref_off = AgoraReferee(corporate=True, depots=True)
        ref_off.new_game(seed=100, warmup_rounds=2)
        self.assertFalse(ref_off.galnet_auto_step)
        self.assertEqual(ref_off.galnet.current_round, 0)  # warmup did not step galnet
        ref_off.step_round()
        self.assertEqual(ref_off.galnet.current_round, 0)

        # Explicitly enabled: advances in lockstep and records news
        ref_on = AgoraReferee(corporate=True, depots=True, galnet_auto_step=True)
        ref_on.new_game(seed=100, warmup_rounds=2)
        self.assertTrue(ref_on.galnet_auto_step)
        self.assertEqual(ref_on.galnet.current_round, 2)
        ref_on.step_round()
        self.assertEqual(ref_on.galnet.current_round, 1)

    def test_galnet_seed_determinism(self):
        """new_game seeds GalNet from the game seed (#123)."""
        ref1 = AgoraReferee()
        ref1.new_game(seed=1)
        ref2 = AgoraReferee()
        ref2.new_game(seed=2)
        # Advance both with forced steps or auto-step to check headline RNG variance
        ev1 = ref1.galnet.step_round(1)
        ev2 = ref2.galnet.step_round(1)
        # Seeds 1 and 2 start with different RNG states in GalNet
        self.assertNotEqual(ref1.galnet.rng.getstate(), ref2.galnet.rng.getstate())

    def test_briefing_piracy_notice_formatting(self):
        """Briefing renders non-commodity events (like piracy) without broken arrows or 0% impact (#123)."""
        ref = AgoraReferee(depots=True)
        ref.new_game(seed=42)
        ev = GalNetNewsEvent(
            id="gn-piracy-1-mars", round=1, timestamp=1000.0,
            station_id="mars", commodity="",
            headline="RAIDERS SIGHTED ON THE MARS LANES",
            body="Traffic control warns of piracy.",
            drift_bias=0.0, duration_rounds=20
        )
        ref.galnet.events.append(ev)
        briefing = build_briefing(ref, viewer="amos")
        self.assertIn("RAIDERS SIGHTED ON THE MARS LANES", briefing)
        self.assertIn("(Mars, round 1)", briefing)
        self.assertNotIn("▼ 0%", briefing)
        self.assertNotIn("Mars  ▼", briefing)

    def test_briefing_surfaces_galnet_trends_under_fog(self):
        """build_briefing() includes GalNet active market shocks and un-fogged guidance (#123)."""
        ref = AgoraReferee(depots=True, corporate=True)
        ref.new_game(seed=42, warmup_rounds=2, fog=True)
        # Force a news shock on Ceres FUEL (+0.40, 4 rounds)
        ev = ref.galnet.force_shock(round_num=0, template_idx=0)

        # Generate briefing for viewer docked at Earth (remote from Ceres)
        briefing = build_briefing(ref, viewer="amos")
        self.assertIn("## GalNet news & price trends", briefing)
        self.assertIn("Active market shocks", briefing)
        self.assertIn("CERES ICE-CRACKING COMPRESSOR BLOWOUT", briefing)
        self.assertIn("Ceres FUEL ▲ +40%", briefing)
        self.assertIn("Under Fog of War", briefing)

    def test_http_galnet_trend_endpoint(self):
        """GET /galnet/trend returns structured un-fogged price trajectory (#123)."""
        ref = AgoraReferee(depots=True)
        ref.new_game(seed=42, warmup_rounds=2)
        ref.galnet.force_shock(round_num=0, template_idx=1)  # mars FRAG +0.35

        handler_cls = make_handler(ref, auth_tokens={'amos': 't-amos'})
        server = HTTPServer(('127.0.0.1', 0), handler_cls)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            url = f"http://127.0.0.1:{port}/galnet/trend?station_id=mars&commodity=FRAG&fogged_spot=14.5"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(data["status"], "ok")
                self.assertEqual(data["station_id"], "mars")
                self.assertEqual(data["commodity"], "FRAG")
                self.assertEqual(data["trend"], "SURGE")
                self.assertEqual(data["direction"], "up")
                self.assertEqual(data["net_drift"], 0.35)
                self.assertEqual(data["fogged_spot"], 14.5)
                self.assertEqual(len(data["active_stories"]), 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_fogged_trader_can_infer_unfogged_trajectory(self):
        """A trader seeing a stale/fogged remote quote uses GalNet to anticipate the real price trend (#123)."""
        ref = AgoraReferee(depots=True)
        # Enable fog with 2 rounds of lag
        ref.new_game(seed=42, warmup_rounds=0, fog=True)
        ref._configure_fog(True, seed=42)

        # Force a major news shock on Mars FRAG in round 1: +0.35 drift
        ev = ref.galnet.force_shock(round_num=1, template_idx=1)

        # Step 2 rounds to let the drift accumulate in true spot price
        ref.step_round()
        ref.step_round()

        # True un-fogged spot price at Mars
        true_spot = ref.spatial.get_station_price("mars", "FRAG")

        # Stale fogged spot quote seen by a player docked at Ceres
        fogged_view = ref.fog.depot_view(ref, viewer="amos")
        fogged_mars_frag = fogged_view["stations"]["mars"]["FRAG"]["spot_price"]

        # GalNet trend exposes the un-fogged trajectory
        trend = ref.galnet.infer_trend("mars", "FRAG", fogged_spot=fogged_mars_frag)
        self.assertEqual(trend["trend"], "SURGE")
        self.assertEqual(trend["direction"], "up")
        self.assertGreater(trend["net_drift"], 0.0)

        # Because drift was +0.35 over 2 rounds, true spot price is elevated relative to base
        base_mars_frag = BASE_PRICES["mars"]["FRAG"]
        self.assertGreater(true_spot, base_mars_frag)


if __name__ == '__main__':
    unittest.main()
