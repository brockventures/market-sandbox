"""
tests.test_galnet - Unit tests for GalNet Breaking News Wire and Drift Shock Engine.
"""

import pytest
from agora.galnet import GalNetEngine, NEWS_TEMPLATES


def test_galnet_initialization():
    engine = GalNetEngine(seed=42, shock_probability=0.5)
    assert engine.seed == 42
    assert engine.current_round == 0
    assert len(engine.events) == 0
    assert len(engine.active_shocks) == 0
    assert engine.get_active_drift("ceres", "FUEL") == 0.0


def test_galnet_determinism():
    engine1 = GalNetEngine(seed=999, shock_probability=1.0)
    engine2 = GalNetEngine(seed=999, shock_probability=1.0)

    event1 = engine1.step_round(1)
    event2 = engine2.step_round(1)

    assert event1 is not None
    assert event2 is not None
    assert event1.station_id == event2.station_id
    assert event1.commodity == event2.commodity
    assert event1.headline == event2.headline
    assert event1.drift_bias == event2.drift_bias
    assert event1.duration_rounds == event2.duration_rounds


def test_galnet_forced_shock():
    engine = GalNetEngine(seed=123)
    ev = engine.force_shock(round_num=1, template_idx=0)
    assert ev.station_id == "ceres"
    assert ev.commodity == "FUEL"
    assert ev.drift_bias == 0.40
    assert ev.duration_rounds == 4
    assert engine.get_active_drift("ceres", "FUEL") == 0.40
    assert engine.get_active_drift("mars", "FRAG") == 0.0


def test_galnet_shock_expiration():
    engine = GalNetEngine(seed=123, shock_probability=0.0)
    # Template 7 has duration_rounds = 2
    ev = engine.force_shock(round_num=10, template_idx=7)
    assert ev.duration_rounds == 2
    assert engine.get_active_drift("earth", "FUEL") == -0.15

    # Round 11: Still active (11 < 10 + 2)
    engine.step_round(11)
    assert engine.get_active_drift("earth", "FUEL") == -0.15

    # Round 12: Expired (12 >= 10 + 2)
    engine.step_round(12)
    assert engine.get_active_drift("earth", "FUEL") == 0.0


def test_galnet_additive_drift():
    engine = GalNetEngine(seed=123, shock_probability=0.0)
    # Force two shocks on Ceres
    engine.force_shock(round_num=1, template_idx=0)  # Ceres FUEL +0.40, dur 4
    engine.force_shock(round_num=1, template_idx=4)  # Ceres FRAG -0.30, dur 4

    assert engine.get_active_drift("ceres", "FUEL") == 0.40
    assert engine.get_active_drift("ceres", "FRAG") == -0.30


def test_galnet_feed_limit():
    engine = GalNetEngine(seed=123, shock_probability=1.0)
    for r in range(1, 25):
        engine.step_round(r)

    assert len(engine.events) == 24
    feed = engine.get_feed(limit=5)
    assert len(feed) == 5
    # Reverse chronological order: newest first
    assert feed[0]["round"] == 24
    assert feed[1]["round"] == 23
