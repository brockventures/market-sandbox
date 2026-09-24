"""Marble-bag draws for yes/no outcome rolls (#214): agora/bag.py and the
sites that use it (hazards, piracy, covert, events)."""
import os
import random
import sqlite3
import tempfile
import unittest

from agora import bag as B
from agora import covert as C
from agora import events as E
from agora import piracy as P
from agora.bag import Bags, composition
from agora.referee import AgoraReferee
from agora.upgrades import CATALOG as UPGRADES


def bags(seed=1, conn=None, ns='t'):
    b = Bags(conn or sqlite3.connect(':memory:'), ns)
    b.reset(seed)
    return b


def streaks(seq):
    best, cur, prev = {True: 0, False: 0}, 0, None
    for x in seq:
        cur = cur + 1 if x == prev else 1
        prev = x
        best[x] = max(best[x], cur)
    return best[True], best[False]


class TestComposition(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(composition(0.10), (1, 10))
        self.assertEqual(composition(0.14), (7, 50))
        self.assertEqual(composition(0.25), (1, 4))
        self.assertEqual(composition(0.5), (1, 2))
        self.assertEqual(composition(0.02), (1, 50))
        self.assertEqual(composition(0.1 * 0.45), (9, 200))   # float noise: 0.045000000000000005
        self.assertEqual(composition(0.2 * 0.85), (17, 100))  # 0.16999999999999998

    def test_no_bag_for_p_off_the_grid(self):
        for p in (1 / 3 + 1e-6, 0.0001, 0.123456, 1 / 201):
            self.assertIsNone(composition(p), p)
        for p in (0, 1, -0.1, 1.5):
            self.assertIsNone(composition(p), p)

    def test_every_live_hazard_and_trace_odds_is_an_exact_bag(self):
        """Every p a live fixed-odds site can ask for: hazard base odds x each
        upgrade tier, and the trace, escape and leak constants."""
        from agora.hazards import DEFAULT_P_DELAY, DEFAULT_P_LOSS
        ps = [P.PRIV_TRACE, P.FIGHT_ESCAPE, C.SABOTAGE_TRACE, E.LEAK_CHANCE]
        for base, kind in ((DEFAULT_P_DELAY, 'shielding'), (DEFAULT_P_LOSS, 'hold')):
            ps += [base * f for f in [1.0] + list(UPGRADES[kind]['factors'])]
        for p in ps:
            k, n = composition(p)
            self.assertLessEqual(n, B.MAX_N)
            self.assertAlmostEqual(k / n, p, places=9)


class TestBag(unittest.TestCase):
    def test_exact_rate_over_whole_bags(self):
        b = bags()
        for p, (k, n) in ((0.10, (1, 10)), (0.14, (7, 50)), (0.25, (1, 4))):
            draws = [b.draw(f"e{p}", 'amos', p) for _ in range(n * 30)]
            self.assertEqual(sum(draws), k * 30, p)
            for i in range(30):  # every single bag holds exactly k hits
                self.assertEqual(sum(draws[i * n:(i + 1) * n]), k)

    def test_streaks_bounded(self):
        b = bags(seed=3)
        for p in (0.10, 0.02, 0.25, 0.14, 0.045):
            k, n = composition(p)
            seq = [b.draw('e', f"f{p}", p) for _ in range(n * 200)]
            hit, miss = streaks(seq)
            self.assertLessEqual(hit, 2)
            self.assertLessEqual(miss, 2 * (-(-n // k) - 1))

    def test_independent_rng_does_run_longer(self):
        """Sanity for the claim: plain rolls at p=0.10 over the same length run
        well past the bag's 18-miss bound."""
        rng = random.Random(3)
        _, miss = streaks([rng.random() < 0.10 for _ in range(2000)])
        self.assertGreater(miss, 18)

    def test_deterministic_per_seed(self):
        def run(seed):
            b = bags(seed)
            return [b.draw('e', 'amos', 0.1) for _ in range(40)]
        self.assertEqual(run(5), run(5))
        self.assertNotEqual(run(5), run(6))

    def test_per_fleet_isolation(self):
        a1 = bags(seed=2)
        alone = [a1.draw('e', 'amos', 0.1) for _ in range(40)]
        a2 = bags(seed=2)
        mixed = []
        for _ in range(40):
            a2.draw('e', 'zero', 0.1)
            a2.draw('e', 'zero', 0.1)
            mixed.append(a2.draw('e', 'amos', 0.1))
        self.assertEqual(alone, mixed)
        self.assertEqual(a2.stats('e', 'zero'), {'draws': 80, 'hits': 8})

    def test_persists_across_restart(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            straight = [x for b in [bags(4)] for x in (b.draw('e', 'amos', 0.14) for _ in range(60))]
            conn = sqlite3.connect(path)
            b = bags(4, conn=conn)
            first = [b.draw('e', 'amos', 0.14) for _ in range(23)]
            conn.commit()
            conn.close()
            b2 = Bags(sqlite3.connect(path), 't')  # a restart: no reset
            self.assertEqual(b2.seed, 4)
            rest = [b2.draw('e', 'amos', 0.14) for _ in range(37)]
            self.assertEqual(first + rest, straight)
        finally:
            os.unlink(path)

    def test_reset_wipes(self):
        b = bags(1)
        b.draw('e', 'amos', 0.1)
        b.reset(9)
        self.assertEqual(b.stats('e', 'amos'), {'draws': 0, 'hits': 0})
        self.assertEqual(b.seed, 9)

    def test_p_change_rebuilds_bag(self):
        b = bags(1)
        for _ in range(3):
            b.draw('e', 'amos', 0.1)
        seq = [b.draw('e', 'amos', 0.25) for _ in range(40)]
        self.assertEqual(sum(seq), 10)

    def test_edges_and_force(self):
        b = bags(1)
        self.assertFalse(b.draw('e', 'amos', 0))
        self.assertTrue(b.draw('e', 'amos', 1.0))
        self.assertTrue(b.draw_varying('e', 'amos', 1.5))
        self.assertEqual(b.stats('e', 'amos')['draws'], 0)
        b.force('e', True, False)
        self.assertEqual([b.draw('e', 'x', 0.0), b.draw_varying('e', 'x', 1.0)], [True, False])
        self.assertEqual(b.stats('e', 'x')['draws'], 0)


class TestAccumulator(unittest.TestCase):
    def test_rate_equals_mean_p_and_streaks_bounded(self):
        rng = random.Random(11)
        b = bags(7)
        total, hits, seq = 0.0, 0, []
        for _ in range(5000):
            p = rng.uniform(0.05, 0.30)
            h = b.draw_varying('raid', 'amos', p)
            total += p
            hits += h
            seq.append(h)
            self.assertLess(abs(total - hits), 2.0)  # at every prefix
        hit, miss = streaks(seq)
        self.assertLessEqual(hit, 2)
        self.assertLess(miss, 2 / 0.05)

    def test_hit_streaks_bounded_at_high_odds(self):
        rng = random.Random(12)
        b = bags(9)
        seq = [b.draw_varying('raid', 'amos', rng.uniform(0.3, 0.6)) for _ in range(5000)]
        self.assertLess(streaks(seq)[0], 2 / (1 - 0.6))

    def test_off_grid_fixed_p_falls_back_to_accumulator(self):
        b = bags(7)
        p = 1 / 3 + 1e-6
        seq = [b.draw('e', 'amos', p) for _ in range(3000)]
        self.assertLess(abs(sum(seq) - 3000 * p), 2)

    def test_deterministic_and_isolated(self):
        def run(other):
            b = bags(8)
            out = []
            for i in range(200):
                if other:
                    b.draw_varying('raid', 'zero', 0.3)
                out.append(b.draw_varying('raid', 'amos', 0.05 + (i % 7) / 50))
            return out
        self.assertEqual(run(False), run(True))


# ------------------------------------------------------------ converted sites

def hz_game(odds, seed=5):
    ref = AgoraReferee(hazards=odds)
    ref.new_game(seed=seed, warmup_rounds=2, hazards=odds)
    return ref


class TestHazardSites(unittest.TestCase):
    def test_loss_is_one_trip_in_ten_per_fleet(self):
        eng = hz_game('0,0.1').hazards
        for agent in ('amos', 'zero'):
            lost = [eng.roll(100, agent_id=agent)[1] > 0 for _ in range(100)]
            self.assertEqual(sum(lost), 10, agent)
            for i in range(10):
                self.assertEqual(sum(lost[i * 10:(i + 1) * 10]), 1)

    def test_empty_trips_draw_no_loss_marble(self):
        eng = hz_game('0,0.1').hazards
        for _ in range(7):
            eng.roll(0, agent_id='amos')
        self.assertEqual(eng.bags.stats('loss', 'amos')['draws'], 0)

    def test_delay_rate_and_upgrade_factor(self):
        eng = hz_game('0.2,0').hazards
        self.assertEqual(sum(eng.roll(0, agent_id='amos')[0] > 0 for _ in range(100)), 20)
        # shielding tier 2 (x0.6): 0.12 = 3 in 25
        self.assertEqual(sum(eng.roll(0, delay_factor=0.6, agent_id='amos')[0] > 0 for _ in range(100)), 12)

    def test_live_transit_uses_the_ships_bag(self):
        # A trip draws from the flying ship's bag (#175), not the fleet's.
        ref = hz_game('0.2,0.1')
        ref.initiate_transit('amos', 'mars', commodity='FRAG', cargo_qty=10)
        self.assertEqual(ref.hazards.bags.stats('delay', 'amos/1')['draws'], 1)
        self.assertEqual(ref.hazards.bags.stats('loss', 'amos/1')['draws'], 1)
        self.assertEqual(ref.hazards.bags.stats('delay', 'zero/1')['draws'], 0)

    def test_bags_survive_a_referee_restart(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            straight = hz_game('0,0.1', seed=6)
            want = [straight.hazards.roll(100, agent_id='amos/1')[1] > 0 for _ in range(30)]
            ref = AgoraReferee(db_path=path, hazards='0,0.1')
            ref.new_game(seed=6, warmup_rounds=2, hazards='0,0.1')
            got = [ref.hazards.roll(100, agent_id='amos/1')[1] > 0 for _ in range(13)]
            ref.conn.commit()
            ref.conn.close()
            again = AgoraReferee(db_path=path, hazards='0,0.1')  # a restart: no new_game
            self.assertEqual(again.hazards.bags.stats('loss', 'amos/1')['draws'], 13)
            got += [again.hazards.roll(100, agent_id='amos/1')[1] > 0 for _ in range(17)]
            self.assertEqual(got, want)
        finally:
            os.unlink(path)

    def test_seeded_game_repeats_and_new_game_wipes(self):
        def run(ref):
            return [ref.hazards.roll(100, agent_id='amos')[:2] for _ in range(30)]
        a, b = hz_game('0.2,0.1', seed=3), hz_game('0.2,0.1', seed=3)
        self.assertEqual(run(a), run(b))
        a.new_game(seed=3, warmup_rounds=2, hazards='0.2,0.1')
        self.assertEqual(a.hazards.bags.stats('loss', 'amos'), {'draws': 0, 'hits': 0})
        self.assertEqual(run(a), run(hz_game('0.2,0.1', seed=3)))
        a.reset_to_genesis()
        self.assertEqual(a.conn.execute("SELECT COUNT(*) FROM rng_bags WHERE event != '__seed__'").fetchone()[0], 0)


def pir_game(odds=(1, 1), seed=7, **kw):
    ref = AgoraReferee(depots=True, depot_model='static', piracy=odds, **kw)
    ref.new_game(seed=seed, warmup_rounds=2, depots=True, piracy=odds, **kw)
    return ref


def depart(ref, n, agent='amos', qty=1000, tag='t', escort=False, vessel=None):
    out = []
    for i in range(n):
        with ref.lock, ref.conn:
            out.append(ref.piracy.roll_departure_locked(f"{tag}{i}", agent, 'ceres', 'mars', True, 'FRAG', qty,
                                                        escort, 0, ref.current_round, vessel_id=vessel))
    return out


def raid_draws(ref, ship, escort=None):
    """Raid draws and hits over every bag of one ship (#175: raid_key)."""
    q = "SELECT COALESCE(SUM(draws), 0), COALESCE(SUM(hits), 0) FROM rng_bags WHERE ns = 'piracy' AND event = 'raid' AND fleet LIKE ?"
    rows = ref.conn.execute(q, (ship + '|%' + ('' if escort is None else ('|escort' if escort else '|bare')),)).fetchone()
    return {'draws': rows[0], 'hits': rows[1]}


class TestPiracySites(unittest.TestCase):
    def test_raid_rate_tracks_the_odds(self):
        ref = pir_game(odds=(0.1, 0.1))
        outs = depart(ref, 300)
        total = sum(o['odds'] for o in outs)
        raided = [o['raided'] for o in outs]
        self.assertGreater(total, 10)
        self.assertLess(abs(sum(raided) - total), 2)
        self.assertLess(streaks(raided)[1], 2 / min(o['odds'] for o in outs))
        self.assertEqual(raid_draws(ref, 'amos/1')['draws'], 300)

    def test_escorted_trips_draw_from_their_own_bag(self):
        # Ryan (#175): escort and armor luck is the ship's. However many bare
        # trips came first, escorted trips hit at exactly the escorted odds:
        # a whole number of escorted bags holds exactly its hits.
        ref = pir_game(odds=(0.4, 0.4))
        c = ref.piracy.chance('amos', 'ceres', 'mars', True, 'FRAG', 1000, True, ref.current_round)
        k, n = composition(P.PiracyDesk.bag_odds(c['exact_odds']))
        for bare in (0, 7, 23):
            ref = pir_game(odds=(0.4, 0.4))
            depart(ref, bare, tag='b')
            outs = depart(ref, 3 * n, escort=True, tag='e')
            self.assertEqual(sum(o['raided'] for o in outs), 3 * k, bare)
            self.assertEqual(raid_draws(ref, 'amos/1', escort=True), {'draws': 3 * n, 'hits': 3 * k})
            self.assertEqual(raid_draws(ref, 'amos/1', escort=False)['draws'], bare)

    def test_two_ships_of_one_corp_have_independent_bags(self):
        ref = pir_game(odds=(0.4, 0.4), hazards='0.2,0.2')
        with ref.lock, ref.conn:
            ref.piracy._move('test-cash', (('SYSTEM', 'CR', -100_000), ('amos', 'CR', 100_000)))
        self.assertEqual(ref.fleet.buy('amos')['payload']['vessel_id'], 'amos/2')
        solo = pir_game(odds=(0.4, 0.4), hazards='0.2,0.2')
        a = [o['raided'] for o in depart(ref, 40, vessel='amos/2', tag='s2')]  # ship 2 flies first
        b = [o['raided'] for o in depart(ref, 40, vessel='amos/1', tag='s1')]
        self.assertEqual(b, [o['raided'] for o in depart(solo, 40, vessel='amos/1', tag='s1')])
        self.assertEqual(raid_draws(ref, 'amos/2')['draws'], 40)
        self.assertEqual(raid_draws(ref, 'amos/1')['draws'], 40)
        # Hazard bags too: ship 2's trips leave ship 1's draws untouched.
        h2 = [ref.hazards.roll(100, agent_id='amos/2')[1] > 0 for _ in range(20)]
        h1 = [ref.hazards.roll(100, agent_id='amos/1')[1] > 0 for _ in range(20)]
        self.assertEqual(ref.hazards.bags.stats('loss', 'amos/1')['draws'], 20)
        self.assertEqual(ref.hazards.bags.stats('loss', 'amos/2')['draws'], 20)
        self.assertEqual(h1, [solo.hazards.roll(100, agent_id='amos/1')[1] > 0 for _ in range(20)])
        self.assertEqual(sum(h2), 4)
        self.assertTrue(a)

    def test_a_pre_ships_database_moves_its_bags_to_ship_one(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            ref = AgoraReferee(db_path=path, hazards='0,0.1', piracy=(0.4, 0.4))
            ref.new_game(seed=6, warmup_rounds=2, hazards='0,0.1', piracy=(0.4, 0.4))
            for _ in range(7):
                ref.hazards.roll(100, agent_id='amos')  # the old key: the corp
            with ref.lock, ref.conn:
                ref.piracy.bags.draw_varying('raid', 'amos', 0.3)
                ref.piracy.bags.draw('escape', 'zero', 0.5)
                ref.covert.bags.draw('sabotage_trace', 'zero', 0.25)
            ref.conn.commit()
            ref.conn.close()
            for _ in range(2):  # a restart, twice
                again = AgoraReferee(db_path=path, hazards='0,0.1', piracy=(0.4, 0.4))
                self.assertEqual(again.hazards.bags.stats('loss', 'amos/1')['draws'], 7)
                self.assertEqual(again.hazards.bags.stats('loss', 'amos')['draws'], 0)
                self.assertEqual(again.piracy.bags.stats('escape', 'zero/1')['draws'], 1)
                self.assertEqual(again.piracy.bags.stats('raid', 'amos')['draws'], 0)
                self.assertEqual(again.covert.bags.stats('sabotage_trace', 'zero')['draws'], 0)
                again.conn.commit()
                again.conn.close()
        finally:
            os.unlink(path)

    def test_empty_trip_draws_nothing(self):
        ref = pir_game(odds=(0.1, 0.1))
        depart(ref, 5, qty=0)
        self.assertEqual(raid_draws(ref, 'amos/1')['draws'], 0)

    def test_trace_is_one_in_ten_from_the_sponsors_bag(self):
        ref = pir_game()
        ref.piracy.hire('zero', 'amos')
        depart(ref, 20)
        traced = ref.conn.execute("SELECT SUM(traced), COUNT(*) FROM piracy_raids").fetchone()
        self.assertEqual(tuple(traced), (2, 20))
        self.assertEqual(ref.piracy.bags.stats('trace', 'zero'), {'draws': 20, 'hits': 2})

    def test_fight_escape_is_one_in_two(self):
        ref = pir_game()
        seen = []
        for _ in range(4):
            loc = ref.get_vessel_location('amos')
            dest = 'luna' if loc.get('station_id') == 'mars' else 'mars'
            ref.piracy.bags.force('raid', True)  # this test is about the escape roll
            r = ref.initiate_transit('amos', dest, 'FRAG', 100)['payload']
            self.assertTrue(r['piracy']['raided'], r)
            seen.append(ref.piracy.respond('amos', r['transit_id'], 'fight')['payload']['status'])
            while ref.get_vessel_location('amos')['status'] == 'in_transit':
                ref.step_round()
        self.assertEqual(sorted(seen), ['escaped', 'escaped', 'lost', 'lost'])
        self.assertEqual(sorted(seen[:2]), ['escaped', 'lost'])
        self.assertEqual(ref.piracy.bags.stats('escape', 'amos/1')['draws'], 4)


class TestCovertAndLeakSites(unittest.TestCase):
    def test_sabotage_trace_is_one_in_four_from_the_saboteurs_bag(self):
        ref = AgoraReferee(events=True, corporate=True)
        ref.new_game(seed=4, warmup_rounds=2, events=True, corporate=True)
        traced = []
        for i in range(4):
            with ref.lock, ref.conn:
                ref.piracy._move(f"test-topup-{i}", (('SYSTEM', 'CR', -10_000), ('zero', 'CR', 10_000)))
            res = ref.covert.execute_sabotage('zero', 'amos', mode='docked')
            self.assertEqual(res['kind'], 'sabotage_ok', res)
            traced.append(res['payload']['traced'])
            ref.current_round += C.SABOTAGE_COOLDOWN
        self.assertEqual(sum(traced), 1)
        # Per saboteur and target ship (#175).
        self.assertEqual(ref.covert.bags.stats('sabotage_trace', 'zero>amos/1')['draws'], 4)

    def test_leak_is_one_in_fifty_from_the_actors_bag(self):
        ref = AgoraReferee(events=True)
        ref.new_game(seed=4, warmup_rounds=2, events=True)
        with ref.lock, ref.conn:
            for i in range(50):
                ref.events.record_locked('sabotage', 'secret', actor='zero', victim='amos', detail=f"x{i}")
            ref.events.record_locked('sabotage', 'secret', actor='marvin', victim='amos', detail='y')
        rep = ref.step_round()
        self.assertEqual(len([e for e in rep['events']['leaked']]) - ref.events.bags.stats('leak', 'marvin')['hits'], 1)
        self.assertEqual(ref.events.bags.stats('leak', 'zero'), {'draws': 50, 'hits': 1})
        self.assertEqual(ref.events.bags.stats('leak', 'marvin')['draws'], 1)


if __name__ == '__main__':
    unittest.main()
