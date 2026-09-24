"""
tests.test_salvage - Unit and integration tests for derelict salvage claims,
distress beacons, and rescue RFQ extortion engine (Issue #22).
"""

import json
import unittest
from agora.referee import AgoraReferee
from agora.server import make_handler
from http.server import HTTPServer
import threading
import urllib.request
import urllib.error



class TestSalvage(unittest.TestCase):
    def test_distress_beacon_declaration(self):
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


    def test_rescue_rfq_and_quoting(self):
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


    def test_atomic_rescue_settlement_and_invariants(self):
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


    def test_derelict_salvage_claim_and_cargo_transfer(self):
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


    def test_server_salvage_http_endpoints(self):
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
            with self.assertRaises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req_unauth)
            assert exc_info.exception.code == 401

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


if __name__ == "__main__":
    unittest.main()


class TestSalvageDocksStrandedFleet(unittest.TestCase):
    """#198: a salvage claim on a transit-linked beacon cancelled the transit
    but left the fleet 'in_transit' forever: it then read as docked at a
    station called "in_transit" and every MOVE was rejected invalid_route."""

    def _vessel_row(self, ref, agent):
        return ref.conn.execute("SELECT station_id, status FROM vessels WHERE vessel_id = ?",
                                (f"{agent}/1",)).fetchone()

    def _loc_row(self, ref, agent):
        return ref.conn.execute("SELECT station_id FROM vessel_locations WHERE agent_id = ?",
                                (agent,)).fetchone()

    def test_claim_docks_fleet_at_origin_and_it_can_move_again(self):
        ref = AgoraReferee(':memory:')
        origin = ref.get_vessel_location('marvin')['station_id']
        dest = 'mars' if origin != 'mars' else 'luna'
        t = ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=0)
        self.assertEqual(t['status'], 'in_transit', t)
        tid = t['payload']['transit_id']
        d = ref.broadcast_distress(agent_id='marvin', location='in_transit', cargo_bounty=None,
                                   transit_id=tid, fuel_needed=15, max_reward_cr=0, reason='out_of_fuel')
        self.assertTrue(d['ok'], d)
        c = ref.claim_salvage(salvager_id='zero', beacon_id=d['beacon_id'])
        self.assertTrue(c['ok'], c)

        # Transit cancelled, fleet docked back at the origin in both tables.
        self.assertEqual(ref.conn.execute("SELECT status FROM transits WHERE transit_id = ?",
                                          (tid,)).fetchone()[0], 'cancelled')
        loc = ref.get_vessel_location('marvin')
        self.assertEqual((loc['status'], loc['station_id']), ('docked', origin))
        self.assertEqual(self._loc_row(ref, 'marvin')[0], origin)
        v = self._vessel_row(ref, 'marvin')
        self.assertEqual((v['station_id'], v['status']), (origin, 'docked'))

        # Past the old arrival round nothing lands and nothing changes.
        for _ in range(8):
            ref.step_round()
        loc = ref.get_vessel_location('marvin')
        self.assertEqual((loc['status'], loc['station_id']), ('docked', origin))

        # And the fleet can MOVE again.
        again = ref.initiate_transit('marvin', dest)
        self.assertEqual(again['status'], 'in_transit', again)
        self.assertEqual(again['payload']['origin'], origin)
        self.assertEqual(ref.get_vessel_location('marvin')['status'], 'in_transit')

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_claim_on_already_arrived_transit_leaves_it_alone(self):
        # #211: A claim targeting a beacon whose vessel already arrived is rejected with 400
        # and does not consume the beacon.
        ref = AgoraReferee(':memory:')
        origin = ref.get_vessel_location('marvin')['station_id']
        dest = 'mars' if origin != 'mars' else 'luna'
        t = ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=0)
        tid = t['payload']['transit_id']
        d = ref.broadcast_distress(agent_id='marvin', transit_id=tid, fuel_needed=15)
        self.assertTrue(d['ok'], d)
        for _ in range(12):
            ref.step_round()
        self.assertEqual(ref.conn.execute("SELECT status FROM transits WHERE transit_id = ?",
                                          (tid,)).fetchone()[0], 'arrived')
        self.assertEqual(ref.get_vessel_location('marvin')['station_id'], dest)

        claim_res = ref.claim_salvage(salvager_id='zero', beacon_id=d['beacon_id'])
        self.assertFalse(claim_res['ok'])
        self.assertEqual(claim_res['reason'], 'transit_already_arrived')
        # Beacon must NOT be consumed or marked salvaged (#211)
        b_status = ref.conn.execute("SELECT status FROM distress_beacons WHERE beacon_id = ?", (d['beacon_id'],)).fetchone()[0]
        self.assertEqual(b_status, 'active')

        # The arrived transit is not rewritten, and the ship stays where it docked.
        self.assertEqual(ref.conn.execute("SELECT status FROM transits WHERE transit_id = ?",
                                          (tid,)).fetchone()[0], 'arrived')
        loc = ref.get_vessel_location('marvin')
        self.assertEqual((loc['status'], loc['station_id']), ('docked', dest))
        v = self._vessel_row(ref, 'marvin')
        self.assertEqual((v['station_id'], v['status']), (dest, 'docked'))
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_broadcast_distress_on_arrived_transit_rejected(self):
        # #211: Cannot broadcast distress on transit that already arrived
        ref = AgoraReferee(':memory:')
        origin = ref.get_vessel_location('marvin')['station_id']
        dest = 'mars' if origin != 'mars' else 'luna'
        t = ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=0)
        tid = t['payload']['transit_id']
        for _ in range(12):
            ref.step_round()
        res = ref.broadcast_distress(agent_id='marvin', transit_id=tid)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'transit_already_arrived')


class TestSalvageBountyCappedAndEscrowReturned(unittest.TestCase):
    """#205: salvage bounty capped at transit cargo; leftover escrow returned to stranded fleet."""

    def test_claim_caps_bounty_at_transit_escrow_and_returns_leftover(self):
        ref = AgoraReferee(':memory:')
        origin = ref.get_vessel_location('marvin')['station_id']
        dest = 'mars' if origin != 'mars' else 'luna'

        # Record initial balances
        marvin_initial = ref.get_balance('marvin', 'FRAG')
        zero_initial = ref.get_balance('zero', 'FRAG')
        sys_initial = ref.get_balance('SYSTEM', 'FRAG')

        # Marvin departs with 100 FRAG
        t = ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=100)
        self.assertEqual(t['status'], 'in_transit')
        tid = t['payload']['transit_id']

        self.assertEqual(ref.get_balance('marvin', 'FRAG'), marvin_initial - 100)
        self.assertEqual(ref.get_balance('SYSTEM', 'FRAG'), sys_initial + 100)

        # Marvin declares distress offering 40 FRAG bounty out of the 100 aboard
        d = ref.broadcast_distress(
            agent_id='marvin',
            location='in_transit',
            cargo_bounty={'FRAG': 40},
            transit_id=tid,
            fuel_needed=15
        )
        self.assertTrue(d['ok'], d)
        self.assertEqual(d['cargo_bounty'], {'FRAG': 40})

        # Zero claims salvage
        c = ref.claim_salvage(salvager_id='zero', beacon_id=d['beacon_id'])
        self.assertTrue(c['ok'], c)
        self.assertEqual(c['cargo_claimed'], {'FRAG': 40})

        # Zero receives 40 FRAG bounty
        self.assertEqual(ref.get_balance('zero', 'FRAG'), zero_initial + 40)
        # Marvin receives leftover 60 FRAG refunded
        self.assertEqual(ref.get_balance('marvin', 'FRAG'), marvin_initial - 40)
        # SYSTEM escrow is completely cleared back to initial baseline
        self.assertEqual(ref.get_balance('SYSTEM', 'FRAG'), sys_initial)

        # Transit cancelled and vessel docked at origin
        self.assertEqual(ref.conn.execute("SELECT status FROM transits WHERE transit_id = ?", (tid,)).fetchone()[0], 'cancelled')
        self.assertEqual(ref.get_vessel_location('marvin')['station_id'], origin)
        self.assertEqual(ref.get_vessel_location('marvin')['status'], 'docked')

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_declared_bounty_exceeding_transit_cargo_is_capped_at_cargo(self):
        ref = AgoraReferee(':memory:')
        origin = ref.get_vessel_location('marvin')['station_id']
        dest = 'mars' if origin != 'mars' else 'luna'

        marvin_initial = ref.get_balance('marvin', 'FRAG')
        zero_initial = ref.get_balance('zero', 'FRAG')
        sys_initial = ref.get_balance('SYSTEM', 'FRAG')

        # Marvin departs with 50 FRAG
        t = ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=50)
        tid = t['payload']['transit_id']

        # Stranded fleet tries to offer 200 FRAG and 100 FOOD when transit only has 50 FRAG
        d = ref.broadcast_distress(
            agent_id='marvin',
            cargo_bounty={'FRAG': 200, 'FOOD': 100},
            transit_id=tid,
            fuel_needed=15
        )
        self.assertTrue(d['ok'], d)
        # Capped to 50 FRAG at broadcast
        self.assertEqual(d['cargo_bounty'], {'FRAG': 50})

        c = ref.claim_salvage(salvager_id='zero', beacon_id=d['beacon_id'])
        self.assertTrue(c['ok'], c)
        self.assertEqual(c['cargo_claimed'], {'FRAG': 50})

        # Zero receives 50 FRAG, not 200
        self.assertEqual(ref.get_balance('zero', 'FRAG'), zero_initial + 50)
        # Marvin has 0 refund (all 50 went to bounty)
        self.assertEqual(ref.get_balance('marvin', 'FRAG'), marvin_initial - 50)
        # SYSTEM escrow is completely restored
        self.assertEqual(ref.get_balance('SYSTEM', 'FRAG'), sys_initial)

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_default_bounty_claims_all_transit_cargo_cleanly(self):
        ref = AgoraReferee(':memory:')
        origin = ref.get_vessel_location('marvin')['station_id']
        dest = 'mars' if origin != 'mars' else 'luna'

        marvin_initial = ref.get_balance('marvin', 'FRAG')
        zero_initial = ref.get_balance('zero', 'FRAG')
        sys_initial = ref.get_balance('SYSTEM', 'FRAG')

        # Marvin departs with 70 FRAG
        t = ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=70)
        tid = t['payload']['transit_id']

        # Omitting cargo_bounty defaults to all transit cargo
        d = ref.broadcast_distress(agent_id='marvin', transit_id=tid, cargo_bounty=None)
        self.assertTrue(d['ok'], d)
        self.assertEqual(d['cargo_bounty'], {'FRAG': 70})

        c = ref.claim_salvage(salvager_id='zero', beacon_id=d['beacon_id'])
        self.assertTrue(c['ok'], c)
        self.assertEqual(c['cargo_claimed'], {'FRAG': 70})

        self.assertEqual(ref.get_balance('zero', 'FRAG'), zero_initial + 70)
        self.assertEqual(ref.get_balance('marvin', 'FRAG'), marvin_initial - 70)
        self.assertEqual(ref.get_balance('SYSTEM', 'FRAG'), sys_initial)

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_zero_bounty_declared_refunds_all_escrow_to_stranded_agent(self):
        ref = AgoraReferee(':memory:')
        origin = ref.get_vessel_location('marvin')['station_id']
        dest = 'mars' if origin != 'mars' else 'luna'

        marvin_initial = ref.get_balance('marvin', 'FRAG')
        zero_initial = ref.get_balance('zero', 'FRAG')
        sys_initial = ref.get_balance('SYSTEM', 'FRAG')

        # Marvin departs with 85 FRAG
        t = ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=85)
        tid = t['payload']['transit_id']

        # Zero bounty declared
        d = ref.broadcast_distress(agent_id='marvin', transit_id=tid, cargo_bounty={'FRAG': 0})
        self.assertTrue(d['ok'], d)
        self.assertEqual(d['cargo_bounty'], {})

        c = ref.claim_salvage(salvager_id='zero', beacon_id=d['beacon_id'])
        self.assertTrue(c['ok'], c)
        self.assertEqual(c['cargo_claimed'], {})

        # Zero receives 0, Marvin receives full 85 refund
        self.assertEqual(ref.get_balance('zero', 'FRAG'), zero_initial)
        self.assertEqual(ref.get_balance('marvin', 'FRAG'), marvin_initial)
        self.assertEqual(ref.get_balance('SYSTEM', 'FRAG'), sys_initial)

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_perishable_escrow_refund_decays_in_flight_spoilage(self):
        # #211: Leftover cargo escrow refunded on cancellation accounts for elapsed transit spoilage
        ref = AgoraReferee(':memory:')
        # marvin has 1000 FRAG at marvin/1 on ceres. Fly ceres -> earth (3 rounds, decay_rate = 0.05)
        marvin_initial = ref.get_balance('marvin/1', 'FRAG')
        zero_initial = ref.get_balance('zero/1', 'FRAG')
        sys_initial = ref.get_balance('SYSTEM', 'FRAG')

        t = ref.initiate_transit('marvin', 'earth', commodity='FRAG', cargo_qty=100, perishable=True)
        self.assertEqual(t.get('status'), 'in_transit')
        tid = t['payload']['transit_id']

        # Advance 2 rounds: elapsed = 2 rounds. Expected decay: 100 * 0.05 * 2 = 10 FRAG
        ref.step_round()
        ref.step_round()

        d = ref.broadcast_distress(agent_id='marvin', transit_id=tid, cargo_bounty={'FRAG': 30})
        self.assertTrue(d['ok'], d)
        bid = d['beacon_id']

        c = ref.claim_salvage(salvager_id='zero', beacon_id=bid)
        self.assertTrue(c['ok'], c)
        # Salvager claims 30 FRAG bounty
        self.assertEqual(c['cargo_claimed'], {'FRAG': 30})

        # Surviving cargo was 100 - 10 = 90 FRAG.
        # Leftover refund to Marvin is 90 - 30 = 60 FRAG (NOT 100 - 30 = 70 FRAG).
        self.assertEqual(ref.get_balance('zero/1', 'FRAG'), zero_initial + 30)
        self.assertEqual(ref.get_balance('marvin/1', 'FRAG'), marvin_initial - 100 + 60)

        # Transit table recorded decayed_qty = 10
        row = ref.conn.execute("SELECT status, cargo_qty, decayed_qty FROM transits WHERE transit_id = ?", (tid,)).fetchone()
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['decayed_qty'], 10)

        # Ledger invariants hold
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)

    def test_perishable_self_salvage_exploit_prevented(self):
        # #211: Zero-bounty salvage on perishable transit refunds surviving cargo, not 100%
        ref = AgoraReferee(':memory:')
        marvin_initial = ref.get_balance('marvin/1', 'FRAG')
        sys_initial = ref.get_balance('SYSTEM', 'FRAG')

        t = ref.initiate_transit('marvin', 'earth', commodity='FRAG', cargo_qty=100, perishable=True)
        tid = t['payload']['transit_id']

        # 2 rounds elapse -> 10 FRAG decays
        ref.step_round()
        ref.step_round()

        # Zero bounty declared
        d = ref.broadcast_distress(agent_id='marvin', transit_id=tid, cargo_bounty={'FRAG': 0})
        self.assertTrue(d['ok'], d)

        c = ref.claim_salvage(salvager_id='zero', beacon_id=d['beacon_id'])
        self.assertTrue(c['ok'], c)
        self.assertEqual(c['cargo_claimed'], {})

        # Marvin receives 90 refund, losing 10 to spoilage (preventing 100% refund exploit)
        self.assertEqual(ref.get_balance('marvin/1', 'FRAG'), marvin_initial - 100 + 90)

        # Decayed cargo recorded
        row = ref.conn.execute("SELECT status, decayed_qty FROM transits WHERE transit_id = ?", (tid,)).fetchone()
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['decayed_qty'], 10)

        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)
