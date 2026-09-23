"""Station order flow (agora/order_flow.py, #162): NPC buyers and sellers
fill resting fleet orders before the depot, sized from the depot's drips."""

import os
import unittest
from unittest import mock

from agora import order_flow as OF
from agora.referee import AgoraReferee, REACTIVE_MAIN_DRIP, REACTIVE_SIDE_DRIP
from agora.server import build_referee_from_env


def game(seed=1, flow=True):
    ref = AgoraReferee(depots=True, asymmetric=True, depot_model='reactive', order_flow=flow)
    ref.new_game(seed=seed, warmup_rounds=3)
    return ref


def order(ref, agent, side, qty, price, comm='FRAG', st=None, tag='t'):
    """Place an order that must rest on the book (every order in these tests
    is a quote waiting for the station's traders, never a fill against the depot)."""
    st = st or ref.get_vessel_location(agent)['station_id']
    res = ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': {
        'order_id': f'{agent}-{tag}-{ref.current_round}-{ref.current_seq}', 'agent_id': agent, 'side': side,
        'qty': qty, 'limit_price': price, 'instrument': comm, 'station_id': st, 'seq_seen': ref.current_seq}})
    assert res.get('payload', {}).get('order_status') == 'resting', res
    return res


def depot(ref, st, comm):
    q = ref.get_depot_summary()['stations'][st][comm]
    return q['best_bid'], q['best_ask']


def flow_txns(ref):
    return {r[0] for r in ref.conn.execute("SELECT DISTINCT txn_id FROM ledger_entries WHERE txn_id LIKE 'flow-%'")}


# Fixed sizes: no jitter, so a test knows exactly how much flow arrives.
# FLOW_MAIN_SCALE=1.0: these test the fill mechanics on both sides; the live
# main side is off (#180, TestMainSideOff).
FIXED = mock.patch.multiple(OF, FLOW_SCALE=1.0, FLOW_MAIN_SCALE=1.0, FLOW_JITTER=(1.0, 1.0))


class TestOnOff(unittest.TestCase):
    def test_off_in_a_bare_referee(self):
        ref = AgoraReferee(depots=True)
        self.assertFalse(ref.order_flow.enabled)
        ref.new_game(seed=1)
        self.assertFalse(ref.order_flow.enabled)

    def test_on_in_the_live_factory_and_env_turns_it_off(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGORA_')}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(build_referee_from_env(':memory:').order_flow.enabled)
        with mock.patch.dict(os.environ, dict(env, AGORA_ORDER_FLOW='0'), clear=True):
            self.assertFalse(build_referee_from_env(':memory:').order_flow.enabled)

    def test_new_game_flag(self):
        ref = game(flow=False)
        ref.new_game(seed=2, order_flow=True)
        self.assertTrue(ref.order_flow.enabled)

    def test_off_means_no_fills(self):
        ref = game(flow=False)
        _, ask = depot(ref, 'earth', 'FRAG')
        order(ref, 'zero', 'ask', 50, ask)
        ref.step_round()
        self.assertEqual(flow_txns(ref), set())


@FIXED
class TestSizing(unittest.TestCase):
    def test_sized_from_the_depot_drips(self):
        ref = game()
        # Earth is FRAG's cheapest station: producers sell there, few buy.
        self.assertEqual(ref.order_flow.expected('earth', 'FRAG'),
                         {'buy': REACTIVE_SIDE_DRIP, 'sell': REACTIVE_MAIN_DRIP})
        # Ceres is FRAG's dearest: consumers buy there.
        self.assertEqual(ref.order_flow.expected('ceres', 'FRAG'),
                         {'buy': REACTIVE_MAIN_DRIP, 'sell': REACTIVE_SIDE_DRIP})
        # Mars is neither: two-sided flow.
        self.assertEqual(ref.order_flow.expected('mars', 'FRAG'),
                         {'buy': REACTIVE_SIDE_DRIP, 'sell': REACTIVE_SIDE_DRIP})

    def test_status_is_readable(self):
        st = game().order_flow.status()
        self.assertTrue(st['enabled'])
        self.assertEqual(set(st['expected']), {'earth', 'luna', 'mars', 'ceres'})


@FIXED
class TestFills(unittest.TestCase):
    def test_npc_buyers_take_a_fleet_ask_at_the_depot_ask_before_the_depot(self):
        ref = game()
        _, ask = depot(ref, 'earth', 'FRAG')
        cr0, frag0 = ref.get_balance('zero', 'CR'), ref.get_balance('zero', 'FRAG')
        order(ref, 'zero', 'ask', 50, ask)
        rep = ref.step_round()
        sold = REACTIVE_SIDE_DRIP  # Earth FRAG buyers a round
        self.assertEqual(ref.get_balance('zero', 'FRAG'), frag0 - sold)
        self.assertEqual(ref.get_balance('zero', 'CR'), cr0 + sold * ask)
        self.assertEqual(rep['order_flow']['earth']['FRAG']['npc_bought'], sold)
        # The NPCs traded with the fleet, not the depot.
        self.assertFalse(ref.conn.execute(
            "SELECT 1 FROM ledger_entries WHERE txn_id LIKE 'flow-%' AND agent_id LIKE 'depot_%'").fetchone())
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_npc_sellers_hit_a_fleet_bid_inside_the_spread(self):
        ref = game()
        bid, ask = depot(ref, 'mars', 'FOOD')
        px = bid + 1 if ask - bid >= 2 else bid
        cr0 = ref.get_balance('marvin', 'CR')
        self.assertNotEqual(order(ref, 'marvin', 'bid', 100, px, 'FOOD')['kind'], 'reject')
        ref.step_round()
        got = ref.get_balance('marvin', 'FOOD')
        self.assertEqual(got, REACTIVE_SIDE_DRIP)
        self.assertEqual(ref.get_balance('marvin', 'CR'), cr0 - got * px)
        self.assertTrue(ref.verify_ledger_invariants()[0])

    def test_quotes_outside_the_depot_touch_are_not_filled(self):
        ref = game()
        bid, ask = depot(ref, 'earth', 'FRAG')
        order(ref, 'zero', 'ask', 50, ask + 1, tag='a')
        order(ref, 'zero', 'bid', 50, max(1, bid - 1), tag='b')
        ref.step_round()
        self.assertEqual(flow_txns(ref), set())

    def test_price_then_time_priority_among_fleets(self):
        ref = game()
        # Two fleets docked at Ceres (FRAG's dearest station: 100 buyers a round).
        ref.conn.execute("UPDATE vessel_locations SET station_id = 'ceres' WHERE agent_id = 'zero'")
        _, ask = depot(ref, 'ceres', 'FRAG')
        order(ref, 'amos', 'ask', 60, ask, tag='late-worse')       # joins the depot
        order(ref, 'zero', 'ask', 60, ask - 1, tag='better')        # better price
        order(ref, 'marvin', 'ask', 1, ask, st='mars', tag='x')     # another station entirely
        a0, z0 = ref.get_balance('amos', 'FRAG'), ref.get_balance('zero', 'FRAG')
        ref.step_round()
        self.assertEqual(z0 - ref.get_balance('zero', 'FRAG'), 60)
        self.assertEqual(a0 - ref.get_balance('amos', 'FRAG'), REACTIVE_MAIN_DRIP - 60)

    def test_same_price_first_come_first_filled(self):
        ref = game()
        ref.conn.execute("UPDATE vessel_locations SET station_id = 'ceres' WHERE agent_id = 'zero'")
        _, ask = depot(ref, 'ceres', 'FRAG')
        order(ref, 'zero', 'ask', 80, ask, tag='first')
        order(ref, 'amos', 'ask', 80, ask, tag='second')
        z0, a0 = ref.get_balance('zero', 'FRAG'), ref.get_balance('amos', 'FRAG')
        ref.step_round()
        self.assertEqual(z0 - ref.get_balance('zero', 'FRAG'), 80)
        self.assertEqual(a0 - ref.get_balance('amos', 'FRAG'), REACTIVE_MAIN_DRIP - 80)

    def test_a_fill_never_exceeds_what_the_fleet_holds(self):
        ref = game()
        _, ask = depot(ref, 'earth', 'FRAG')
        order(ref, 'zero', 'ask', 50, ask)
        # The FRAG leaves its account some other way (a balanced move to SYSTEM).
        left = 7
        gone = ref.get_balance('zero', 'FRAG') - left
        with ref.conn:
            for acct, d in (('zero', -gone), ('SYSTEM', gone)):
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'FRAG'", (d, acct))
                ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('t-drain', 0, ?, 'FRAG', ?)", (acct, d))
        ref.step_round()
        self.assertEqual(ref.get_balance('zero', 'FRAG'), 0)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_a_fleet_that_left_the_station_is_not_filled(self):
        ref = game()
        _, ask = depot(ref, 'earth', 'FRAG')
        order(ref, 'zero', 'ask', 50, ask)
        res = ref.initiate_transit(agent_id='zero', destination='luna', commodity='FRAG', cargo_qty=0)
        self.assertEqual(res.get('status'), 'in_transit')
        ref.step_round()
        self.assertFalse([t for t in flow_txns(ref) if t.startswith('flow-earth-')])

    def test_halted_books_and_out_fleets_are_skipped(self):
        ref = game()
        _, ask = depot(ref, 'earth', 'FRAG')
        order(ref, 'zero', 'ask', 50, ask, tag='frag')
        ref.trigger_circuit_breaker_halt('earth', 'FRAG', ask, reason='test')
        ref.step_round()
        self.assertFalse([t for t in flow_txns(ref) if t.startswith('flow-earth-frag')])

        ref = game()
        _, ask = depot(ref, 'earth', 'FRAG')
        order(ref, 'zero', 'ask', 50, ask)
        with mock.patch.object(ref, 'fleet_out', side_effect=lambda a: 'bankrupt' if a == 'zero' else None):
            ref.step_round()
        self.assertEqual(flow_txns(ref), set())

    def test_every_fill_is_a_balanced_txn_with_system(self):
        ref = game()
        for a in ('zero', 'amos', 'marvin', 'aerial'):
            st = ref.get_vessel_location(a)['station_id']
            for comm in ('FRAG', 'FUEL'):
                bid, ask = depot(ref, st, comm)
                order(ref, a, 'ask', 30, ask, comm, tag=f'a{comm}')
                order(ref, a, 'bid', 30, bid, comm, tag=f'b{comm}')
        ref.step_round()
        self.assertTrue(flow_txns(ref))
        for (txn,) in ref.conn.execute("SELECT DISTINCT txn_id FROM ledger_entries WHERE txn_id LIKE 'flow-%'"):
            parties = {r[0] for r in ref.conn.execute("SELECT agent_id FROM ledger_entries WHERE txn_id = ?", (txn,))}
            self.assertIn('SYSTEM', parties)
            self.assertEqual(len(parties), 2)
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)


class TestUnfundedRestingOrders(unittest.TestCase):
    """A resting order whose funding left the account since it was placed is
    cancelled before anything matches it (found by the #162 sim: a maker
    sold into a novice's stale bid after a debt payment took its CR)."""

    def drain(self, ref, agent, inst, keep):
        gone = ref.get_balance(agent, inst) - keep
        with ref.conn:
            for acct, d in ((agent, -gone), ('SYSTEM', gone)):
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
                ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES ('t-drain', 0, ?, ?, ?)",
                                 (acct, inst, d))

    def hit(self, ref, price):
        return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': {
            'order_id': 'marvin-hit', 'agent_id': 'marvin', 'side': 'ask', 'qty': 50, 'limit_price': price,
            'instrument': 'FRAG', 'station_id': 'mars', 'seq_seen': ref.current_seq}})

    def test_a_stale_bid_is_cancelled_not_filled_into_a_negative_balance(self):
        ref = game(flow=False)
        ref.conn.execute("UPDATE vessel_locations SET station_id = 'mars' WHERE agent_id = 'zero'")
        bid, ask = depot(ref, 'mars', 'FRAG')
        self.assertGreaterEqual(ask - bid, 2)
        frag0 = ref.get_balance('zero', 'FRAG')
        order(ref, 'zero', 'bid', 100, bid + 1, tag='stale')   # funded when placed
        self.drain(ref, 'zero', 'CR', 5)                        # then the CR goes
        self.assertNotEqual(self.hit(ref, bid + 1)['kind'], 'reject')
        self.assertEqual(ref.get_balance('zero', 'CR'), 5)
        self.assertEqual(ref.get_balance('zero', 'FRAG'), frag0)
        status = ref.conn.execute("SELECT status FROM orders WHERE order_id LIKE 'zero-stale-%'").fetchone()[0]
        self.assertEqual(status, 'cancelled')
        ok, errs = ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_a_funded_bid_still_fills(self):
        ref = game(flow=False)
        ref.conn.execute("UPDATE vessel_locations SET station_id = 'mars' WHERE agent_id = 'zero'")
        bid, _ = depot(ref, 'mars', 'FRAG')
        frag0 = ref.get_balance('zero', 'FRAG')
        order(ref, 'zero', 'bid', 100, bid + 1, tag='ok')
        self.hit(ref, bid + 1)
        self.assertEqual(ref.get_balance('zero', 'FRAG'), frag0 + 50)


class TestDeterminism(unittest.TestCase):
    def _fills(self, seed):
        ref = game(seed=seed)
        out = []
        for _ in range(5):
            bid, ask = depot(ref, 'mars', 'FRAG')
            ref.cancel_all('marvin')
            order(ref, 'marvin', 'ask', 200, ask, tag='a')
            order(ref, 'marvin', 'bid', 200, bid, tag='b')
            rep = ref.step_round()['order_flow']
            out.append(rep.get('mars', {}).get('FRAG'))
        return out

    def test_same_seed_same_flow(self):
        self.assertEqual(self._fills(3), self._fills(3))
        self.assertNotEqual(self._fills(3), self._fills(4))


if __name__ == '__main__':
    unittest.main()


class TestMainSideOff(unittest.TestCase):
    """#180: live, there are no NPC buyers at a good's dearest station and no
    NPC sellers at its cheapest; the depot's drips are that demand and supply.
    Side-station flow, where makers quote, is unchanged."""

    @mock.patch.multiple(OF, FLOW_SCALE=1.0, FLOW_JITTER=(1.0, 1.0))
    def test_main_side_sized_zero_side_unchanged(self):
        self.assertEqual(OF.FLOW_MAIN_SCALE, 0.0)
        ref = game()
        self.assertEqual(ref.order_flow.expected('ceres', 'FRAG'), {'buy': 0, 'sell': REACTIVE_SIDE_DRIP})
        self.assertEqual(ref.order_flow.expected('earth', 'FRAG'), {'buy': REACTIVE_SIDE_DRIP, 'sell': 0})
        self.assertEqual(ref.order_flow.expected('mars', 'FRAG'),
                         {'buy': REACTIVE_SIDE_DRIP, 'sell': REACTIVE_SIDE_DRIP})
        self.assertEqual(ref.order_flow.status()['flow_main_scale'], 0.0)

    @mock.patch.multiple(OF, FLOW_SCALE=1.0, FLOW_JITTER=(1.0, 1.0))
    def test_an_ask_at_the_dear_station_is_not_filled(self):
        ref = game()
        # amos starts at Ceres, FRAG's dearest station.
        self.assertEqual(ref.get_vessel_location('amos')['station_id'], 'ceres')
        _, ask = depot(ref, 'ceres', 'FRAG')
        frag0 = ref.get_balance('amos', 'FRAG')
        order(ref, 'amos', 'ask', 50, ask)
        ref.step_round()
        self.assertEqual(ref.get_balance('amos', 'FRAG'), frag0)
        self.assertEqual(flow_txns(ref), set())

    def test_briefing_says_so(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGORA_')}
        with mock.patch.dict(os.environ, env, clear=True):
            ref = build_referee_from_env(':memory:')
        ref.new_game(seed=1)
        from agora.briefing import build_briefing
        self.assertIn("no NPC traders on those sides", build_briefing(ref))
