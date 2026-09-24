"""Earned institutional standing by income lane (#187 track 2, agora/standing.py)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from agora import standing as S
from agora import upgrades as U
from agora.referee import AgoraReferee


def post(ref, txn, legs):
    """Append a ledger txn (the standing desk only reads the ledger)."""
    seq = ref.current_seq
    with ref.lock, ref.conn:
        for agent, inst, d in legs:
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                             (txn, seq, agent, inst, d))


def book_trade(ref, trade_id, station, buyer, seller, inst, qty, price):
    """A book trade as the referee settles it: ledger txn trade-<id> plus its book event."""
    post(ref, f"trade-{trade_id}", [(buyer, 'CR', -qty * price), (seller, 'CR', qty * price),
                                    (buyer, inst, qty), (seller, inst, -qty)])
    with ref.lock, ref.conn:
        ref.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'trade', ?)",
                         (ref.current_seq + 1, json.dumps({'trade_id': trade_id, 'station_id': station,
                                                           'instrument': inst, 'qty': qty, 'price': price})))


def lanes(ref, agent):
    return {l: v['lane_profit_cr'] for l, v in ref.standing.report(agent)['corps'][agent]['lanes'].items()}


class TestRules(unittest.TestCase):
    def test_tier1_needs_both_share_and_floor(self):
        st = S.new_lane_state()
        self.assertEqual(S.advance(st, 0.49, 10 ** 6, 1)[0]['tier'], 0)
        self.assertEqual(S.advance(st, 0.9, S.T1_FLOOR - 1, 1)[0]['tier'], 0)
        s, moves = S.advance(st, 0.50, S.T1_FLOOR, 7)
        self.assertEqual((s['tier'], s['first_t1'], moves), (1, 7, [('earn', 1)]))

    def test_tier2_needs_both_share_and_floor(self):
        t1 = S.advance(S.new_lane_state(), 0.6, S.T1_FLOOR, 1)[0]
        self.assertEqual(S.advance(t1, 0.74, 10 ** 6, 2)[0]['tier'], 1)
        self.assertEqual(S.advance(t1, 0.9, S.T2_FLOOR - 1, 2)[0]['tier'], 1)
        s, moves = S.advance(t1, 0.75, S.T2_FLOOR, 9)
        self.assertEqual((s['tier'], s['first_t2'], moves), (2, 9, [('earn', 2)]))
        # From nothing to tier 2 in one round when both bars are cleared.
        s, moves = S.advance(S.new_lane_state(), 0.8, S.T2_FLOOR, 3)
        self.assertEqual((s['tier'], moves), (2, [('earn', 1), ('earn', 2)]))

    def test_tier1_lapses_only_after_25_rounds_below_35pct(self):
        s = S.advance(S.new_lane_state(), 1.0, S.T1_FLOOR, 0)[0]
        for r in range(1, S.LAPSE_ROUNDS):  # 24 rounds below: kept
            s, moves = S.advance(s, 0.34, S.T1_FLOOR, r)
            self.assertEqual((s['tier'], moves), (1, []))
        s, moves = S.advance(s, 0.40, S.T1_FLOOR, 30)  # one round back over 35% resets the count
        self.assertEqual(s['below1'], 0)
        for r in range(S.LAPSE_ROUNDS - 1):
            s, _ = S.advance(s, 0.0, S.T1_FLOOR, 31 + r)
        self.assertEqual(s['tier'], 1)
        s, moves = S.advance(s, 0.0, S.T1_FLOOR, 99)
        self.assertEqual((s['tier'], moves), (0, [('lapse', 1)]))
        self.assertEqual(s['first_t1'], 0)  # history kept

    def test_share_between_keep_and_earn_holds_tier_indefinitely(self):
        s = S.advance(S.new_lane_state(), 1.0, S.T1_FLOOR, 0)[0]
        for r in range(200):
            s, moves = S.advance(s, 0.36, S.T1_FLOOR, r + 1)
            self.assertEqual(moves, [])
        self.assertEqual(s['tier'], 1)

    def test_tier2_demotes_to_tier1_below_60pct(self):
        s = S.advance(S.new_lane_state(), 0.9, S.T2_FLOOR, 0)[0]
        moves = []
        for r in range(S.LAPSE_ROUNDS):
            s, moves = S.advance(s, 0.55, S.T2_FLOOR, r + 1)
        self.assertEqual((s['tier'], moves), (1, [('lapse', 2)]))
        s, moves = S.advance(s, 0.80, S.T2_FLOOR, 50)  # earned back
        self.assertEqual((s['tier'], moves), (2, [('earn', 2)]))

    def test_losing_tier1_from_tier2_drops_both(self):
        s = S.advance(S.new_lane_state(), 0.9, S.T2_FLOOR, 0)[0]
        for r in range(S.LAPSE_ROUNDS):
            s, moves = S.advance(s, 0.0, S.T2_FLOOR, r + 1)
        self.assertEqual(s['tier'], 0)
        self.assertEqual(moves, [('lapse', 2), ('lapse', 1)])

    def test_shares_ignore_losing_lanes(self):
        sh = S.shares({'hauling': 60_000, 'trading': 20_000, 'covert': -50_000})
        self.assertAlmostEqual(sh['hauling'], 0.75)
        self.assertAlmostEqual(sh['trading'], 0.25)
        self.assertEqual(sh['covert'], 0.0)
        self.assertEqual(S.shares({'hauling': -5}), {l: 0.0 for l in S.LANES})


class TestAttribution(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(standing=True)
        self.ref.standing.step_locked(0)  # past the genesis entries

    def step(self):
        with self.ref.lock, self.ref.conn:
            self.ref.standing.step_locked(self.ref.current_round + 1)
        self.ref.current_round += 1

    def test_goods_moved_between_stations_is_hauling(self):
        book_trade(self.ref, 'trd-1', 'ceres', 'zero', 'depot_ceres', 'ORE', 100, 10)
        post(self.ref, 'escrow-tx-1', [('zero', 'ORE', -100), ('SYSTEM', 'ORE', 100)])
        post(self.ref, 'fuel-tx-1', [('zero', 'FUEL', -5), ('SYSTEM', 'FUEL', 5)])  # genesis fuel: no cost
        post(self.ref, 'toll-tx-1', [('zero', 'CR', -50), ('SYSTEM', 'CR', 50)])
        self.step()
        post(self.ref, 'release-tx-1', [('zero', 'ORE', 90), ('SYSTEM', 'ORE', -90)])  # 10 lost in flight
        book_trade(self.ref, 'trd-2', 'earth', 'depot_earth', 'zero', 'ORE', 90, 25)
        self.step()
        # 90 x 25 - 90 x 10 (sold) - 10 x 10 (lost) - 50 toll
        self.assertEqual(lanes(self.ref, 'zero')['hauling'], 2250 - 900 - 100 - 50)
        self.assertEqual(lanes(self.ref, 'zero')['market_making'], 0)

    def test_bought_and_sold_at_one_station_is_market_making(self):
        post(self.ref, 'flow-luna-ore-r1-1', [('amos', 'ORE', 50), ('amos', 'CR', -500),
                                              ('SYSTEM', 'ORE', -50), ('SYSTEM', 'CR', 500)])
        post(self.ref, 'flow-luna-ore-r1-2', [('amos', 'ORE', -50), ('amos', 'CR', 600),
                                              ('SYSTEM', 'ORE', 50), ('SYSTEM', 'CR', -600)])
        self.step()
        self.assertEqual(lanes(self.ref, 'amos')['market_making'], 100)
        self.assertEqual(lanes(self.ref, 'amos')['hauling'], 0)

    def test_hauled_cargo_sold_to_order_flow_is_still_hauling(self):
        # #180: haulers rest their cargo for the station's NPC buyers.
        book_trade(self.ref, 'trd-3', 'ceres', 'zero', 'depot_ceres', 'FOOD', 10, 10)
        post(self.ref, 'flow-luna-food-r2-1', [('zero', 'FOOD', -10), ('zero', 'CR', 200),
                                               ('SYSTEM', 'FOOD', 10), ('SYSTEM', 'CR', -200)])
        self.step()
        self.assertEqual(lanes(self.ref, 'zero')['hauling'], 100)

    def test_genesis_goods_have_no_lane(self):
        book_trade(self.ref, 'trd-4', 'earth', 'depot_earth', 'amos', 'FRAG', 100, 12)
        self.step()
        self.assertEqual(set(lanes(self.ref, 'amos').values()), {0})

    def test_stock_round_trip_is_trading_and_own_stock_is_not(self):
        book_trade(self.ref, 'trd-5', 'ceres', 'zero', 'exchange', 'EQ_AMOS', 10, 20)
        book_trade(self.ref, 'trd-6', 'ceres', 'exchange', 'zero', 'EQ_AMOS', 10, 26)
        book_trade(self.ref, 'trd-7', 'ceres', 'exchange', 'zero', 'EQ_ZERO', 10, 30)  # its own treasury stock
        post(self.ref, 'fee-loan-x-r1', [('zero', 'CR', -5), ('amos', 'CR', 5)])
        self.step()
        self.assertEqual(lanes(self.ref, 'zero')['trading'], 60 - 5)

    def test_short_sale_then_cover_is_trading(self):
        post(self.ref, 'share-loan-z', [('marvin', 'EQ_AMOS', -20), ('zero', 'EQ_AMOS', 20)])
        post(self.ref, 'col-loan-z', [('zero', 'CR', -1000), ('SYSTEM', 'CR', 1000)])
        book_trade(self.ref, 'trd-8', 'ceres', 'exchange', 'zero', 'EQ_AMOS', 20, 30)  # sold short at 30
        self.step()
        book_trade(self.ref, 'trd-9', 'ceres', 'zero', 'exchange', 'EQ_AMOS', 20, 22)  # covered at 22
        post(self.ref, 'ret-share-loan-z', [('zero', 'EQ_AMOS', -20), ('marvin', 'EQ_AMOS', 20)])
        post(self.ref, 'ret-col-loan-z', [('SYSTEM', 'CR', -1000), ('zero', 'CR', 1000)])
        self.step()
        self.assertEqual(lanes(self.ref, 'zero')['trading'], 20 * (30 - 22))
        self.assertEqual(lanes(self.ref, 'marvin')['trading'], 0)

    def test_privateer_loot_and_ransom_are_covert(self):
        post(self.ref, 'piracy-hire-c1', [('aerial', 'CR', -1500), ('SYSTEM', 'CR', 1500)])
        post(self.ref, 'piracy-loot-tx-9', [('SYSTEM', 'ORE', -40), ('aerial', 'ORE', 40)])
        post(self.ref, 'piracy-ransom-tx-8', [('zero', 'CR', -1000), ('SYSTEM', 'CR', 500), ('aerial', 'CR', 500)])
        book_trade(self.ref, 'trd-10', 'earth', 'depot_earth', 'aerial', 'ORE', 40, 25)
        self.step()
        self.assertEqual(lanes(self.ref, 'aerial')['covert'], -1500 + 500 + 1000)
        self.assertEqual(lanes(self.ref, 'zero')['hauling'], -1000)


class TestDesk(unittest.TestCase):
    def ref_with(self, **tiers):
        ref = AgoraReferee(standing=True, upgrades=True)
        with ref.lock, ref.conn:
            for cap, t in tiers.items():
                ref.conn.execute("INSERT OR REPLACE INTO standing_lanes (agent_id, lane, tier) VALUES ('zero', ?, ?)",
                                 (S.CAP_LANE[cap], t))
        return ref

    def test_allows_and_tiers(self):
        ref = self.ref_with(freight_guild=1)
        self.assertEqual(ref.standing.tiers('zero'), {'freight_guild': 1, 'exchange_seat': 0, 'market_house': 0,
                                                      'belt_syndicate': 0})
        self.assertTrue(ref.standing.allows('zero', 'freight_guild'))
        self.assertFalse(ref.standing.allows('zero', 'freight_guild:2'))
        self.assertTrue(ref.standing.allows('zero', 'ship_4'))
        self.assertFalse(ref.standing.allows('zero', 'ship_5'))
        self.assertFalse(ref.standing.allows('zero', 'heavy_hull'))
        self.assertFalse(ref.standing.allows('amos', 'freight_guild'))
        self.assertFalse(ref.standing.allows('zero', 'exchange_seat'))
        self.assertEqual(ref.standing.titles('zero'), ['Guild Member'])
        with self.assertRaises(ValueError):
            ref.standing.allows('zero', 'freight_gild')
        with self.assertRaises(ValueError):
            ref.standing.allows('zero', 'freight_guild:3')

    def test_off_allows_everything(self):
        ref = AgoraReferee(standing=False)
        self.assertTrue(ref.standing.allows('zero', 'ship_5'))
        self.assertIsNone(ref.standing.step_locked(1))

    def test_tech_bought_with_standing_is_kept_after_it_lapses(self):
        ref = self.ref_with(freight_guild=1)
        ref.new_game(seed=3, warmup_rounds=1, standing=True, upgrades=True)
        with ref.lock, ref.conn:
            ref.conn.execute("INSERT INTO standing_lanes (agent_id, lane, tier) VALUES ('zero', 'hauling', 1)")
            ref.conn.execute("UPDATE accounts SET balance = balance + 100000 WHERE agent_id = 'zero' AND instrument = 'CR'")
        gated = dict(U.CATALOG['armor'], standing=['freight_guild', 'freight_guild:2', None])
        with mock.patch.dict(U.CATALOG, {'armor': gated}):
            self.assertEqual(ref.upgrades.buy('amos', 'armor')['payload']['reason'], 'standing_required')
            self.assertEqual(ref.upgrades.buy('zero', 'armor')['kind'], 'upgrade_ok')
            with ref.lock, ref.conn:  # standing lapses
                ref.conn.execute("UPDATE standing_lanes SET tier = 0 WHERE agent_id = 'zero'")
            self.assertFalse(ref.standing.allows('zero', 'freight_guild'))
            self.assertEqual(ref.upgrades.tier('zero', 'armor'), 1)
            self.assertEqual(ref.upgrades.factor('zero', 'armor'), U.CATALOG['armor']['factors'][0])
            ref.current_round = 100  # tier 2 on sale, but the catalog is closed to zero now
            self.assertEqual(ref.upgrades.buy('zero', 'armor')['payload']['reason'], 'standing_required')
            self.assertEqual(ref.upgrades.catalog()[2]['tier_detail'][0]['standing'], 'freight_guild')

    def test_step_posts_galnet_on_admission(self):
        ref = AgoraReferee(standing=True)
        ref.standing.step_locked(0)
        post(ref, 'contract-deliver-c1-1-0', [('zero', 'CR', 50_000), ('SYSTEM', 'CR', -50_000)])
        rep = ref.step_round()
        self.assertEqual(rep['standing']['changes'], [{'agent_id': 'zero', 'lane': 'hauling', 'institution':
                                                       'freight_guild', 'move': 'earn', 'tier': 1}])
        heads = [json.loads(p)['headline'] for (p,) in ref.conn.execute(
            "SELECT payload FROM book_events WHERE kind = 'news' AND payload LIKE '%gn-standing-%'")]
        self.assertEqual(heads, ['GUILD ADMITS ZERO'])
        self.assertTrue(ref.standing.allows('zero', 'freight_guild'))

    def test_window_drops_old_income(self):
        ref = AgoraReferee(standing=True)
        ref.standing.step_locked(0)
        post(ref, 'contract-deliver-c1-1-0', [('zero', 'CR', 50_000), ('SYSTEM', 'CR', -50_000)])
        ref.step_round()
        post(ref, 'fee-l-r1', [('zero', 'CR', 10), ('amos', 'CR', -10)])
        for _ in range(S.WINDOW):
            ref.step_round()
        z = ref.standing.report('zero')['corps']['zero']['lanes']
        self.assertEqual(z['hauling']['trailing_cr'], 0)
        self.assertEqual(z['hauling']['lane_profit_cr'], 50_000)  # the floor is cumulative
        self.assertEqual(z['trading']['share'], 1.0)

    def test_survives_restart_without_double_counting(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            ref = AgoraReferee(db_path=path, standing=True)
            ref.standing.step_locked(0)
            post(ref, 'contract-deliver-c1-1-0', [('zero', 'CR', 50_000), ('SYSTEM', 'CR', -50_000)])
            book_trade(ref, 'trd-1', 'ceres', 'zero', 'depot_ceres', 'ORE', 10, 10)
            ref.step_round()
            ref.conn.close()
            ref2 = AgoraReferee(db_path=path, standing=True)  # current_round restarts at 0
            self.assertTrue(ref2.standing.allows('zero', 'freight_guild'))
            self.assertEqual(ref2.standing.book['lots']['zero']['ORE'], [['ceres', 'bought', 10, 100.0]])
            ref2.step_round()
            self.assertEqual(lanes(ref2, 'zero')['hauling'], 50_000)
            z = ref2.standing.report('zero')['corps']['zero']['lanes']['hauling']
            self.assertEqual(z['trailing_cr'], 50_000)  # the window survived, and was not counted twice
            self.assertEqual(ref2.standing.tick, 3)  # the desk's own counter carried over the restart
            ref2.conn.close()
        finally:
            os.unlink(path)

    def test_reset_wipes_standing(self):
        ref = AgoraReferee(standing=True)
        ref.standing.step_locked(0)
        post(ref, 'contract-deliver-c1-1-0', [('zero', 'CR', 50_000), ('SYSTEM', 'CR', -50_000)])
        ref.step_round()
        self.assertTrue(ref.standing.allows('zero', 'freight_guild'))
        ref.reset_to_genesis()
        self.assertFalse(ref.standing.allows('zero', 'freight_guild'))
        self.assertEqual(ref.conn.execute("SELECT COUNT(*) FROM standing_income").fetchone()[0], 0)
        ref.step_round()  # genesis re-read as neutral, not income
        self.assertEqual(set(lanes(ref, 'zero').values()), {0})

    def test_briefing_and_endpoint_shape(self):
        from agora.briefing import build_briefing
        ref = AgoraReferee(standing=True)
        ref.step_round()
        text = build_briefing(ref)
        self.assertIn('## Institutional standing', text)
        self.assertIn('Sol Freight Guild', text)
        self.assertIn('lapses after 25 rounds below 35%', text)
        rep = ref.standing.report()
        self.assertEqual(set(rep['corps']), {'aerial', 'amos', 'marvin', 'zero'})
        self.assertEqual(rep['rules']['tier2'], {'share': 0.75, 'lane_profit_cr': 120_000, 'keep_share': 0.6,
                                                 'lane_profit_cr_by_lane': {'hauling': 120_000, 'trading': 120_000,
                                                                            'market_making': 80_000, 'covert': 120_000}})


class TestStylesEarnTheirLane(unittest.TestCase):
    def test_each_style_books_to_its_own_lane(self):
        """One live-default styles game: the attribution that decides every
        perk puts each bot's income in the lane it actually plays."""
        from tools import economy_sim as sim
        holder = {}
        r = sim._run("styles", "flat", 1, 150, "strict", 50, None, None, None, None, None, None,
                     on_round=lambda ref: holder.__setitem__('ref', ref))
        rep = holder['ref'].standing.report()['corps']
        want = {'hauler': 'hauling', 'privateer': 'hauling', 'maker': 'market_making', 'stock_trader': 'trading'}
        for a, f in r['fleets'].items():
            mix = {l: v['share'] for l, v in rep[a]['lanes'].items()}
            self.assertEqual(max(mix, key=mix.get), want[f['strategy']], (f['strategy'], mix))


class TestMarketMakingTier2Floor(unittest.TestCase):
    def test_market_making_tier2_floor_is_80k(self):
        self.assertEqual(S.t2_floor('market_making'), 80_000)
        self.assertEqual(S.t2_floor('hauling'), S.T2_FLOOR)
        t1 = S.advance(S.new_lane_state(), 0.8, S.T1_FLOOR, 1)[0]
        self.assertEqual(S.advance(t1, 0.8, 80_000, 2, S.t2_floor('market_making'))[0]['tier'], 2)
        self.assertEqual(S.advance(t1, 0.8, 80_000, 2, S.t2_floor('hauling'))[0]['tier'], 1)


if __name__ == '__main__':
    unittest.main()
