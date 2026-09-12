"""
tests.test_spatial - Unit tests for Sol station topology, route physics, and price surfaces.
"""

import pytest
from agora.spatial import StationPriceEngine, get_route, STATIONS, COMMODITIES, BASE_PRICES
from agora.galnet import GalNetEngine


def test_station_constants():
    assert len(STATIONS) == 4
    assert "ceres" in STATIONS
    assert "earth" in STATIONS
    assert len(COMMODITIES) == 2


def test_route_lookup():
    # Local
    assert get_route("earth", "earth") == {"rounds": 0, "fuel": 0}
    # Short hop
    r_luna = get_route("earth", "luna")
    assert r_luna["rounds"] == 1
    assert r_luna["fuel"] == 5
    # Long haul
    r_ceres = get_route("earth", "ceres")
    assert r_ceres["rounds"] == 3
    assert r_ceres["fuel"] == 30
    # Symmetric
    r_back = get_route("ceres", "earth")
    assert r_back == r_ceres
    # Invalid
    assert get_route("earth", "pluto") is None


def test_price_engine_determinism():
    eng1 = StationPriceEngine(seed=42)
    eng2 = StationPriceEngine(seed=42)

    p1 = eng1.step_round(1)
    p2 = eng2.step_round(1)

    assert [x.to_dict() for x in p1] == [x.to_dict() for x in p2]
    assert eng1.get_prices() == eng2.get_prices()


def test_galnet_drift_coupling():
    galnet = GalNetEngine(seed=999)
    # Force Ceres fuel shock (+0.40 drift)
    galnet.force_shock(1, template_idx=0)

    engine_clean = StationPriceEngine(seed=123)
    engine_shocked = StationPriceEngine(seed=123)

    p_clean = engine_clean.step_round(1, galnet_engine=None)
    p_shocked = engine_shocked.step_round(1, galnet_engine=galnet)

    ceres_fuel_clean = engine_clean.get_station_price("ceres", "FUEL")
    ceres_fuel_shocked = engine_shocked.get_station_price("ceres", "FUEL")

    # With +0.40 drift, shocked price should be significantly higher than clean price
    assert ceres_fuel_shocked > ceres_fuel_clean


def test_referee_spatial_integration():
    from agora.referee import AgoraReferee
    ref = AgoraReferee()

    # 1. Fleet initially docked at ceres
    locs = ref.get_all_vessel_locations()
    agent_locs = {l['agent_id']: l['station_id'] for l in locs}
    assert agent_locs.get('amos') == 'ceres'
    assert agent_locs.get('zero') == 'ceres'

    # 2. Database price persistence on step_round
    step_result = ref.step_round(1)
    assert step_result['round'] == 1
    assert 'ceres' in step_result['prices']

    cur = ref.conn.cursor()
    cur.execute("SELECT COUNT(*) FROM station_prices WHERE round = 1")
    count = cur.fetchone()[0]
    # 4 stations * 2 commodities = 8 rows
    assert count == 8

    # 3. Solvency double-spend prevention across multiple station books
    # amos has 10,000 CR
    # amos places bid on ceres: 50 qty @ 100 limit = 5000 CR committed
    res1 = ref.submit_envelope({
        'kind': 'order',
        'payload': {
            'order_id': 'bid-ceres-1',
            'agent_id': 'amos',
            'station_id': 'ceres',
            'instrument': 'FRAG',
            'side': 'bid',
            'qty': 50,
            'limit_price': 100,
            'seq_seen': 0
        }
    })
    assert res1.get('kind') == 'market_tick'

    # amos now has 5,000 CR available. Attempting another bid requiring 6,000 CR must fail
    res2 = ref.submit_envelope({
        'kind': 'order',
        'payload': {
            'order_id': 'bid-ceres-2',
            'agent_id': 'amos',
            'station_id': 'ceres',
            'instrument': 'FRAG',
            'side': 'bid',
            'qty': 60,
            'limit_price': 100,
            'seq_seen': 0
        }
    })
    assert res2.get('kind') == 'reject'
    assert res2['payload']['reason'] == 'insufficient_balance'

    # 4. Invariant audit
    valid, errors = ref.verify_ledger_invariants()
    assert valid is True
    assert len(errors) == 0
