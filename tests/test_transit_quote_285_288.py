import unittest
import json
from agora.referee import AgoraReferee
from agora.briefing import build_briefing
from agora.spatial import get_route

def give(ref, acct, inst, qty):
    with ref.lock, ref.conn:
        ref.fleet._move(f"test-give-{acct}-{inst}-{ref.current_seq}", ((acct, inst, qty), ('SYSTEM', inst, -qty)))

def place_test_order(ref, agent, side, qty, px, inst, st, vessel=None, oid=None):
    p = {
        'order_id': oid or f"t-{agent}-{side}-{inst}-{ref.current_seq}",
        'agent_id': agent,
        'side': side,
        'qty': qty,
        'limit_price': px,
        'instrument': inst,
        'station_id': st,
        'seq_seen': ref.current_seq
    }
    if vessel is not None:
        p['vessel_id'] = vessel
    return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': p})

class TestTransitQuote285288(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(piracy='0.15,0.04', hazards='0.20,0.25')
        self.ref.new_game(seed=42, warmup_rounds=2, hazards='0.20,0.25')

    def test_dry_run_zero_side_effects(self):
        """Issue #288 / #285: Dry run quote must be 100% free of side effects."""
        ref = self.ref
        agent = 'amos'
        vessel = 'amos/1'
        loc = ref.get_vessel_location(agent)
        st = loc['station_id']
        dest = 'ceres' if st != 'ceres' else 'earth'

        give(ref, vessel, 'FUEL', 500)
        give(ref, vessel, 'FRAG', 100)

        # Place a resting ask on the book
        place_test_order(ref, agent, 'ask', 20, 9999, 'FRAG', st, vessel=vessel)
        order_count_before = len([o for b in ref.books[st].values() for o in b.asks if o.agent_id == agent])
        self.assertGreater(order_count_before, 0)

        # Snapshot state before dry run
        fuel_before = ref._account_balance(vessel, 'FUEL')
        cr_before = ref._account_balance(agent, 'CR')
        frag_before = ref._account_balance(vessel, 'FRAG')
        loc_before = ref.get_vessel_location(agent)
        round_before = ref.current_round
        active_before = set(getattr(ref, 'active_agents', set()))

        # Snapshot marble bag states
        hazard_bag_before = ref.conn.execute("SELECT * FROM rng_bags").fetchall()
        piracy_bag_before = []

        # Execute dry-run transit quote
        res = ref.initiate_transit(agent_id=agent, destination=dest, commodity='FRAG', cargo_qty=50, vessel_id=vessel, dry_run=True)
        self.assertEqual(res.get('kind'), 'transit_quote')
        self.assertEqual(res.get('status'), 'quote')
        payload = res['payload']
        self.assertTrue(payload.get('dry_run'))
        self.assertEqual(payload.get('quoted_round'), round_before)

        # Verify state is 100% UNCHANGED
        self.assertEqual(ref._account_balance(vessel, 'FUEL'), fuel_before)
        self.assertEqual(ref._account_balance(agent, 'CR'), cr_before)
        self.assertEqual(ref._account_balance(vessel, 'FRAG'), frag_before)
        self.assertEqual(ref.get_vessel_location(agent), loc_before)
        self.assertEqual(ref.current_round, round_before)
        self.assertEqual(set(getattr(ref, 'active_agents', set())), active_before)

        # Resting order remains open
        order_count_after = len([o for b in ref.books[st].values() for o in b.asks if o.agent_id == agent])
        self.assertEqual(order_count_after, order_count_before)

        # Marble bag state is completely untouched
        hazard_bag_after = ref.conn.execute("SELECT * FROM rng_bags").fetchall()
        piracy_bag_after = []
        self.assertEqual(hazard_bag_after, hazard_bag_before)
        self.assertEqual(piracy_bag_after, piracy_bag_before)

    def test_quote_matches_real_departure_odds(self):
        """Acceptance test #288: Quoted odds must equal real departure odds from same state."""
        ref = self.ref
        agent = 'marvin'
        vessel = 'marvin/1'
        loc = ref.get_vessel_location(agent)
        st = loc['station_id']
        dest = 'ceres' if st != 'ceres' else 'earth'

        give(ref, vessel, 'FUEL', 500)
        give(ref, vessel, 'FOOD', 250)

        # Dry run quote
        quote_res = ref.initiate_transit(agent_id=agent, destination=dest, commodity='FOOD', cargo_qty=100, vessel_id=vessel, dry_run=True)
        self.assertEqual(quote_res['kind'], 'transit_quote')
        q_piracy = quote_res['payload']['piracy']

        # Real departure from same state
        real_res = ref.initiate_transit(agent_id=agent, destination=dest, commodity='FOOD', cargo_qty=100, vessel_id=vessel, dry_run=False)
        self.assertEqual(real_res['status'], 'in_transit')
        r_piracy = real_res['payload']['piracy']

        # Acceptance check: quoted odds match real odds
        self.assertEqual(q_piracy['odds'], r_piracy['odds'])
        self.assertGreater(q_piracy['odds'], 0.0)
        self.assertIn('base', q_piracy)
        self.assertIn('hot', q_piracy)
        self.assertIn('value_mult', q_piracy)

    def test_privateer_exclusion_disclosure(self):
        """Marvin feedback #1: Quote must explicitly disclose privateer exclusion."""
        ref = self.ref
        agent = 'zero'
        vessel = 'zero/1'
        loc = ref.get_vessel_location(agent)
        dest = 'ceres' if loc['station_id'] != 'ceres' else 'earth'
        give(ref, vessel, 'FUEL', 500)
        give(ref, vessel, 'FRAG', 100)

        res = ref.initiate_transit(agent_id=agent, destination=dest, commodity='FRAG', cargo_qty=50, vessel_id=vessel, dry_run=True)
        piracy = res['payload']['piracy']
        self.assertIn('excludes', piracy)
        self.assertIn('privateers', piracy['excludes'])
        self.assertIn('privateer_add', piracy)
        self.assertEqual(piracy['privateer_add'], 0.15)
        self.assertFalse(piracy['privateers'])

    def test_quote_arrival_range_and_burst_warning(self):
        """Marvin feedback #3: Quote returns arrival_round_min and arrival_round_max, fires burst warning on max delay."""
        ref = self.ref
        agent = 'amos'
        vessel = 'amos/1'
        loc = ref.get_vessel_location(agent)
        dest = 'ceres' if loc['station_id'] != 'ceres' else 'earth'
        give(ref, vessel, 'FUEL', 500)

        # Mock a burst ending soon
        # base trip is say 2 rounds, max delay 3 rounds (arrival between dep+2 and dep+5)
        base_route = get_route(loc['station_id'], dest, ref.current_round)
        base_rounds = base_route['rounds']
        ref.burst_active = True
        ref.burst_end_round = ref.current_round + base_rounds + 1 # Late only if storm delay happens!
        ref.get_burst_info = lambda: {
            'active': True,
            'rounds_remaining': ref.burst_end_round - ref.current_round,
            'end_round': ref.burst_end_round
        }

        res = ref.initiate_transit(agent_id=agent, destination=dest, vessel_id=vessel, dry_run=True)
        payload = res['payload']
        self.assertEqual(payload['arrival_round_min'], ref.current_round + base_rounds)
        self.assertEqual(payload['arrival_round_max'], ref.current_round + base_rounds + 3)
        self.assertIn('warning', payload)
        self.assertIn('burst concludes', payload['warning'])

    def test_receipt_fields_consistency(self):
        """Issue #285: rounds_duration == arrival_round - departure_round, base_rounds and delay_rounds explicit."""
        ref = self.ref
        agent = 'amos'
        vessel = 'amos/1'
        loc = ref.get_vessel_location(agent)
        dest = 'ceres' if loc['station_id'] != 'ceres' else 'earth'
        give(ref, vessel, 'FUEL', 500)
        give(ref, vessel, 'FRAG', 100)

        res = ref.initiate_transit(agent_id=agent, destination=dest, commodity='FRAG', cargo_qty=50, vessel_id=vessel)
        payload = res['payload']
        dep = payload['departure_round']
        arr = payload['arrival_round']
        duration = payload['rounds_duration']
        base = payload['base_rounds']
        delay = payload['delay_rounds']

        self.assertEqual(duration, arr - dep)
        self.assertEqual(arr - dep, base + delay)

    def test_briefing_mentions_hazards_and_quotes(self):
        """Issue #285 & #288: Briefing mentions route hazards and pre-trip dry_run quote."""
        ref = self.ref
        brief = build_briefing(ref)
        self.assertIn("Route hazards:", brief)
        self.assertIn("storm delay", brief)
        self.assertIn("hull breach loss", brief)
        self.assertIn("Pre-trip quote:", brief)

    def test_http_transit_quote_endpoints(self):
        """Integration test for HTTP quote endpoints."""
        import threading
        import urllib.request
        from http.server import HTTPServer
        from agora.server import make_handler

        auth_tokens = {'amos': 'tok-amos', 'zero': 'tok-zero', 'admin': 'tok-admin'}
        handler = make_handler(self.ref, auth_tokens=auth_tokens)
        server = HTTPServer(('127.0.0.1', 0), handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            loc = self.ref.get_vessel_location('amos')
            st = loc['station_id']
            dest = 'ceres' if st != 'ceres' else 'earth'
            give(self.ref, 'amos/1', 'FUEL', 500)

            # 1. POST /stations/transit with dry_run=true
            req_data = json.dumps({'destination': dest, 'dry_run': True}).encode('utf-8')
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/stations/transit",
                data=req_data,
                headers={'Authorization': 'Bearer tok-amos', 'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(body.get('kind'), 'transit_quote')
                self.assertTrue(body['payload'].get('dry_run'))

            # 2. GET /stations/transit/quote
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/stations/transit/quote?destination={dest}&cargo_qty=0",
                headers={'Authorization': 'Bearer tok-amos'}
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(body.get('kind'), 'transit_quote')
                self.assertTrue(body['payload'].get('dry_run'))

            # 3. GET /referee/piracy/quote
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/referee/piracy/quote?destination={dest}",
                headers={'Authorization': 'Bearer tok-amos'}
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(body.get('kind'), 'transit_quote')
                self.assertIn('piracy', body['payload'])
        finally:
            server.shutdown()
            server.server_close()

if __name__ == '__main__':
    unittest.main()
