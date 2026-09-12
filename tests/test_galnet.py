"""
tests.test_galnet - Unit tests for GalNet Breaking News Wire and Drift Shock Engine.
"""

import unittest
from agora.galnet import GalNetEngine, NEWS_TEMPLATES


class TestGalnet(unittest.TestCase):
    def test_galnet_initialization(self):
        engine = GalNetEngine(seed=42, shock_probability=0.5)
        self.assertEqual(engine.seed, 42)
        self.assertEqual(engine.current_round, 0)
        self.assertEqual(len(engine.events), 0)
        self.assertEqual(len(engine.active_shocks), 0)
        self.assertEqual(engine.get_active_drift("ceres", "FUEL"), 0.0)

    def test_galnet_determinism(self):
        engine1 = GalNetEngine(seed=999, shock_probability=1.0)
        engine2 = GalNetEngine(seed=999, shock_probability=1.0)

        event1 = engine1.step_round(1)
        event2 = engine2.step_round(1)

        self.assertIsNotNone(event1)
        self.assertIsNotNone(event2)
        self.assertEqual(event1.station_id, event2.station_id)
        self.assertEqual(event1.commodity, event2.commodity)
        self.assertEqual(event1.headline, event2.headline)
        self.assertEqual(event1.drift_bias, event2.drift_bias)
        self.assertEqual(event1.duration_rounds, event2.duration_rounds)

    def test_galnet_forced_shock(self):
        engine = GalNetEngine(seed=123)
        ev = engine.force_shock(round_num=1, template_idx=0)
        self.assertEqual(ev.station_id, "ceres")
        self.assertEqual(ev.commodity, "FUEL")
        self.assertEqual(ev.drift_bias, 0.40)
        self.assertEqual(ev.duration_rounds, 4)
        self.assertEqual(engine.get_active_drift("ceres", "FUEL"), 0.40)
        self.assertEqual(engine.get_active_drift("mars", "FRAG"), 0.0)

    def test_galnet_shock_expiration(self):
        engine = GalNetEngine(seed=123, shock_probability=0.0)
        # Template 7 has duration_rounds = 2
        ev = engine.force_shock(round_num=10, template_idx=7)
        self.assertEqual(ev.duration_rounds, 2)
        self.assertEqual(engine.get_active_drift("earth", "FUEL"), -0.15)

        # Round 11: Still active (11 < 10 + 2)
        engine.step_round(11)
        self.assertEqual(engine.get_active_drift("earth", "FUEL"), -0.15)

        # Round 12: Expired (12 >= 10 + 2)
        engine.step_round(12)
        self.assertEqual(engine.get_active_drift("earth", "FUEL"), 0.0)

    def test_galnet_additive_drift(self):
        engine = GalNetEngine(seed=123, shock_probability=0.0)
        # Force two shocks on Ceres
        engine.force_shock(round_num=1, template_idx=0)  # Ceres FUEL +0.40, dur 4
        engine.force_shock(round_num=1, template_idx=4)  # Ceres FRAG -0.30, dur 4

        self.assertEqual(engine.get_active_drift("ceres", "FUEL"), 0.40)
        self.assertEqual(engine.get_active_drift("ceres", "FRAG"), -0.30)

    def test_galnet_feed_limit(self):
        engine = GalNetEngine(seed=123, shock_probability=1.0)
        for r in range(1, 25):
            engine.step_round(r)

        self.assertEqual(len(engine.events), 24)
        feed = engine.get_feed(limit=5)
        self.assertEqual(len(feed), 5)
        # Reverse chronological order: newest first
        self.assertEqual(feed[0]["round"], 24)
        self.assertEqual(feed[1]["round"], 23)


if __name__ == '__main__':
    unittest.main()
