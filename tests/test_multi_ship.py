"""
tests/test_multi_ship.py - several ships per corp (#175 PR 2, agora/fleet.py).

Goods and FUEL live on each ship's ledger account ('<corp>/<n>'), CR on the
corp. Covers buying (price, cap, the Guild standing gates for hulls 4 and 5),
per-ship goods and trading only where a ship is docked, independent trips,
same-station transfers and station holds, scrapping, upkeep, the takeover
rename/scrap rules (#164), per-ship leaderboard marks, standing lots across
a same-corp cross, the upgrade migration from a one-ship database, and the
HTTP routes.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from agora import fleet as F
from agora.corporate import TAKEOVER_SHARES
from agora.referee import AgoraReferee
from agora.server import make_handler
from agora.spatial import STATIONS


def game(**kw):
    kw.setdefault('depots', True)
    ref = AgoraReferee(**kw)
    ref.new_game(seed=4, warmup_rounds=2, **{k: v for k, v in kw.items() if k != 'db_path'})
    return ref


def give(ref, acct, inst, qty):
    """Mint qty of inst to acct from SYSTEM, balanced."""
    with ref.lock, ref.conn:
        ref.fleet._move(f"test-give-{acct}-{inst}-{ref.current_seq}", ((acct, inst, qty), ('SYSTEM', inst, -qty)))


def dock(ref, vid, st):
    with ref.lock, ref.conn:
        ref.conn.execute("UPDATE vessels SET station_id = ?, status = 'docked' WHERE vessel_id = ?", (st, vid))


def order(ref, agent, side, qty, px, inst, st, vessel=None, oid=None):
    p = {'order_id': oid or f"t-{agent}-{side}-{inst}-{ref.current_seq}", 'agent_id': agent, 'side': side,
         'qty': qty, 'limit_price': px, 'instrument': inst, 'station_id': st, 'seq_seen': ref.current_seq}
    if vessel is not None:
        p['vessel_id'] = vessel
    return ref.submit_envelope({'v': 1, 'kind': 'order', 'payload': p})


def clean(t, ref):
    ok, errs = ref.verify_ledger_invariants()
    t.assertTrue(ok, errs)


def other(st):
    return next(s for s in STATIONS if s != st)


class TestBuying(unittest.TestCase):
    def test_buy_charges_the_price_and_docks_an_empty_ship_where_the_buyer_is(self):
        ref = game()
        give(ref, 'amos', 'CR', 100_000)
        st = ref.get_vessel_location('amos')['station_id']
        cr, sys_cr = ref.get_balance('amos', 'CR'), ref.get_balance('SYSTEM', 'CR')
        r = ref.fleet.buy('amos')
        self.assertEqual(r['kind'], 'ship_bought', r)
        p = r['payload']
        self.assertEqual((p['vessel_id'], p['station_id'], p['cost'], p['status']), ('amos/2', st, F.SHIP_PRICES[2], 'docked'))
        self.assertEqual(ref.get_balance('amos', 'CR'), cr - F.SHIP_PRICES[2])
        self.assertEqual(ref.get_balance('SYSTEM', 'CR'), sys_cr + F.SHIP_PRICES[2])
        self.assertEqual(ref.get_ship_accounts('amos').get('amos/2', {}), {})
        self.assertEqual(ref.get_vessel_location('amos', 'amos/2')['station_id'], st)
        self.assertEqual(ref.fleet.buy('amos')['payload']['cost'], F.SHIP_PRICES[3])
        clean(self, ref)

    def test_buy_needs_cash_and_a_docked_ship(self):
        ref = game()
        r = ref.fleet.buy('amos')  # genesis CR is below the price
        self.assertEqual(r['payload']['reason'], 'insufficient_credits')
        give(ref, 'amos', 'CR', 100_000)
        st = ref.get_vessel_location('amos')['station_id']
        self.assertEqual(ref.initiate_transit('amos', other(st))['status'], 'in_transit')
        self.assertEqual(ref.fleet.buy('amos')['payload']['reason'], 'vessel_not_docked')
        self.assertEqual(ref.fleet.buy('zero', 'amos/1')['payload']['reason'], 'invalid_vessel')

    def test_cap_three_then_guild_standing_for_four_and_five(self):
        ref = game(standing=True)
        give(ref, 'amos', 'CR', 1_000_000)
        self.assertEqual(ref.fleet.buy('amos')['kind'], 'ship_bought')
        self.assertEqual(ref.fleet.buy('amos')['kind'], 'ship_bought')
        r = ref.fleet.buy('amos')
        self.assertEqual(r['payload']['reason'], 'standing_required')
        self.assertIn('ship_4', r['payload']['detail'])
        with ref.lock, ref.conn:
            ref.conn.execute("INSERT OR REPLACE INTO standing_lanes (agent_id, lane, cum_profit, tier) VALUES ('amos', 'hauling', 40000, 1)")
        self.assertTrue(ref.standing.allows('amos', 'ship_4'))
        self.assertEqual(ref.fleet.buy('amos')['payload']['vessel_id'], 'amos/4')
        self.assertEqual(ref.fleet.buy('amos')['payload']['reason'], 'standing_required')
        with ref.lock, ref.conn:
            ref.conn.execute("UPDATE standing_lanes SET tier = 2, cum_profit = 120000 WHERE agent_id = 'amos' AND lane = 'hauling'")
        self.assertEqual(ref.fleet.buy('amos')['payload']['vessel_id'], 'amos/5')
        self.assertEqual(ref.fleet.buy('amos')['payload']['reason'], 'fleet_full')
        self.assertEqual(ref.fleet.ship_cap('amos'), 5)
        clean(self, ref)

    def test_standing_off_opens_hulls_four_and_five(self):
        ref = game(standing=False)
        give(ref, 'amos', 'CR', 1_000_000)
        for n in (2, 3, 4, 5):
            self.assertEqual(ref.fleet.buy('amos')['payload']['vessel_id'], f'amos/{n}')
        self.assertEqual(ref.fleet.buy('amos')['payload']['reason'], 'fleet_full')


class TestShipsCarryGoods(unittest.TestCase):
    def setUp(self):
        self.ref = game()
        give(self.ref, 'amos', 'CR', 100_000)
        self.st = self.ref.get_vessel_location('amos')['station_id']
        self.assertEqual(self.ref.fleet.buy('amos')['kind'], 'ship_bought')

    def test_genesis_goods_are_on_ship_one(self):
        ref = self.ref
        self.assertEqual(ref.get_balance('amos/1', 'FRAG'), 1000)
        self.assertEqual(ref.get_balance('amos', 'FRAG'), 1000)  # the corp total
        with ref.lock:
            bare = ref.conn.execute("SELECT balance FROM accounts WHERE agent_id='amos' AND instrument='FRAG'").fetchone()
        self.assertTrue(bare is None or bare[0] == 0)

    def test_a_ship_sells_only_its_own_hold(self):
        ref = self.ref
        r = order(ref, 'amos', 'ask', 10, 1, 'FRAG', self.st, vessel='amos/2')
        self.assertEqual(r['payload']['reason'], 'insufficient_balance')
        r = order(ref, 'amos', 'ask', 10, 1, 'FRAG', self.st)  # ship 1 by default
        self.assertNotEqual(r['kind'], 'reject', r)
        self.assertEqual(ref.get_balance('amos/1', 'FRAG'), 990)
        clean(self, ref)

    def test_bought_goods_land_on_the_buying_ship(self):
        ref = self.ref
        ask = ref.get_depot_summary()['stations'][self.st]['ORE']['best_ask']
        r = order(ref, 'amos', 'bid', 20, ask, 'ORE', self.st, vessel='2')
        self.assertEqual(r['payload'].get('filled_qty'), 20, r)
        self.assertEqual(ref.get_balance('amos/2', 'ORE'), 20)
        self.assertEqual(ref.get_balance('amos/1', 'ORE'), 0)
        clean(self, ref)

    def test_a_ship_trades_only_where_it_is_docked(self):
        ref = self.ref
        dest = other(self.st)
        dock(ref, 'amos/2', dest)
        give(ref, 'amos/2', 'FRAG', 50)
        r = order(ref, 'amos', 'ask', 10, 1, 'FRAG', self.st, vessel='amos/2')
        self.assertEqual(r['payload']['reason'], 'vessel_not_docked')
        r = order(ref, 'amos', 'ask', 10, 1, 'FRAG', dest, vessel='amos/2')
        self.assertNotEqual(r['kind'], 'reject', r)
        clean(self, ref)

    def test_no_rival_ship_and_no_ship_as_agent(self):
        ref = self.ref
        zst = ref.get_vessel_location('zero')['station_id']
        self.assertEqual(order(ref, 'zero', 'ask', 1, 1, 'FRAG', zst, vessel='amos/1')['payload']['reason'], 'invalid_vessel')
        self.assertEqual(order(ref, 'amos/1', 'ask', 1, 1, 'FRAG', self.st)['payload']['reason'], 'invalid_format')
        self.assertEqual(ref.initiate_transit('zero', other(zst), vessel_id='amos/2')['payload']['reason'], 'invalid_vessel')
        self.assertEqual(ref.fleet.transfer('zero', 'amos/1', 'zero/1', 'FRAG', 1)['payload']['reason'], 'invalid_vessel')
        self.assertEqual(ref.peer.offer('zero', zst, 'FRAG', 10, 5, vessel_id='amos/1')['payload']['reason'], 'invalid_vessel')

    def test_ships_fly_independently_one_trip_each(self):
        ref = self.ref
        a, b = [s for s in STATIONS if s != self.st][:2]
        give(ref, 'amos/2', 'FUEL', 300)
        self.assertEqual(ref.initiate_transit('amos', a, vessel_id='amos/2')['status'], 'in_transit')
        self.assertEqual(ref.initiate_transit('amos', b, vessel_id='amos/2')['payload']['reason'], 'already_in_transit')
        # Ship 1 still trades and flies while ship 2 is away.
        self.assertNotEqual(order(ref, 'amos', 'ask', 5, 1, 'FRAG', self.st)['kind'], 'reject')
        self.assertEqual(ref.initiate_transit('amos', b)['status'], 'in_transit')
        flying = ref.conn.execute("SELECT COUNT(*) FROM transits WHERE agent_id='amos' AND status='in_transit'").fetchone()[0]
        self.assertEqual(flying, 2)
        for _ in range(15):
            ref.step_round()
        locs = {l['vessel_id']: l['station_id'] for l in ref.fleet_locations('amos')}
        self.assertEqual(locs, {'amos/1': b, 'amos/2': a})
        clean(self, ref)

    def test_departure_cancels_only_that_ships_orders(self):
        ref = self.ref
        give(ref, 'amos/2', 'FUEL', 300)
        give(ref, 'amos/2', 'ORE', 10)
        order(ref, 'amos', 'ask', 10, 9999, 'FRAG', self.st, oid='keep')
        order(ref, 'amos', 'ask', 10, 9999, 'ORE', self.st, vessel='amos/2', oid='go')
        ref.initiate_transit('amos', other(self.st), vessel_id='amos/2')
        resting = {o.order_id for b in ref.books[self.st].values() for o in b.asks if o.agent_id == 'amos'}
        self.assertEqual(resting, {'keep'})


class TestTransfersAndHolds(unittest.TestCase):
    def setUp(self):
        self.ref = game()
        give(self.ref, 'amos', 'CR', 100_000)
        self.st = self.ref.get_vessel_location('amos')['station_id']
        self.ref.fleet.buy('amos')

    def test_same_station_transfer(self):
        ref = self.ref
        r = ref.fleet.transfer('amos', 'amos/1', 'amos/2', 'FUEL', 100)
        self.assertEqual(r['kind'], 'transfer_ok', r)
        self.assertEqual((ref.get_balance('amos/1', 'FUEL'), ref.get_balance('amos/2', 'FUEL')), (400, 100))
        self.assertEqual(ref.fleet.transfer('amos', '1', '2', 'FRAG', 5000)['payload']['reason'], 'insufficient_balance')
        self.assertEqual(ref.fleet.transfer('amos', '1', '2', 'CR', 5)['payload']['reason'], 'invalid_instrument')
        clean(self, ref)

    def test_no_transfer_across_stations_or_in_flight(self):
        ref = self.ref
        dock(ref, 'amos/2', other(self.st))
        self.assertEqual(ref.fleet.transfer('amos', 'amos/1', 'amos/2', 'FRAG', 1)['payload']['reason'], 'not_same_station')
        dock(ref, 'amos/2', self.st)
        ref.fleet.transfer('amos', 'amos/1', 'amos/2', 'FUEL', 100)
        ref.initiate_transit('amos', other(self.st), vessel_id='amos/2')
        self.assertEqual(ref.fleet.transfer('amos', 'amos/1', 'amos/2', 'FRAG', 1)['payload']['reason'], 'vessel_in_transit')

    def test_goods_committed_to_asks_cannot_be_transferred(self):
        ref = self.ref
        order(ref, 'amos', 'ask', 990, 9999, 'FRAG', self.st)
        self.assertEqual(ref.fleet.transfer('amos', 'amos/1', 'amos/2', 'FRAG', 20)['payload']['reason'], 'insufficient_balance')
        self.assertEqual(ref.fleet.transfer('amos', 'amos/1', 'amos/2', 'FRAG', 10)['kind'], 'transfer_ok')

    def test_station_hold_loads_only_onto_a_ship_there(self):
        ref = self.ref
        give(ref, F.hold_account('amos', self.st), 'ORE', 30)
        far = other(self.st)
        give(ref, F.hold_account('amos', far), 'ORE', 7)
        self.assertEqual(ref.get_balance('amos', 'ORE'), 37)  # corp total counts holds
        self.assertEqual(ref.fleet.transfer('amos', f'@{self.st}', 'amos/2', 'ORE', 30)['kind'], 'transfer_ok')
        self.assertEqual(ref.fleet.transfer('amos', f'@{far}', 'amos/2', 'ORE', 7)['payload']['reason'], 'not_same_station')
        self.assertEqual(ref.get_balance('amos/2', 'ORE'), 30)
        clean(self, ref)

    def test_peer_refund_waits_in_the_hold_when_the_seller_ship_has_left(self):
        ref = game(peer_trades=True)
        st = ref.get_vessel_location('amos')['station_id']
        eid = ref.peer.offer('amos', st, 'FRAG', 100, 5)['payload']['escrow_id']
        ref.initiate_transit('amos', other(st))
        ref.peer.cancel('amos', eid)
        self.assertEqual(ref.get_balance(F.hold_account('amos', st), 'FRAG'), 100)
        self.assertEqual(ref.get_balance('amos/1', 'FRAG'), 900)
        clean(self, ref)


class TestScrapAndUpkeep(unittest.TestCase):
    def test_scrap_pays_book_value_and_keeps_net_worth(self):
        ref = game()
        give(ref, 'amos', 'CR', 100_000)
        st = ref.get_vessel_location('amos')['station_id']
        ref.fleet.buy('amos')
        give(ref, 'amos/2', 'ORE', 40)
        nw = next(e for e in ref.get_leaderboard() if e['agent_id'] == 'amos')['net_worth']
        self.assertEqual(ref.fleet.scrap('amos', 'amos/1')['payload']['reason'], 'invalid_vessel')
        r = ref.fleet.scrap('amos', 'amos/2')
        self.assertEqual(r['payload']['paid'], F.SHIP_PRICES[2] // 2)
        self.assertEqual(ref.get_vessels('amos')[-1]['vessel_id'], 'amos/1')
        self.assertEqual(ref.get_balance(F.hold_account('amos', st), 'ORE'), 40)
        self.assertEqual(next(e for e in ref.get_leaderboard() if e['agent_id'] == 'amos')['net_worth'], nw)
        clean(self, ref)

    def test_upkeep_per_extra_ship_and_debt_when_short(self):
        ref = game(corporate=True)
        give(ref, 'amos', 'CR', 100_000)
        ref.fleet.buy('amos')
        ref.fleet.buy('amos')
        cr = ref.get_balance('amos', 'CR')
        rep = ref.step_round()
        self.assertEqual(rep['ship_upkeep'].get('amos'), 2 * F.SHIP_UPKEEP)
        paid = -sum(r[0] for r in ref.conn.execute(
            "SELECT delta FROM ledger_entries WHERE txn_id LIKE 'ship-upkeep-amos-%' AND agent_id = 'amos'"))
        self.assertEqual(paid, 2 * F.SHIP_UPKEEP)
        self.assertLessEqual(ref.get_balance('amos', 'CR'), cr - 2 * F.SHIP_UPKEEP)
        self.assertNotIn('zero', rep['ship_upkeep'])
        # Drain the corp: what it cannot pay becomes debt.
        left = ref.get_balance('amos', 'CR')
        with ref.lock, ref.conn:
            ref.fleet._move('test-drain', (('amos', 'CR', -left), ('SYSTEM', 'CR', left)))
        ref.step_round()
        # The shortfall was booked as debt (the same step's distress sale may already have paid it).
        ev = ref.conn.execute("SELECT detail FROM corp_events WHERE kind = 'debt' AND agent_id = 'amos'").fetchall()
        self.assertTrue(any('ship upkeep' in r[0] for r in ev), ev)
        clean(self, ref)


class TestLeaderboardAndStanding(unittest.TestCase):
    def test_each_ships_cargo_is_marked_where_that_ship_is(self):
        ref = game()
        give(ref, 'amos', 'CR', 100_000)
        st = ref.get_vessel_location('amos')['station_id']
        far = other(st)
        ref.fleet.buy('amos')
        dock(ref, 'amos/2', far)
        give(ref, 'amos/2', 'ORE', 100)
        give(ref, 'amos/1', 'ORE', 50)
        mark = lambda s: int(round(ref.spatial.get_station_price(s, 'ORE')))  # noqa: E731
        e = next(e for e in ref.get_leaderboard() if e['agent_id'] == 'amos')
        frag = 1000 * int(round(ref.spatial.get_station_price(st, 'FRAG')))
        expect = e['liquid'] + frag + 50 * mark(st) + 100 * mark(far) + F.SHIP_PRICES[2] // 2 + e['upgrades_value']
        self.assertEqual(e['net_worth'] - e.get('stocks_value', 0), expect)
        self.assertEqual((e['ships'], e['ships_value'], e['ore']), (2, F.SHIP_PRICES[2] // 2, 150))
        self.assertNotIn('amos/1', {x['agent_id'] for x in ref.get_leaderboard()})

    def test_two_ships_of_one_corp_crossing_book_no_lane_profit(self):
        ref = game(standing=True, depots=False)  # no depot quotes: the two ships meet each other
        give(ref, 'amos', 'CR', 100_000)
        st = ref.get_vessel_location('amos')['station_id']
        ref.fleet.buy('amos')
        ref.step_round()  # book the genesis and purchase
        before = {r['lane']: r['cum_profit'] for r in ref.conn.execute(
            "SELECT lane, cum_profit FROM standing_lanes WHERE agent_id = 'amos'")}
        px = 20
        order(ref, 'amos', 'ask', 200, px, 'FRAG', st, vessel='amos/1', oid='x-ask')
        r = order(ref, 'amos', 'bid', 200, px, 'FRAG', st, vessel='amos/2', oid='x-bid')
        self.assertEqual(r['payload'].get('filled_qty'), 200, r)
        self.assertEqual(ref.get_balance('amos/2', 'FRAG'), 200)
        ref.step_round()
        after = {r['lane']: r['cum_profit'] for r in ref.conn.execute(
            "SELECT lane, cum_profit FROM standing_lanes WHERE agent_id = 'amos'")}
        self.assertEqual(after, before)
        lots = ref.standing.book['lots']
        self.assertEqual(sum(l[2] for l in lots.get('amos/2', {}).get('FRAG', [])), 200)
        clean(self, ref)


class TestTakeover(unittest.TestCase):
    def _takeover(self, ref, raider, target):
        need = TAKEOVER_SHARES - ref.get_balance(raider, f"EQ_{ref.corporate._sym(target)[3:]}")
        sym = ref.corporate._sym(target)
        need = TAKEOVER_SHARES - ref.get_balance(raider, sym)
        with ref.lock, ref.conn:
            ref.corporate._move('test-buyup', ((target, sym, -need), (raider, sym, need)))
        ref.step_round()
        self.assertEqual(ref.corporate.status(target), 'absorbed')

    def test_absorbed_ships_are_renamed_kept_to_cap_and_the_rest_scrapped(self):
        ref = game(corporate=True, rival_shares=100, standing=True)
        give(ref, 'zero', 'CR', 200_000)
        give(ref, 'marvin', 'CR', 200_000)
        ref.fleet.buy('zero')  # zero/2: the raider owns 2, cap 3
        ref.fleet.buy('marvin')
        ref.fleet.buy('marvin')  # marvin/1..3
        m_st = ref.get_vessel_location('marvin')['station_id']
        give(ref, 'marvin/3', 'ORE', 70)
        cr = ref.get_balance('zero', 'CR')
        self._takeover(ref, 'zero', 'marvin')
        ships = {v['vessel_id']: v for v in ref.fleet.ships('zero', active_only=False)}
        # zero keeps its own 2, then marvin/1 as zero/3; marvin/2 and 3 are over the cap: scrapped.
        self.assertEqual(sorted(ships), ['zero/1', 'zero/2', 'zero/3'])
        self.assertIn('ex-marvin/1', ships['zero/3']['name'])
        self.assertEqual(ref.fleet.ships('marvin', active_only=False), [])
        self.assertEqual(ref.get_balance('zero/3', 'FRAG'), 1000)  # marvin/1's genesis cargo, renamed with it
        self.assertEqual(ref.get_balance(F.hold_account('zero', m_st), 'ORE'), 70)
        scrap = (F.SHIP_PRICES[2] + F.SHIP_PRICES[3]) // 2
        got = sum(r[0] for r in ref.conn.execute(
            "SELECT delta FROM ledger_entries WHERE txn_id LIKE 'ship-scrap-%' AND agent_id = 'zero' AND instrument = 'CR'"))
        self.assertEqual(got, scrap)
        self.assertGreaterEqual(ref.get_balance('zero', 'CR'), cr)
        clean(self, ref)

    def test_an_absorbed_ship_in_flight_over_the_cap_is_scrapped_when_it_lands(self):
        ref = game(corporate=True, rival_shares=100, standing=True)
        give(ref, 'zero', 'CR', 200_000)
        ref.fleet.buy('zero')
        ref.fleet.buy('zero')  # zero at its cap of 3
        m_st = ref.get_vessel_location('marvin')['station_id']
        dest = other(m_st)
        self.assertEqual(ref.initiate_transit('marvin', dest, commodity='FRAG', cargo_qty=300)['status'], 'in_transit')
        aboard = ref.conn.execute("SELECT cargo_qty FROM transits WHERE vessel_id = 'marvin/1' AND status = 'in_transit'").fetchone()[0]
        self._takeover(ref, 'zero', 'marvin')
        v = ref.conn.execute("SELECT vessel_id, status FROM vessels WHERE vessel_id = 'zero/4'").fetchone()
        self.assertEqual(tuple(v), ('zero/4', 'scrap_pending'))
        self.assertEqual(ref.conn.execute("SELECT agent_id FROM transits WHERE vessel_id = 'zero/4'").fetchone()[0], 'zero')
        for _ in range(15):
            ref.step_round()
        self.assertIsNone(ref.conn.execute("SELECT 1 FROM vessels WHERE vessel_id = 'zero/4'").fetchone())
        # Its remaining genesis FRAG and the cargo that landed wait in zero's hold at the destination.
        self.assertEqual(ref.get_balance(F.hold_account('zero', dest), 'FRAG'), 700 + aboard)
        self.assertEqual(len(ref.fleet.ships('zero')), 3)
        clean(self, ref)


class TestMigration(unittest.TestCase):
    def test_a_one_ship_database_upgrades_in_place_and_idempotently(self):
        path = Path(tempfile.mkdtemp()) / 'legacy.db'
        ref = AgoraReferee(db_path=str(path), depots=True)
        st = ref.get_vessel_location('amos')['station_id']
        # Rewind to the pre-#175-PR-2 shape: goods on the corp account, a
        # vessel_locations table, no orders.vessel_id, and a resting ask.
        with ref.lock, ref.conn:
            for corp in ('amos', 'zero', 'marvin', 'aerial'):
                for inst, bal in ref.conn.execute("SELECT instrument, balance FROM accounts WHERE agent_id = ?",
                                                  (f"{corp}/1",)).fetchall():
                    ref.conn.execute("UPDATE ledger_entries SET agent_id = ? WHERE agent_id = ? AND instrument = ?",
                                     (corp, f"{corp}/1", inst))
                    ref.conn.execute("INSERT INTO accounts (agent_id, instrument, balance) VALUES (?, ?, ?)", (corp, inst, bal))
                ref.conn.execute("DELETE FROM accounts WHERE agent_id = ?", (f"{corp}/1",))
            for name in ('vessel_locations_insert', 'vessel_locations_update', 'vessel_locations_delete'):
                ref.conn.execute(f"DROP TRIGGER {name}")
            ref.conn.execute("DROP VIEW vessel_locations")
            ref.conn.execute("""CREATE TABLE vessel_locations (agent_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,
                                docked_since INTEGER NOT NULL DEFAULT 0, updated_at TEXT)""")
            ref.conn.execute("INSERT INTO vessel_locations (agent_id, station_id) SELECT agent_id, station_id FROM vessels")
            ref.conn.execute("INSERT INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, station_id) "
                             "VALUES ('old-ask', 'amos', 'FRAG', 'ask', 100, 999, 0, 'open', ?)", (st,))
        ref.conn.close()
        for boot in (1, 2):
            ref = AgoraReferee(db_path=str(path), depots=True)
            ok, errs = ref.verify_ledger_invariants()
            self.assertTrue(ok, errs)
            self.assertEqual(ref.conn.execute("SELECT type FROM sqlite_master WHERE name = 'vessel_locations'").fetchone()[0], 'view')
            self.assertEqual(ref.get_balance('amos/1', 'FRAG'), 1000)
            self.assertEqual(ref.conn.execute("SELECT COUNT(DISTINCT txn_id) FROM ledger_entries "
                                              "WHERE txn_id LIKE 'ship-migrate-%'").fetchone()[0], 4)
            ask = next(o for o in ref.books[st]['FRAG'].asks if o.order_id == 'old-ask')
            self.assertEqual((ask.acct, ask.vessel_id), ('amos/1', 'amos/1'))
            ref.conn.close()


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = game()
        give(cls.ref, 'amos', 'CR', 100_000)
        tokens = {'amos': 'ta', 'zero': 'tz', 'admin': 'tadm'}
        cls.server = HTTPServer(('127.0.0.1', 0), make_handler(cls.ref, auth_tokens=tokens))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _call(self, method, path, token=None, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Authorization': f'Bearer {token}'} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_buy_transfer_scrap_and_read_the_fleet(self):
        s, d = self._call('POST', '/referee/vessels/buy', 'ta', {})
        self.assertEqual((s, d['kind']), (200, 'ship_bought'), d)
        vid = d['payload']['vessel_id']
        s, d = self._call('POST', '/referee/vessels/transfer', 'ta', {'from': 'amos/1', 'to': vid, 'instrument': 'FUEL', 'qty': 50})
        self.assertEqual(s, 200, d)
        s, d = self._call('GET', '/referee/vessels?agent_id=amos')
        self.assertEqual(s, 200)
        fleet = d['fleet']
        self.assertEqual(fleet['owned'], 2)
        self.assertEqual(fleet['next_ship']['price'], F.SHIP_PRICES[3])
        self.assertEqual(next(x for x in fleet['ships'] if x['vessel_id'] == vid)['hold'], {'FUEL': 50})
        s, d = self._call('GET', '/referee/accounts', 'ta')
        self.assertEqual({a['instrument']: a['balance'] for a in d['accounts']}['FUEL'], 500)  # corp total
        self.assertEqual(d['ships'][vid], {'FUEL': 50})
        # A fleet token acts only as itself: zero cannot move amos's goods or scrap its ship.
        s, d = self._call('POST', '/referee/vessels/transfer', 'tz', {'from': 'amos/1', 'to': vid, 'instrument': 'FUEL', 'qty': 1})
        self.assertEqual((s, d['payload']['reason']), (400, 'invalid_vessel'))
        s, d = self._call('POST', '/referee/vessels/scrap', 'tz', {'vessel_id': vid})
        self.assertEqual((s, d['payload']['reason']), (400, 'invalid_vessel'))
        s, d = self._call('POST', '/referee/vessels/buy', 'tadm', {'agent_id': 'amos/1'})
        self.assertEqual(s, 400)
        s, d = self._call('POST', '/referee/vessels/scrap', 'ta', {'vessel_id': vid})
        self.assertEqual((s, d['kind']), (200, 'ship_scrapped'), d)
        ok, errs = self.ref.verify_ledger_invariants()
        self.assertTrue(ok, errs)

    def test_transit_and_order_take_a_vessel_id(self):
        st = self.ref.get_vessel_location('amos')['station_id']
        s, d = self._call('POST', '/stations/transit', 'tz', {'destination': other(st), 'vessel_id': 'amos/1'})
        self.assertEqual((s, d['payload']['reason']), (400, 'invalid_vessel'))
        s, d = self._call('POST', '/referee/orders', 'tz', {'v': 1, 'kind': 'order', 'payload': {
            'order_id': 'h1', 'agent_id': 'zero', 'side': 'ask', 'qty': 1, 'limit_price': 1, 'instrument': 'FRAG',
            'station_id': self.ref.get_vessel_location('zero')['station_id'], 'seq_seen': 0, 'vessel_id': 'amos/1'}})
        self.assertEqual(d['payload']['reason'], 'invalid_vessel')


if __name__ == '__main__':
    unittest.main()
