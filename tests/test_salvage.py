"""
tests.test_salvage - Unit and integration tests for derelict salvage claims,
distress beacons, and rescue RFQ extortion engine (Issue #22).
"""

import json
import pytest
from agora.referee import AgoraReferee
from agora.server import make_handler
from http.server import HTTPServer
import threading
import urllib.request
import urllib.error


def test_distress_beacon_declaration():
    ref = AgoraReferee()
    # amos declares distress due to out_of_fuel
    res = ref.broadcast_distress(
        agent_id='amos',
        location='mars_ceres',
        cargo_bounty={'FRAG': 50, 'FUEL': 0},
        fuel_needed=20,
        max_reward_cr=200,
        reason='out_of_fuel'
    )
    assert res['ok'] is True
    beacon_id = res['beacon_id']
    assert res['location'] == 'mars_ceres'
    assert res['cargo_bounty'] == {'FRAG': 50}

    # Duplicate active beacon rejected
    dup = ref.broadcast_distress(agent_id='amos')
    assert dup['ok'] is False
    assert dup['reason'] == 'active_beacon_exists'

    # Check beacons list
    beacons = ref.get_distress_beacons(status='active')
    assert len(beacons) == 1
    assert beacons[0]['beacon_id'] == beacon_id
    assert beacons[0]['agent_id'] == 'amos'
    assert beacons[0]['cargo_bounty'] == {'FRAG': 50}


def test_rescue_rfq_and_quoting():
    ref = AgoraReferee()
    ref.broadcast_distress(
        agent_id='amos',
        location='earth_mars',
        cargo_bounty={'FRAG': 30},
        fuel_needed=15,
        max_reward_cr=300
    )
    rfqs = ref.get_rescue_rfqs(status='open')
    assert len(rfqs) == 1
    rfq_id = rfqs[0]['rfq_id']

    # Self-rescue quote rejected
    self_q = ref.submit_rescue_quote(
        rescuer_id='amos',
        rfq_id=rfq_id,
        fuel_offered=15,
        price_cr=100
    )
    assert self_q['ok'] is False
    assert self_q['reason'] == 'cannot_rescue_self'

    # Insufficient fuel offered rejected
    low_fuel = ref.submit_rescue_quote(
        rescuer_id='marvin',
        rfq_id=rfq_id,
        fuel_offered=10,  # needed 15
        price_cr=150
    )
    assert low_fuel['ok'] is False
    assert low_fuel['reason'] == 'insufficient_fuel_offered'

    # Marvin quotes competitive rescue
    q_marvin = ref.submit_rescue_quote(
        rescuer_id='marvin',
        rfq_id=rfq_id,
        fuel_offered=15,
        price_cr=250
    )
    assert q_marvin['ok'] is True
    assert q_marvin['status'] == 'pending'

    # Zero quotes extortionate rescue (higher price)
    q_zero = ref.submit_rescue_quote(
        rescuer_id='zero',
        rfq_id=rfq_id,
        fuel_offered=20,
        price_cr=400
    )
    assert q_zero['ok'] is True

    # Check RFQs list returns both quotes sorted by price
    rfqs_after = ref.get_rescue_rfqs()
    assert len(rfqs_after[0]['quotes']) == 2
    assert rfqs_after[0]['quotes'][0]['rescuer_id'] == 'marvin'
    assert rfqs_after[0]['quotes'][0]['price_cr'] == 250
    assert rfqs_after[0]['quotes'][1]['rescuer_id'] == 'zero'
    assert rfqs_after[0]['quotes'][1]['price_cr'] == 400


def test_atomic_rescue_settlement_and_invariants():
    ref = AgoraReferee()
    # Baseline balances: amos has 10,000 CR, 500 FUEL; marvin has 10,000 CR, 500 FUEL
    amos_cr_before = ref.get_balance('amos', 'CR')
    amos_fuel_before = ref.get_balance('amos', 'FUEL')
    marvin_cr_before = ref.get_balance('marvin', 'CR')
    marvin_fuel_before = ref.get_balance('marvin', 'FUEL')

    distress = ref.broadcast_distress(
        agent_id='amos',
        location='ceres',
        fuel_needed=25,
        max_reward_cr=500
    )
    rfq_id = distress['rfq_id']
    beacon_id = distress['beacon_id']

    quote = ref.submit_rescue_quote(
        rescuer_id='marvin',
        rfq_id=rfq_id,
        fuel_offered=25,
        price_cr=350  # 350 CR for 25 FUEL
    )
    quote_id = quote['quote_id']

    # Non-owner cannot accept quote
    unauth = ref.accept_rescue_quote(agent_id='zero', quote_id=quote_id)
    assert unauth['ok'] is False
    assert unauth['reason'] == 'unauthorized'

    # Amos accepts Marvin's rescue quote
    settle = ref.accept_rescue_quote(agent_id='amos', quote_id=quote_id)
    assert settle['ok'] is True
    assert settle['status'] == 'rescued'

    # Balances verify
    assert ref.get_balance('amos', 'CR') == amos_cr_before - 350
    assert ref.get_balance('marvin', 'CR') == marvin_cr_before + 350
    assert ref.get_balance('amos', 'FUEL') == amos_fuel_before + 25
    assert ref.get_balance('marvin', 'FUEL') == marvin_fuel_before - 25

    # Beacon status updated
    beacons = ref.get_distress_beacons()
    rescued_b = next(b for b in beacons if b['beacon_id'] == beacon_id)
    assert rescued_b['status'] == 'rescued'
    assert rescued_b['rescued_by'] == 'marvin'

    # Ledger invariants check
    valid, errors = ref.verify_ledger_invariants()
    assert valid is True, f"Invariant errors: {errors}"


def test_derelict_salvage_claim_and_cargo_transfer():
    ref = AgoraReferee()
    # Zero has 1000 FRAG initially
    # Zero declares distress with 100 FRAG cargo bounty
    zero_frag_before = ref.get_balance('zero', 'FRAG')
    marvin_frag_before = ref.get_balance('marvin', 'FRAG')

    distress = ref.broadcast_distress(
        agent_id='zero',
        location='ceres_mars',
        cargo_bounty={'FRAG': 100},
        fuel_needed=30
    )
    beacon_id = distress['beacon_id']

    # Owner cannot salvage own vessel
    self_salvage = ref.claim_salvage(salvager_id='zero', beacon_id=beacon_id)
    assert self_salvage['ok'] is False
    assert self_salvage['reason'] == 'cannot_salvage_self'

    # Marvin claims the derelict salvage
    claim_res = ref.claim_salvage(salvager_id='marvin', beacon_id=beacon_id)
    assert claim_res['ok'] is True
    assert claim_res['status'] == 'salvaged'
    assert claim_res['cargo_claimed'] == {'FRAG': 100}

    # Verify cargo transfer: zero -> marvin
    assert ref.get_balance('zero', 'FRAG') == zero_frag_before - 100
    assert ref.get_balance('marvin', 'FRAG') == marvin_frag_before + 100

    # Beacon status updated
    beacons = ref.get_distress_beacons(status='salvaged')
    assert len(beacons) == 1
    assert beacons[0]['beacon_id'] == beacon_id
    assert beacons[0]['salvaged_by'] == 'marvin'

    # Second claim rejected (already salvaged)
    claim_dup = ref.claim_salvage(salvager_id='amos', beacon_id=beacon_id)
    assert claim_dup['ok'] is False
    assert claim_dup['reason'] == 'beacon_not_active'

    # Verify ledger invariants
    valid, errors = ref.verify_ledger_invariants()
    assert valid is True, f"Invariant errors: {errors}"


def test_server_salvage_http_endpoints():
    ref = AgoraReferee()
    tokens = {
        'amos': 'token-amos',
        'marvin': 'token-marvin',
        'zero': 'token-zero',
        'admin': 'token-admin'
    }
    handler = make_handler(ref, auth_tokens=tokens)
    server = HTTPServer(('127.0.0.1', 0), handler)
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()

    base_url = f"http://127.0.0.1:{port}"

    try:
        # 1. GET /salvage/summary initially empty
        req = urllib.request.Request(f"{base_url}/salvage/summary")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            assert data['status'] == 'ok'
            assert data['summary']['active_beacons'] == 0

        # 2. POST /salvage/distress without auth fails 401
        post_data = json.dumps({'location': 'earth', 'fuel_needed': 15}).encode()
        req_unauth = urllib.request.Request(f"{base_url}/salvage/distress", data=post_data, headers={'Content-Type': 'application/json'})
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_unauth)
        assert exc_info.value.code == 401

        # 3. POST /salvage/distress as amos
        req_amos = urllib.request.Request(
            f"{base_url}/salvage/distress",
            data=json.dumps({
                'location': 'earth',
                'cargo_bounty': {'FRAG': 20},
                'fuel_needed': 15,
                'max_reward_cr': 100
            }).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer token-amos'
            }
        )
        with urllib.request.urlopen(req_amos) as resp:
            data = json.loads(resp.read().decode())
            assert data['ok'] is True
            beacon_id = data['beacon_id']
            rfq_id = data['rfq_id']

        # 4. GET /salvage/beacons returns active beacon
        req_b = urllib.request.Request(f"{base_url}/salvage/beacons?status=active")
        with urllib.request.urlopen(req_b) as resp:
            data = json.loads(resp.read().decode())
            assert len(data['beacons']) == 1
            assert data['beacons'][0]['beacon_id'] == beacon_id

        # 5. POST /salvage/quote as marvin
        req_quote = urllib.request.Request(
            f"{base_url}/salvage/quote",
            data=json.dumps({
                'rfq_id': rfq_id,
                'fuel_offered': 15,
                'price_cr': 80
            }).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer token-marvin'
            }
        )
        with urllib.request.urlopen(req_quote) as resp:
            data = json.loads(resp.read().decode())
            assert data['ok'] is True
            quote_id = data['quote_id']

        # 6. POST /salvage/accept_quote as amos
        req_accept = urllib.request.Request(
            f"{base_url}/salvage/accept_quote",
            data=json.dumps({'quote_id': quote_id}).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer token-amos'
            }
        )
        with urllib.request.urlopen(req_accept) as resp:
            data = json.loads(resp.read().decode())
            assert data['ok'] is True
            assert data['status'] == 'rescued'

        # 7. GET /salvage/summary shows rescued_count == 1
        with urllib.request.urlopen(f"{base_url}/salvage/summary") as resp:
            data = json.loads(resp.read().decode())
            assert data['summary']['rescued_count'] == 1
            assert data['summary']['active_beacons'] == 0

    finally:
        server.shutdown()
        server.server_close()
