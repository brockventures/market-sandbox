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


def test_orbital_windows_lifecycle():
    from agora.spatial import get_alignment_windows, get_active_window_for_route

    # Round 0: all corridors inactive
    windows_r0 = get_alignment_windows(0)
    assert len(windows_r0) == 3
    assert all(not w['is_active'] for w in windows_r0)
    assert get_active_window_for_route("earth", "mars", 0) is None

    # Base route check at round 0
    r_em_0 = get_route("earth", "mars", 0)
    assert r_em_0['rounds'] == 2
    assert r_em_0['fuel'] == 15
    assert r_em_0['is_aligned'] is False

    # Round 4: Earth-Mars Opposition active!
    windows_r4 = get_alignment_windows(4)
    em_w = next(w for w in windows_r4 if w['corridor_id'] == 'earth_mars')
    assert em_w['is_active'] is True
    assert em_w['rounds_remaining'] == 2  # active on rounds 4 and 5

    r_em_4 = get_route("earth", "mars", 4)
    assert r_em_4['is_aligned'] is True
    assert r_em_4['rounds'] == 1          # Halved from 2 to 1 round!
    assert r_em_4['fuel'] == 10           # Reduced from 15 to 10 fuel!
    assert r_em_4['rounds_remaining'] == 2

    # Round 5: Earth-Mars still active, Mars-Ceres also active!
    windows_r5 = get_alignment_windows(5)
    mc_w = next(w for w in windows_r5 if w['corridor_id'] == 'mars_ceres')
    assert mc_w['is_active'] is True

    r_mc_5 = get_route("mars", "ceres", 5)
    assert r_mc_5['is_aligned'] is True
    assert r_mc_5['rounds'] == 1          # Halved from 2 to 1 round!
    assert r_mc_5['fuel'] == 12           # Reduced from 20 to 12 fuel!

    # Round 6: Earth-Ceres active!
    r_ec_6 = get_route("earth", "ceres", 6)
    assert r_ec_6['is_aligned'] is True
    assert r_ec_6['rounds'] == 2          # Reduced from 3 to 2 rounds!
    assert r_ec_6['fuel'] == 18           # Reduced from 30 to 18 fuel!


def test_asteroid_belt_toll_booths_and_solvency():
    from agora.referee import AgoraReferee
    ref = AgoraReferee()

    # ceres -> mars is a belt route, requires 25 CR toll
    route = get_route("ceres", "mars", 0)
    assert route['is_belt_route'] is True
    assert route['toll'] == 25

    # amos starts with 10,000 CR at ceres
    init_cr = ref.get_balance('amos', 'CR')
    init_fuel = ref.get_balance('amos', 'FUEL')
    init_system_cr = ref.get_balance('SYSTEM', 'CR')

    # Transit ceres -> mars with 0 cargo
    res = ref.initiate_transit('amos', 'mars', cargo_qty=0)
    assert res.get('kind') == 'status'
    assert res['status'] == 'in_transit'
    assert res['payload']['toll_paid'] == 25
    assert res['payload']['fuel_burned'] == 20

    # Verify CR and FUEL debits to SYSTEM
    assert ref.get_balance('amos', 'CR') == init_cr - 25
    assert ref.get_balance('amos', 'FUEL') == init_fuel - 20
    assert ref.get_balance('SYSTEM', 'CR') == init_system_cr + 25

    # Invariants strictly hold (conservation sum(delta)==0, non-negativity)
    valid, errors = ref.verify_ledger_invariants()
    assert valid is True
    assert len(errors) == 0


def test_perishable_cargo_decay_mechanics():
    from agora.referee import AgoraReferee
    ref = AgoraReferee()

    # Step to round 4 (Earth-Mars alignment active)
    # amos docked at ceres. Move amos to mars first
    ref.initiate_transit('amos', 'mars', cargo_qty=0)
    ref.step_round(2)  # arrives at mars

    # Zero docked at ceres. Zero has 10,000 CR, 500 FUEL, 1,000 FRAG.
    # Transit ceres -> earth (3 rounds, belt route with 5% decay per round)
    init_frag = ref.get_balance('zero', 'FRAG')
    init_cr = ref.get_balance('zero', 'CR')

    # Non-perishable cargo transit (standard durable scrap)
    res_durable = ref.initiate_transit('zero', 'earth', commodity='FRAG', cargo_qty=100, perishable=False)
    assert res_durable['status'] == 'in_transit'
    assert res_durable['payload']['perishable'] is False

    # Step to round 5 (dep was 2, arrival round is 2 + 3 = 5)
    step_res = ref.step_round(5)
    assert len(step_res['arrived_transits']) == 1
    arr = step_res['arrived_transits'][0]
    assert arr['cargo_delivered'] == 100
    assert arr['cargo_decayed'] == 0
    # Full cargo returned, zero back to 1,000 FRAG
    assert ref.get_balance('zero', 'FRAG') == init_frag

    # Now test PERISHABLE cargo decay from earth -> ceres
    # Route earth -> ceres: 3 rounds transit, belt route (decay_rate = 0.05)
    # 5% decay per round * 3 rounds = 15% decay!
    # With 100 cargo: floor(100 * 0.15) = 15 units decayed, 85 units delivered!
    res_perish = ref.initiate_transit('zero', 'ceres', commodity='FRAG', cargo_qty=100, perishable=True)
    assert res_perish['status'] == 'in_transit'
    assert res_perish['payload']['perishable'] is True

    # Step 3 rounds forward: 5 + 3 = round 8
    step_res2 = ref.step_round(8)
    assert len(step_res2['arrived_transits']) == 1
    arr_perish = step_res2['arrived_transits'][0]
    assert arr_perish['cargo_decayed'] == 15
    assert arr_perish['cargo_delivered'] == 85

    # Zero account received 85 FRAG; 15 units decayed in belt
    assert ref.get_balance('zero', 'FRAG') == init_frag - 15

    # Double-entry ledger conservation check: sum(delta) == 0 for all txns
    valid, errors = ref.verify_ledger_invariants()
    assert valid is True
    assert len(errors) == 0

