"""
tests/test_sim_live_parity.py - the simulator plays the live game (#155).

Ryan, #agent-chat 2026-09-23: "the simulator should simulate the same game
as our live game." tools/economy_sim.py builds its referee with the live
server's own factory, agora.server.build_referee_from_env, and starts the
game as a bare POST /referee/admin/new_game does. These tests fail if it
stops doing so: if the simulator builds its own referee, passes different
settings, reads the caller's AGORA_* environment, or ends up with any
feature flag or constant different from the live server's. Both checks are
generic: a flag added to build_referee_from_env is covered with no change
here.
"""

import os
import unittest
from unittest import mock

import agora.server as server
from tools import economy_sim as sim

SEED = 7
_PRIMITIVE = (bool, int, float, str, type(None))


def _clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith('AGORA_')}


def _primitive(v) -> bool:
    return isinstance(v, _PRIMITIVE) or (isinstance(v, tuple) and all(_primitive(x) for x in v))


def settings(ref):
    """Every primitive setting on the referee, and one level into each desk
    and engine it holds (hazards.odds, piracy.odds, fog.lag, exchange.vol,
    circuit_breaker.band_pct, ...). A new feature flag lands in here without
    anyone listing it."""
    out = {}
    for k, v in vars(ref).items():
        if k == 'db_path':
            continue
        if _primitive(v):
            out[k] = v
        elif hasattr(v, '__dict__') and not isinstance(v, type):
            for k2, v2 in vars(v).items():
                if _primitive(v2):
                    out[f'{k}.{k2}'] = v2
    return out


def live_referee(seed=SEED):
    """What the live server runs after a bare new_game with this seed."""
    with mock.patch.dict(os.environ, _clean_env(), clear=True):
        ref = server.build_referee_from_env(':memory:')
    ref.new_game(seed=seed, warmup_rounds=5)
    return ref


class _Spy:
    """Records the keyword arguments build_referee_from_env hands AgoraReferee."""

    def __init__(self):
        self.calls = []
        self.real = server.AgoraReferee

    def __call__(self, *args, **kwargs):
        self.calls.append(dict(kwargs, db_path=None))
        return self.real(*args, **kwargs)


# An environment that would turn features off if anything read it.
HOSTILE_ENV = {'AGORA_UPGRADES': '0', 'AGORA_PIRACY': '0', 'AGORA_CONTRACTS': '0', 'AGORA_FOG': '0',
               'AGORA_PEER_TRADES': '0', 'AGORA_CORPORATE': '0', 'AGORA_HAZARDS': '0', 'AGORA_IDLE_FEE': '0',
               'AGORA_EVENTS': '0', 'AGORA_ORDER_FLOW': '0'}


class TestSimulatorPlaysTheLiveGame(unittest.TestCase):
    def _live_and_sim_kwargs(self, **run_kw):
        spy = _Spy()
        with mock.patch.object(server, 'AgoraReferee', side_effect=spy):
            with mock.patch.dict(os.environ, _clean_env(), clear=True):
                server.build_referee_from_env(':memory:')
            live = spy.calls.pop()
            with mock.patch.dict(os.environ, HOSTILE_ENV):
                sim.run('privateer_vs_haulers', 'flat', seed=SEED, rounds=2, **run_kw)
        return live, spy.calls

    def test_sim_builds_its_referee_with_the_live_factory_and_settings(self):
        live, sim_calls = self._live_and_sim_kwargs()
        self.assertEqual(len(sim_calls), 1, "the simulator must build its referee with build_referee_from_env, once")
        for flag, value in live.items():
            with self.subTest(flag=flag):
                self.assertIn(flag, sim_calls[0], f"build_referee_from_env sets {flag}; the simulator does not")
                self.assertEqual(sim_calls[0][flag], value, f"{flag} is {value!r} live but {sim_calls[0][flag]!r} in the sim")
        self.assertEqual(set(sim_calls[0]), set(live))

    def test_every_live_feature_is_on_in_the_sim_referee(self):
        want = settings(live_referee())
        got = settings(sim.start_game(SEED))
        for key, value in want.items():
            with self.subTest(setting=key):
                self.assertEqual(got.get(key, '<missing>'), value, f"live {key} = {value!r}, sim has {got.get(key)!r}")
        self.assertEqual(set(got), set(want))
        # The ones that matter most, spelled out, so a failure reads plainly.
        ref = sim.start_game(SEED)
        for on in ('depots_enabled', 'peer_trades', 'contracts_enabled', 'corporate_enabled', 'upgrades_enabled',
                   'events_enabled'):
            self.assertTrue(getattr(ref, on), on)
        self.assertIsNotNone(ref.fog)
        self.assertIsNotNone(ref.hazards.odds)
        self.assertTrue(ref.piracy.enabled)
        self.assertTrue(ref.order_flow.enabled)
        self.assertGreater(ref.exchange_shares, 0)
        self.assertGreater(ref.idle_fee, 0)
        self.assertGreater(ref.rival_shares, 0)

    def test_the_referee_run_plays_is_the_live_one(self):
        # Whatever run() does between building the referee and the first
        # round must not change a setting either: snapshot it the first time
        # the fleets look at the board, before anything has acted.
        seen = []
        real = sim.fleet_views

        def look(ref):
            if not seen:
                seen.append(settings(ref))
            return real(ref)
        with mock.patch.object(sim, 'fleet_views', side_effect=look), mock.patch.dict(os.environ, HOSTILE_ENV):
            sim.run('privateer_vs_haulers', 'flat', seed=SEED, rounds=1)
        self.assertEqual(seen[0], settings(live_referee()))

    def test_sim_ignores_the_callers_environment(self):
        with mock.patch.dict(os.environ, HOSTILE_ENV):
            got = settings(sim.start_game(SEED))
        self.assertEqual(got, settings(live_referee()))

    def test_fleets_start_where_the_live_game_puts_them(self):
        ref = sim.start_game(SEED)
        live = live_referee()
        for a in sim.FLEETS:
            self.assertEqual(ref.get_vessel_location(a)['station_id'], live.get_vessel_location(a)['station_id'])
        self.assertEqual({a: ref.get_vessel_location(a)['station_id'] for a in sim.FLEETS},
                         {'zero': 'earth', 'amos': 'ceres', 'marvin': 'mars', 'aerial': 'luna'})

    # ---- the checks above have teeth

    def test_a_feature_turned_off_in_the_sim_is_caught_by_both_checks(self):
        live, sim_calls = self._live_and_sim_kwargs(overrides={'upgrades': False})
        self.assertNotEqual(sim_calls[0]['upgrades'], live['upgrades'])
        want, got = settings(live_referee()), settings(sim.start_game(SEED, {'upgrades': False}))
        self.assertNotEqual(want, got)
        self.assertEqual({k for k in want if want[k] != got.get(k)}, {'upgrades_enabled'})

    def test_a_changed_constant_is_caught(self):
        want = settings(live_referee())
        got = settings(sim.start_game(SEED, {'hazards': '0.3,0.1'}))
        self.assertEqual({k for k in want if want[k] != got.get(k)}, {'_hazard_odds', 'hazards.odds'})

    def test_a_referee_built_outside_the_factory_is_caught(self):
        spy = _Spy()
        bare = lambda seed, overrides=None: sim.AgoraReferee(depots=True, asymmetric=True)  # noqa: E731
        with mock.patch.object(server, 'AgoraReferee', side_effect=spy), mock.patch.object(sim, 'start_game', bare):
            sim.run('idle4', 'flat', seed=SEED, rounds=1)
        self.assertEqual(spy.calls, [])
        self.assertNotEqual(settings(bare(SEED)), settings(live_referee()))


class TestNoRuleCopiesLeft(unittest.TestCase):
    def test_sim_rule_classes_are_gone(self):
        for name in ('ContractBoard', 'Corporate', 'Hazards', 'Piracy', 'Fog', 'PeerDesk', 'charge_docking_fees'):
            self.assertFalse(hasattr(sim, name), f"tools/economy_sim.py still defines its own {name}")


if __name__ == '__main__':
    unittest.main()
