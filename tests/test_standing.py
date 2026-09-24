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
    def test_thresholds(self):
        self.assertEqual((S.T1_FLOOR, S.T2_FLOOR), (40_000, 120_000))
        self.assertEqual({l: S.threshold(l, 2) for l in S.LANES},
                         {'hauling': 120_000, 'trading': 120_000, 'market_making': 80_000, 'covert': 120_000})
        self.assertEqual({S.threshold(l, 1) for l in S.LANES}, {40_000})

    def test_tier1_at_40k(self):
        st = S.new_lane_state()
        self.assertEqual(S.advance(st, S.T1_FLOOR - 1, 1, 'hauling')[0]['tier'], 0)
        s, moves = S.advance(st, S.T1_FLOOR, 7, 'hauling')
        self.assertEqual((s['tier'], s['first_t1'], moves), (1, 7, [('earn', 1)]))

    def test_tier2_at_120k_and_both_in_one_round(self):
        t1 = S.advance(S.new_lane_state(), S.T1_FLOOR, 1, 'hauling')[0]
        self.assertEqual(S.advance(t1, S.T2_FLOOR - 1, 2, 'hauling')[0]['tier'], 1)
        s, moves = S.advance(t1, S.T2_FLOOR, 9, 'hauling')
        self.assertEqual((s['tier'], s['first_t2'], moves), (2, 9, [('earn', 2)]))
        s, moves = S.advance(S.new_lane_state(), S.T2_FLOOR, 3, 'trading')
        self.assertEqual((s['tier'], moves), (2, [('earn', 1), ('earn', 2)]))

    def test_earned_standing_is_permanent(self):
        s = S.advance(S.new_lane_state(), S.T2_FLOOR, 0, 'hauling')[0]
        for r in range(1, 300):  # the lane bleeds money for the rest of the game
            s, moves = S.advance(s, -500_000, r, 'hauling')
            self.assertEqual((s['tier'], moves), (2, []))
        s = S.advance(S.new_lane_state(), S.T1_FLOOR, 0, 'covert')[0]
        s, moves = S.advance(s, 0, 1, 'covert')
        self.assertEqual((s['tier'], moves), (1, []))

    def test_halfway_news_once_per_tier(self):
        s, moves = S.advance(S.new_lane_state(), 19_999, 1, 'hauling')
        self.assertEqual(moves, [])
        s, moves = S.advance(s, 20_000, 2, 'hauling')
        self.assertEqual(moves, [('halfway', 1)])
        s, moves = S.advance(s, 10_000, 3, 'hauling')  # dips back under half
        s, moves = S.advance(s, 25_000, 4, 'hauling')  # and over again: no second story
        self.assertEqual(moves, [])
        s, moves = S.advance(s, 40_000, 5, 'hauling')
        self.assertEqual(moves, [('earn', 1)])
        s, moves = S.advance(s, 60_000, 6, 'hauling')  # half of 120k
        self.assertEqual(moves, [('halfway', 2)])

    def test_halfway_skipped_when_it_lands_with_an_admission(self):
        s, moves = S.advance(S.new_lane_state(), 50_000, 1, 'hauling')  # past 20k and 40k at once
        self.assertEqual((moves, s['half1']), ([('earn', 1)], True))
        s, moves = S.advance(S.new_lane_state(), 40_000, 1, 'market_making')  # 40k is also half of 80k
        self.assertEqual((moves, s['half2']), ([('earn', 1)], True))
        self.assertEqual(S.advance(s, 60_000, 2, 'market_making')[1], [])

    def test_progress(self):
        self.assertEqual(S.progress('hauling', 0, 31_200), {'next_tier': 1, 'next_title': 'Guild Member',
                                                            'next_threshold_cr': 40_000, 'progress_pct': 78})
        self.assertEqual(S.progress('hauling', 0, 39_999)['progress_pct'], 99)
        self.assertEqual(S.progress('hauling', 0, -5_000)['progress_pct'], 0)
        self.assertEqual(S.progress('hauling', 1, 60_000)['progress_pct'], 50)
        self.assertEqual(S.progress('market_making', 1, 60_000)['progress_pct'], 75)
        self.assertEqual(S.progress('hauling', 1, 30_000)['next_title'], 'Guild Master')  # tier kept after losses
        self.assertEqual(S.progress('trading', 2, 1)['next_tier'], None)

    def test_news(self):
        self.assertEqual(S.news('zero', 'hauling', 'earn', 1)[0], 'GUILD ADMITS ZERO')
        self.assertEqual(S.news('zero', 'hauling', 'earn', 2)[0], 'ZERO NAMED GUILD MASTER')
        head, body = S.news('zero', 'hauling', 'halfway', 1, 20_400)
        self.assertEqual(head, 'ZERO HALFWAY TO GUILD MEMBER')
        self.assertIn('20,400 of 40,000 CR', body)


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

    def test_sabotage_loot_is_covert(self):
        # #186: what a sabotage takes reaches the saboteur on a sabotage-loot- txn.
        post(self.ref, 'sabotage-fee-marvin-zero-r1', [('marvin', 'CR', -1000), ('SYSTEM', 'CR', 1000)])
        post(self.ref, 'sabotage-loot-marvin-zero-r1', [('SYSTEM', 'ORE', -30), ('marvin', 'ORE', 30)])
        book_trade(self.ref, 'trd-11', 'earth', 'depot_earth', 'marvin', 'ORE', 30, 25)
        self.step()
        self.assertEqual(lanes(self.ref, 'marvin')['covert'], -1000 + 750)
        self.assertEqual(lanes(self.ref, 'marvin')['hauling'], 0)


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

    def test_standing_is_checked_at_purchase_time_only(self):
        ref = self.ref_with(freight_guild=1)
        ref.new_game(seed=3, warmup_rounds=1, standing=True, upgrades=True)
        with ref.lock, ref.conn:
            ref.conn.execute("INSERT INTO standing_lanes (agent_id, lane, tier) VALUES ('zero', 'hauling', 1)")
            ref.conn.execute("UPDATE accounts SET balance = balance + 100000 WHERE agent_id = 'zero' AND instrument = 'CR'")
        gated = dict(U.CATALOG['armor'], standing=['freight_guild', 'freight_guild:2', None])
        with mock.patch.dict(U.CATALOG, {'armor': gated}):
            self.assertEqual(ref.upgrades.buy('amos', 'armor')['payload']['reason'], 'standing_required')
            self.assertEqual(ref.upgrades.buy('zero', 'armor')['kind'], 'upgrade_ok')
            with ref.lock, ref.conn:  # standing never lapses now; force it off to prove factor() ignores it
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

    def test_losses_reduce_the_counter_but_keep_the_tier(self):
        ref = AgoraReferee(standing=True)
        ref.standing.step_locked(0)
        post(ref, 'contract-deliver-c1-1-0', [('zero', 'CR', 50_000), ('SYSTEM', 'CR', -50_000)])
        ref.step_round()
        post(ref, 'toll-tx-9', [('zero', 'CR', -30_000), ('SYSTEM', 'CR', 30_000)])
        for _ in range(60):
            ref.step_round()
        z = ref.standing.report('zero')['corps']['zero']['lanes']['hauling']
        self.assertEqual((z['lane_profit_cr'], z['tier']), (20_000, 1))
        self.assertEqual((z['next_tier'], z['next_threshold_cr'], z['progress_pct']), (2, 120_000, 16))
        self.assertTrue(ref.standing.allows('zero', 'freight_guild'))

    def test_step_posts_galnet_halfway(self):
        ref = AgoraReferee(standing=True)
        ref.standing.step_locked(0)
        post(ref, 'contract-deliver-c1-1-0', [('zero', 'CR', 21_000), ('SYSTEM', 'CR', -21_000)])
        rep = ref.step_round()
        self.assertEqual(rep['standing']['changes'], [{'agent_id': 'zero', 'lane': 'hauling', 'institution':
                                                       'freight_guild', 'move': 'halfway', 'tier': 1}])
        self.assertIsNone(ref.step_round()['standing'])  # once
        heads = [json.loads(p)['headline'] for (p,) in ref.conn.execute(
            "SELECT payload FROM book_events WHERE kind = 'news' AND payload LIKE '%gn-standing-%'")]
        self.assertEqual(heads, ['ZERO HALFWAY TO GUILD MEMBER'])

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
        self.assertEqual(ref.conn.execute("SELECT COUNT(*) FROM standing_lanes WHERE tier > 0").fetchone()[0], 0)
        ref.step_round()  # genesis re-read as neutral, not income
        self.assertEqual(set(lanes(ref, 'zero').values()), {0})

    def test_briefing_and_endpoint_shape(self):
        from agora.briefing import build_briefing
        ref = AgoraReferee(standing=True)
        ref.standing.step_locked(0)
        post(ref, 'contract-deliver-c1-1-0', [('zero', 'CR', 31_200), ('SYSTEM', 'CR', -31_200)])
        ref.step_round()
        text = build_briefing(ref, viewer='zero')
        self.assertIn('## Institutional standing', text)
        self.assertIn('reach 40,000 CR for tier 1 and 120,000 CR for tier 2 (80,000 for market making)', text)
        self.assertNotIn('lapse', text.split('## Institutional standing')[1].split('##')[0])
        self.assertIn('- Sol Freight Guild (freight): 31,200 / 40,000 CR (78%) - next: Guild Member '
                      '(opens 4th ship berth, Guild refit yards)', text)
        sec = text.split('## Institutional standing')[1]
        self.assertLess(sec.index('**zero** (you)'), sec.index('**aerial**'))  # the viewer's own corp first
        self.assertIn('**aerial**', build_briefing(ref))
        rep = ref.standing.report()
        self.assertEqual(set(rep['corps']), {'aerial', 'amos', 'marvin', 'zero'})
        self.assertEqual(rep['rules']['tier2']['lane_profit_cr_by_lane'],
                         {'hauling': 120_000, 'trading': 120_000, 'market_making': 80_000, 'covert': 120_000})
        self.assertTrue(rep['rules']['permanent'])
        z = rep['corps']['zero']['lanes']['hauling']
        self.assertEqual({k: z[k] for k in ('lane_profit_cr', 'tier', 'next_tier', 'next_threshold_cr', 'progress_pct')},
                         {'lane_profit_cr': 31_200, 'tier': 0, 'next_tier': 1, 'next_threshold_cr': 40_000,
                          'progress_pct': 78})

    def test_old_database_migrates(self):
        """A #207-era DB: extra columns stay unused, the window table goes, a
        lapsed tier comes back, and no halfway burst on the next round."""
        import sqlite3
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            ref = AgoraReferee(db_path=path, standing=True)
            ref.standing.step_locked(0)
            ref.conn.close()
            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE standing_lanes")
            conn.execute("""CREATE TABLE standing_lanes (agent_id TEXT NOT NULL, lane TEXT NOT NULL,
                cum_profit INTEGER NOT NULL DEFAULT 0, tier INTEGER NOT NULL DEFAULT 0,
                below1 INTEGER NOT NULL DEFAULT 0, below2 INTEGER NOT NULL DEFAULT 0,
                first_t1 INTEGER, first_t2 INTEGER, share REAL NOT NULL DEFAULT 0, trailing INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (agent_id, lane))""")
            conn.execute("CREATE TABLE standing_income (agent_id TEXT, tick INTEGER, lane TEXT, cr INTEGER)")
            conn.execute("INSERT INTO standing_lanes (agent_id, lane, cum_profit, tier, first_t1) "
                         "VALUES ('zero', 'hauling', 45000, 0, 12)")  # lapsed under the old rule
            conn.execute("INSERT INTO standing_lanes (agent_id, lane, cum_profit) VALUES ('amos', 'market_making', 30000)")
            conn.commit()
            conn.close()
            ref2 = AgoraReferee(db_path=path, standing=True)
            self.assertTrue(ref2.standing.allows('zero', 'freight_guild'))
            self.assertIsNone(ref2.conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'standing_income'").fetchone())
            self.assertIsNone(ref2.step_round()['standing'])  # amos is past half already: no story now
            self.assertEqual(lanes(ref2, 'amos')['market_making'], 30_000)
            ref2.conn.close()
        finally:
            os.unlink(path)


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
            mix = {l: v['lane_profit_cr'] for l, v in rep[a]['lanes'].items()}
            self.assertEqual(max(mix, key=mix.get), want[f['strategy']], (f['strategy'], mix))


class TestMarketMakingTier2Floor(unittest.TestCase):
    def test_market_making_tier2_floor_is_80k(self):
        self.assertEqual(S.t2_floor('market_making'), 80_000)
        self.assertEqual(S.t2_floor('hauling'), S.T2_FLOOR)
        t1 = S.advance(S.new_lane_state(), S.T1_FLOOR, 1, 'market_making')[0]
        self.assertEqual(S.advance(t1, 80_000, 2, 'market_making')[0]['tier'], 2)
        t1 = S.advance(S.new_lane_state(), S.T1_FLOOR, 1, 'hauling')[0]
        self.assertEqual(S.advance(t1, 80_000, 2, 'hauling')[0]['tier'], 1)


if __name__ == '__main__':
    unittest.main()
