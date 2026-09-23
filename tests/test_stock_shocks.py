"""Stock reactions to news and capitalized upgrades (#151)."""
import unittest

from agora import exchange as X
from agora import piracy as P
from agora import upgrades as U
from agora.referee import AgoraReferee

CARGO = 1000


class Fixed:
    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0) if len(self.vals) > 1 else self.vals[0]

    def randint(self, a, b):
        return a


def game(events=True, seed=7, piracy=(0.0001, 0.0001), **kw):
    opts = dict(depots=True, depot_model='static', piracy=piracy, events=events, upgrades=True,
                rival_shares=100, exchange_shares=100, contracts=True)
    opts.update(kw)
    ref = AgoraReferee(**opts)
    ref.new_game(seed=seed, warmup_rounds=2, **opts)
    return ref


def record(ref, *a, **kw):
    with ref.lock, ref.conn:
        return ref.events.record_locked(*a, **kw)


def px(ref, sym='EQ_AMOS'):
    return ref.exchange.price[sym]


class TestShockTable(unittest.TestCase):
    def test_table(self):
        self.assertEqual(X.shock_for({'kind': 'upgrade', 'actor': 'amos'}), ('amos', 0.02))
        self.assertEqual(X.shock_for({'kind': 'escort', 'actor': 'amos'}), ('amos', 0.01))
        self.assertEqual(X.shock_for({'kind': 'raid_repelled', 'victim': 'amos'}), ('amos', 0.01))
        self.assertEqual(X.shock_for({'kind': 'contract_lapse', 'actor': 'amos'}), ('amos', -0.03))
        self.assertEqual(X.shock_for({'kind': 'privateer_contract', 'actor': 'zero', 'victim': 'amos'}),
                         ('zero', -0.08))
        self.assertEqual(X.shock_for({'kind': 'sabotage', 'actor': 'zero', 'victim': 'amos'}), ('zero', -0.08))
        self.assertEqual(X.shock_for({'kind': 'stake_20', 'actor': 'zero', 'victim': 'amos'}), ('amos', 0.03))
        self.assertAlmostEqual(X.shock_for({'kind': 'hazard_loss', 'victim': 'amos', 'amount': 25_000})[1], -0.025)
        self.assertEqual(X.shock_for({'kind': 'pirate_loss', 'victim': 'amos', 'amount': 80_000}), ('amos', -0.05))
        self.assertIsNone(X.shock_for({'kind': 'hazard_loss', 'victim': 'amos', 'amount': 0}))
        self.assertIsNone(X.shock_for({'kind': 'debt', 'actor': 'amos'}))


class TestShocks(unittest.TestCase):
    def test_public_event_jumps_ref_price_not_nav(self):
        ref = game()
        nav0 = ref.stock_marks({b['agent_id']: b['net_worth'] - b['stocks_value'] for b in ref.get_leaderboard()})
        p0 = px(ref)
        record(ref, 'contract_lapse', 'public', actor='amos', detail='missed')
        self.assertAlmostEqual(px(ref), p0 * 0.97)
        nav1 = ref.stock_marks({b['agent_id']: b['net_worth'] - b['stocks_value'] for b in ref.get_leaderboard()})
        self.assertEqual(nav0['EQ_AMOS']['nav'], nav1['EQ_AMOS']['nav'])
        self.assertEqual(ref.exchange.summary()['recent_shocks'][-1]['kind'], 'contract_lapse')

    def test_shock_fades_through_reversion(self):
        # Same seeded game twice, one shocked: the gap decays by REVERSION a round.
        plain, hit = game(exchange_vol=0.0), game(exchange_vol=0.0)
        for ref in (plain, hit):
            for _ in range(5):
                ref.step_round()
        record(hit, 'contract_lapse', 'public', actor='amos', detail='missed')
        gap0 = px(plain) - px(hit)
        self.assertAlmostEqual(gap0, px(plain) * 0.03)
        for _ in range(10):
            plain.step_round()
            hit.step_round()
        gap = px(plain) - px(hit)
        self.assertGreater(gap, 0)
        self.assertAlmostEqual(gap, gap0 * (1 - X.REVERSION) ** 10, places=6)

    def test_private_and_secret_move_nothing_until_exposed(self):
        ref = game()
        p0, z0 = px(ref), px(ref, 'EQ_ZERO')
        ref.piracy.hire('zero', 'amos')
        ref.piracy.rng = Fixed(0.0, 0.9)  # raided, not traced
        ref.initiate_transit('amos', 'mars', 'FRAG', CARGO)
        self.assertEqual(px(ref, 'EQ_ZERO'), z0)
        self.assertEqual(px(ref), p0)
        self.assertEqual([s for s in ref.exchange.shocks if s['symbol'] == 'EQ_ZERO'], [])
        cid = ref.piracy.active_contracts('zero')[0]['contract_id']
        z1 = px(ref, 'EQ_ZERO')
        with ref.lock, ref.conn:
            ref.events.expose_link_locked(cid, 'leak')
        self.assertAlmostEqual(px(ref, 'EQ_ZERO'), z1 * 0.92)

    def test_second_trace_no_second_scandal_shock(self):
        ref = game()
        ref.piracy.hire('zero', 'amos')
        ref.piracy.rng = Fixed(0.0, 0.0)
        ref.initiate_transit('amos', 'mars', 'FRAG', CARGO // 2)
        ref.step_round()
        ref.step_round()
        ref.step_round()
        ref.piracy.rng = Fixed(0.0, 0.0)
        ref.initiate_transit('amos', 'earth', 'FRAG', 50)
        self.assertEqual(len([s for s in ref.exchange.shocks if s['kind'] == 'privateer_contract']), 1)

    def test_upgrade_escort_and_losses_from_live_actions(self):
        ref = game(piracy=(1, 1))
        p0 = px(ref)
        self.assertEqual(ref.upgrades.buy('amos', 'armor')['kind'], 'upgrade_ok')
        self.assertAlmostEqual(px(ref), p0 * 1.02)
        p1 = px(ref)
        ref.piracy.rng = Fixed(0.0, 0.9)
        r = ref.initiate_transit('amos', 'mars', 'FRAG', CARGO, escort=True)['payload']
        self.assertTrue(r['piracy']['raided'])
        self.assertAlmostEqual(px(ref), p1 * 1.01)  # escort
        p2 = px(ref)
        ref.piracy.respond('amos', r['transit_id'], 'surrender')
        lost = P.cargo_value('FRAG', int(CARGO * P.SURRENDER_PCT))
        self.assertAlmostEqual(px(ref), p2 * (1 + max(-0.05, -0.01 * lost / 10_000)))
        ev = [e for e in ref.events.visible_to(None) if e['kind'] == 'pirate_loss'][0]
        self.assertEqual(ev['amount'], lost)

    def test_raid_repelled(self):
        ref = game(piracy=(1, 1))
        ref.piracy.rng = Fixed(0.0, 0.9)
        tid = ref.initiate_transit('amos', 'mars', 'FRAG', CARGO)['payload']['transit_id']
        p0 = px(ref)
        ref.piracy.rng = Fixed(0.0)  # escape roll < FIGHT_ESCAPE
        ref.piracy.respond('amos', tid, 'fight')
        self.assertAlmostEqual(px(ref), p0 * 1.01)

    def test_hazard_loss(self):
        ref = game(piracy=None, hazards=(0, 1))
        p0 = px(ref)
        ref.initiate_transit('amos', 'mars', 'FRAG', CARGO)
        ev = [e for e in ref.events.visible_to(None) if e['kind'] == 'hazard_loss'][0]
        self.assertGreater(ev['amount'], 0)
        self.assertAlmostEqual(px(ref), p0 * (1 + max(-0.05, -0.01 * ev['amount'] / 10_000)))

    def test_contract_lapse_from_live_contract(self):
        ref = game(piracy=None)
        with ref.lock, ref.conn:
            ref.conn.execute("INSERT INTO station_contracts (contract_id, station_id, instrument, qty_total, "
                             "qty_remaining, price, posted_round, deadline, owner, status) "
                             "VALUES ('kx', 'mars', 'FRAG', 10, 10, 20, 0, 0, 'amos', 'open')")
        ref.step_round()  # exchange refresh, then the lapse
        shocks = [s for s in ref.exchange.shocks if s['kind'] == 'contract_lapse']
        self.assertEqual(len(shocks), 1)
        self.assertEqual((shocks[0]['symbol'], shocks[0]['pct']), ('EQ_AMOS', -0.03))

    def test_stake_20_takeover_premium(self):
        ref = game(piracy=None)
        with ref.lock, ref.conn:
            ref.piracy._move('test-stake', (('marvin', 'EQ_AMOS', -100), ('zero', 'EQ_AMOS', 100)))
        ref.step_round()
        s = [s for s in ref.exchange.shocks if s['kind'] == 'stake_20']
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]['symbol'], s[0]['pct']), ('EQ_AMOS', 0.03))

    def test_events_off_no_shocks(self):
        ref = game(events=False, piracy=(1, 1))
        p0 = px(ref)
        ref.upgrades.buy('amos', 'armor')
        record(ref, 'contract_lapse', 'public', actor='amos', detail='x')
        self.assertEqual(px(ref), p0)
        self.assertEqual(ref.exchange.shocks, [])

    def test_seeded_reproducible(self):
        def run():
            ref = game(seed=11, piracy=(0.3, 0.3), hazards=(0.2, 0.3))
            ref.piracy.hire('zero', 'amos')
            ref.upgrades.buy('marvin', 'hold')
            dest = {'amos': 'mars', 'zero': 'earth', 'marvin': 'luna', 'aerial': 'mars'}
            for r in range(12):
                for a in sorted(dest):
                    ref.initiate_transit(a, dest[a] if r % 2 == 0 else 'ceres', 'FRAG',
                                         min(200, ref.get_balance(a, 'FRAG')), escort=(a == 'aerial'))
                ref.step_round()
            return dict(ref.exchange.price), [(s['kind'], s['symbol']) for s in ref.exchange.shocks]
        a, b = run(), run()
        self.assertEqual(a, b)
        self.assertTrue(a[1])


class TestCapitalizedUpgrades(unittest.TestCase):
    def nw(self, ref, agent='amos'):
        return {b['agent_id']: b for b in ref.get_leaderboard()}[agent]

    def test_half_the_price_stays_in_net_worth_and_nav(self):
        ref = game(piracy=None, events=False)
        before = self.nw(ref)['net_worth']
        price = U.CATALOG['armor']['prices'][0]
        ref.upgrades.buy('amos', 'armor')
        row = self.nw(ref)
        self.assertEqual(row['upgrades_value'], int(price * U.CAPITAL_PCT))
        self.assertEqual(row['net_worth'], before - price + int(price * U.CAPITAL_PCT))
        self.assertEqual(ref.upgrades.buy('amos', 'shielding')['kind'], 'upgrade_ok')
        total = U.CATALOG['armor']['prices'][0] + U.CATALOG['shielding']['prices'][0]
        self.assertEqual(self.nw(ref)['upgrades_value'], int(total * U.CAPITAL_PCT))
        base = {b['agent_id']: b['net_worth'] - b['stocks_value'] for b in ref.get_leaderboard()}
        self.assertEqual(ref.stock_marks(base)['EQ_AMOS']['nav'], max(1.0, round(base['amos'] / 1000, 2)))

    def test_out_corp_has_no_upgrade_value(self):
        ref = game(piracy=None, events=False)
        ref.upgrades.buy('amos', 'armor')
        ref.fleet_out = lambda a: 'gone' if a == 'amos' else None
        self.assertEqual(self.nw(ref)['upgrades_value'], 0)


if __name__ == '__main__':
    unittest.main()
