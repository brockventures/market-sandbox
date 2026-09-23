"""
tests/test_economy_sim.py - tools/economy_sim.py runs end to end, in-process,
fast enough for CI, and its scripted fleets actually trade.
"""

import ast
import inspect
import os
import textwrap
import unittest
from unittest import mock

import agora.server as server_mod
from tools.economy_sim import (
    Corporate as PrototypeCorporate,
    ContractBoard as PrototypeContractBoard,
    Fog as PrototypeFog,
    Hazards as PrototypeHazards,
    Piracy as PrototypePiracy,
    PeerDesk as PrototypePeerDesk,
    build_live_referee,
    run,
)


def _agora_referee_kwargs(build_fn) -> list:
    """The keyword arguments `build_fn` (agora.server.build_referee_from_env)
    passes into AgoraReferee(...), read straight from its source so a new
    AGORA_* feature added there is picked up automatically -- no one has to
    remember to update a hand-written list here."""
    src = textwrap.dedent(inspect.getsource(build_fn))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AgoraReferee":
            return [kw.arg for kw in node.keywords if kw.arg]
    raise AssertionError(f"no AgoraReferee(...) call found in {build_fn.__qualname__}'s source")


# kwarg -> a reader of the *effective* setting on a constructed referee.
# band_pct is read off the circuit breaker (what --live's report shows and
# what the parity guard below actually cares about matching); asymmetric
# is read off the fleet roster, because a bare new_game() sets
# ref.asymmetric_enabled to None (it only rewrites home stations when
# truthy) even though the roster it inherited from construction is
# unaffected -- comparing that attribute directly would be a false
# mismatch between two referees that behave identically.
ATTR_FOR_KWARG = {
    "db_path": lambda r: None,  # not a feature flag; excluded from the parity assertions below
    "depots": lambda r: r.depots_enabled,
    "asymmetric": lambda r: len({row[0] for row in r.conn.execute("SELECT home_station FROM fleet_roster")}) > 1,
    "depot_model": lambda r: r.depot_model,
    "band_pct": lambda r: r.circuit_breaker.band_pct,
    "peer_trades": lambda r: r.peer_trades,
    "fog": lambda r: (r.fog.lag, r.fog.noise) if r.fog else None,
    "idle_fee": lambda r: r.idle_fee,
    "rival_shares": lambda r: r.rival_shares,
    "exchange_shares": lambda r: r.exchange_shares,
    "exchange_vol": lambda r: r.exchange.vol,
    "contracts": lambda r: r.contracts_enabled,
    "hazards": lambda r: r.hazards.odds,
    "corporate": lambda r: r.corporate_enabled,
    "upgrades": lambda r: r.upgrades_enabled,
    "piracy": lambda r: r.piracy.odds,
}


class TestLiveRefereeParity(unittest.TestCase):
    """#economy_sim --live: the sim's referee must be built from the same
    feature flags as the live server's, for every flag the server has --
    not just the ones this test's author happened to remember."""

    def test_every_build_referee_from_env_kwarg_has_a_parity_check(self):
        # This is the one that fails CI when a new AGORA_* feature is added
        # to build_referee_from_env without matching sim support: a kwarg
        # with no entry in ATTR_FOR_KWARG has nothing checking it below.
        kwargs = _agora_referee_kwargs(server_mod.build_referee_from_env)
        self.assertTrue(kwargs, "build_referee_from_env's AgoraReferee(...) call had no keyword arguments")
        missing = [k for k in kwargs if k not in ATTR_FOR_KWARG]
        self.assertEqual(missing, [], f"add a parity check for {missing} in ATTR_FOR_KWARG (tests/test_economy_sim.py)")

    def test_live_referee_settings_match_build_referee_from_env(self):
        kwargs = [k for k in _agora_referee_kwargs(server_mod.build_referee_from_env) if k != "db_path"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGORA_")}
        with mock.patch.dict(os.environ, env, clear=True):
            want = server_mod.build_referee_from_env(db_path=":memory:")
            got = build_live_referee(seed=1)
        for kw in kwargs:
            self.assertEqual(ATTR_FOR_KWARG[kw](got), ATTR_FOR_KWARG[kw](want), kw)

    def test_env_overrides_propagate_to_both(self):
        with mock.patch.dict(os.environ, {"AGORA_PIRACY": "0", "AGORA_FOG": "0", "AGORA_CORPORATE": "0"}):
            want = server_mod.build_referee_from_env(db_path=":memory:")
            got = build_live_referee(seed=1)
        self.assertIsNone(want.fog)
        self.assertIsNone(got.fog)
        self.assertFalse(want.corporate_enabled)
        self.assertFalse(got.corporate_enabled)
        self.assertIsNone(want.piracy.odds)
        self.assertIsNone(got.piracy.odds)

    def test_live_run_never_instantiates_a_sim_prototype(self):
        # Requirement 2: in --live, the referee's own rules are the only
        # source. If any of these six classes gets instantiated during a
        # run, that run is simulating the prototype, not the live game.
        prototypes = (PrototypeContractBoard, PrototypeCorporate, PrototypeHazards,
                      PrototypePiracy, PrototypeFog, PrototypePeerDesk)
        originals = [cls.__init__ for cls in prototypes]

        def _boom(self, *a, **kw):
            raise AssertionError(f"{type(self).__name__} instantiated during a --live run")

        for cls in prototypes:
            cls.__init__ = _boom
        try:
            r = run("mixed", "flat", seed=1, rounds=30, live=True)
        finally:
            for cls, orig in zip(prototypes, originals):
                cls.__init__ = orig
        self.assertIsNone(r["first_invariant_failure"])


class TestLiveMode(unittest.TestCase):
    """#economy_sim --live over the standard scenarios."""

    def test_mixed_is_reproducible_and_clean(self):
        a = run("mixed", "flat", seed=2, rounds=60, live=True)
        b = run("mixed", "flat", seed=2, rounds=60, live=True)
        self.assertIsNone(a["first_invariant_failure"])
        self.assertEqual({k: v["pnl"] for k, v in a["fleets"].items()},
                         {k: v["pnl"] for k, v in b["fleets"].items()})
        self.assertTrue(a["live"])

    def test_haulers4_trades_and_stays_balanced(self):
        r = run("haulers4", "flat", seed=1, rounds=60, live=True)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["transits"], 0)

    def test_novice_vs_haulers_uses_the_live_contract_desk(self):
        r = run("novice_vs_haulers", "flat", seed=1, rounds=80, live=True)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertIsNotNone(r["contracts"])
        self.assertGreater(r["contracts"]["posted"], 0)

    def test_idle4_never_charges_the_idle_fee_when_nobody_acts(self):
        # referee.py _charge_idle_fees_locked: idle_fee only fires in a
        # round where some fleet acted ("so an empty server never bleeds
        # everyone's cash"). All four fleets here are idlers -- and, since
        # idlers are excluded from LivePeerMatcher (see its docstring),
        # nothing calls ref.mark_active() -- so the fee never fires and
        # pnl is exactly 0. This is the live idle_fee rule, not a --dock-fee
        # gap: --prototype's --dock-fee (a different, simpler mechanic)
        # charges unconditionally, which is why the two disagree here.
        r = run("idle4", "flat", seed=1, rounds=50, live=True)
        self.assertIsNone(r["first_invariant_failure"])
        for f in r["fleets"].values():
            self.assertEqual(f["pnl"], 0)

    def test_mixed_idler_pays_the_live_idle_fee_exactly(self):
        # aerial is the idler in "mixed"; the other three fleets act every
        # round, so aerial's idle fee fires every round too: 200 * 10 CR.
        r = run("mixed", "flat", seed=1, rounds=200, live=True)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertEqual(r["fleets"]["aerial"]["pnl"], -2000)

    def test_live_rejects_mixing_prototype_flags(self):
        with self.assertRaises(TypeError):
            run("mixed", "flat", seed=1, rounds=10, live=True, contracts=True)
        with self.assertRaises(TypeError):
            run("mixed", "planet", seed=1, rounds=10, live=True)


class TestEconomySim(unittest.TestCase):
    def test_short_mixed_run(self):
        r = run("mixed", "flat", seed=2, rounds=30, mode="tolerant", check_every=10)
        self.assertEqual(r["rounds"], 30)
        self.assertGreater(r["transits"], 0)
        self.assertGreater(r["fills"]["fleet_vs_depot"], 0)
        self.assertEqual(r["fleets"]["aerial"]["pnl"], 0)  # idler never trades
        self.assertIsNone(r["first_invariant_failure"])

    def test_reactive_depots_run_clean(self):
        r = run("haulers4", "flat", seed=1, rounds=40, mode="tolerant", check_every=10,
                depot_model="reactive", band_pct=0.25)
        self.assertEqual(r["depot_model"], "reactive")
        self.assertGreater(r["transits"], 0)
        self.assertIsNone(r["first_negative_depot_round"])
        self.assertIsNone(r["first_invariant_failure"])

    def test_contracts_are_delivered_and_balanced(self):
        r = run("market", "flat", seed=1, rounds=60, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, contracts=True)
        self.assertGreater(r["contracts"]["posted"], 0)
        self.assertGreater(r["contracts"]["units_delivered"], 0)
        self.assertIsNone(r["first_invariant_failure"])

    def test_novices_trade_badly_but_keep_the_ledger_clean(self):
        r = run("novice4", "flat", seed=1, rounds=60, check_every=20,
                depot_model="reactive", band_pct=0.25)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["bad_orders"], 0)
        self.assertGreater(r["transits"], 0)
        self.assertTrue(any(f["pnl"] > 0 for f in r["fleets"].values()))

    def test_owned_contracts_change_hands_and_balance(self):
        r = run("haulers4", "flat", seed=1, rounds=60, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, owned_contracts=True, fog=(3, 0.15))
        self.assertIsNone(r["first_invariant_failure"])
        self.assertTrue(r["contracts"]["owned"])
        self.assertGreater(r["contracts"]["transfers"], 0)

    def test_peer_desk_keeps_ledger_balanced(self):
        r = run("novice_vs_haulers", "flat", seed=1, rounds=80, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, peer=True, fog=(3, 0.15))
        self.assertIsNone(r["first_invariant_failure"])
        self.assertIn("trades", r["peer"])

    def test_corporate_risk_runs_clean_and_is_reproducible(self):
        a = run("novice_vs_haulers", "flat", seed=3, rounds=60, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, corporate=True)
        b = run("novice_vs_haulers", "flat", seed=3, rounds=60, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, corporate=True)
        self.assertIsNone(a["first_invariant_failure"])
        self.assertGreater(a["corporate"]["claims"], 0)
        self.assertEqual({k: v["leaderboard_nw"] for k, v in a["fleets"].items()},
                         {k: v["leaderboard_nw"] for k, v in b["fleets"].items()})

    def test_corporate_non_deliverers_never_take_contracts(self):
        # An idler or maker never delivers, so it must never claim or buy a
        # contract: before this fix both did, ate the penalties and were
        # bankrupted or taken over in almost every mixed run.
        r = run("mixed", "flat", seed=1, rounds=150, mode="tolerant", check_every=50,
                depot_model="reactive", band_pct=0.25, corporate=True)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["corporate"]["claims"], 0)
        self.assertNotIn("marvin", r["corporate"]["debt_end"])
        self.assertNotIn("aerial", r["corporate"]["debt_end"])
        self.assertEqual([b for b in r["corporate"]["bankruptcies"] if b["fleet"] in ("marvin", "aerial")], [])
        self.assertEqual(r["fleets"]["aerial"]["pnl"], 0)

    def test_peer_desk_respects_goods_committed_to_resting_asks(self):
        # All features on, seed 5: the peer desk used to sell ORE that a
        # resting distress ask had committed, and the seller went to -200 ORE.
        r = run("novice_vs_haulers", "flat", seed=5, rounds=160, mode="tolerant", check_every=4,
                depot_model="reactive", band_pct=0.25, peer=True, fog=(3, 0.15), corporate=True)
        self.assertIsNone(r["first_invariant_failure"])

    def test_stock_trader_trades_against_stand_in_liquidity(self):
        r = run("stocks", "flat", seed=1, rounds=120, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, corporate=True, equity_mm=(0.05, 20))
        self.assertIsNone(r["first_invariant_failure"])
        st = r["stocks"]["fleets"]["marvin"]
        self.assertNotEqual(st["stock_cash"], 0)  # it traded
        self.assertEqual(r["corporate"]["claims"] > 0, True)
        self.assertNotIn("marvin", r["corporate"]["debt_end"])  # never takes contracts

    def test_stock_trader_against_referee_exchange(self):
        r = run("stocks", "flat", seed=2, rounds=120, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, corporate=True, exchange=(100, 0.03))
        self.assertIsNone(r["first_invariant_failure"])
        self.assertNotEqual(r["stocks"]["fleets"]["marvin"]["stock_cash"], 0)
        self.assertGreater(r["stocks"]["exchange"]["cr"], 0)

    def test_no_stock_trader_leaves_results_unchanged(self):
        r = run("mixed", "flat", seed=1, rounds=30, mode="tolerant")
        self.assertNotIn("stocks", r)

    def test_daytraders_trade_and_spread_scale_restores(self):
        import agora.spatial as sp
        before = sp.BASE_PRICES["ceres"]["ORE"]
        r = run("daytrade_vs_haulers", "flat", seed=1, rounds=80, mode="tolerant", check_every=20,
                depot_model="reactive", band_pct=0.25, vol=2.4, spread_scale=0.5)
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["day_trades"], 0)
        self.assertEqual(sp.BASE_PRICES["ceres"]["ORE"], before)

    def test_claim_bond_and_hazards_keep_ledger_balanced_and_repeat(self):
        kw = dict(mode="tolerant", check_every=10, depot_model="reactive", band_pct=0.25,
                  corporate=True, bond=0.25, hazards=(0.2, 0.1))
        a = run("novice_vs_haulers", "flat", seed=2, rounds=120, **kw)
        b = run("novice_vs_haulers", "flat", seed=2, rounds=120, **kw)
        self.assertIsNone(a["first_invariant_failure"])
        self.assertGreater(a["hazards"]["delays"] + a["hazards"]["losses"], 0)
        self.assertGreater(a["corporate"].get("bonds_cr", 0), 0)
        self.assertEqual({k: v["pnl"] for k, v in a["fleets"].items()}, {k: v["pnl"] for k, v in b["fleets"].items()})

    def test_piracy_and_privateers_keep_ledger_balanced(self):
        r = run("privateer_vs_haulers", "flat", seed=3, rounds=150, mode="tolerant", check_every=10,
                depot_model="reactive", band_pct=0.25, corporate=True, bond=0.25, piracy=(0.3, 0.1))
        self.assertIsNone(r["first_invariant_failure"])
        self.assertGreater(r["piracy"]["raids"], 0)
        self.assertGreater(r["piracy"]["privateer_contracts"], 0)

    def test_dock_fee_charges_idlers(self):
        r = run("idle4", "flat", seed=1, rounds=20, dock_fee=10)
        for f in r["fleets"].values():
            self.assertEqual(f["pnl"], -200)
        self.assertEqual(r["dock_fees_collected"], 800)
        self.assertIsNone(r["first_invariant_failure"])

    def test_planet_genesis_preserves_value(self):
        r = run("idle4", "planet", seed=1, rounds=1)
        for f in r["fleets"].values():
            self.assertAlmostEqual(f["start"], 33000, delta=20)


if __name__ == "__main__":
    unittest.main()
