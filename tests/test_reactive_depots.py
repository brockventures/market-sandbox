"""
tests/test_reactive_depots.py - the reactive depot model (depot_model="reactive").

Settings are passed explicitly so the tests never depend on AGORA_DEPOT_MODEL
or AGORA_BAND_PCT in the environment.
"""

import os
import unittest
from unittest import mock

from agora.referee import AgoraReferee, REACTIVE_TARGET, REACTIVE_SHELF_SKEW, BASE_PRICES


def make(**kw):
    kw.setdefault("depot_model", "reactive")
    kw.setdefault("band_pct", 0.25)
    return AgoraReferee(depots=True, asymmetric=True, **kw)


def depot_quote(ref, st, comm, side):
    book = ref.books[st][comm]
    orders = [o for o in (book.bids if side == "bid" else book.asks) if o.agent_id == f"depot_{st}"]
    return orders[0] if orders else None


def order(ref, oid, agent, side, qty, price, comm, st):
    return ref.submit_envelope({"v": 1, "kind": "order", "payload": {
        "order_id": oid, "agent_id": agent, "side": side, "qty": qty, "limit_price": price,
        "instrument": comm, "station_id": st, "seq_seen": ref.current_seq}})


class TestReactiveDepots(unittest.TestCase):
    def test_static_is_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGORA_DEPOT_MODEL", None)
            self.assertEqual(AgoraReferee(depots=True).depot_model, "static")

    def test_env_selects_model_and_band(self):
        with mock.patch.dict(os.environ, {"AGORA_DEPOT_MODEL": "reactive", "AGORA_BAND_PCT": "0.25"}):
            ref = AgoraReferee(depots=True)
            self.assertEqual(ref.depot_model, "reactive")
            self.assertEqual(ref.circuit_breaker.band_pct, 0.25)

    def test_new_game_can_switch_model_and_band(self):
        ref = AgoraReferee(depots=True, depot_model="static", band_pct=0.10)
        ref.new_game(seed=1, depots=True, depot_model="reactive", band_pct=0.25)
        self.assertEqual(ref.depot_model, "reactive")
        self.assertEqual(ref.circuit_breaker.band_pct, 0.25)
        self.assertTrue(ref._reactive)

    def test_band_survives_new_game(self):
        ref = make()
        ref.new_game(seed=1, depots=True, asymmetric=True)
        self.assertEqual(ref.circuit_breaker.band_pct, 0.25)

    def test_ask_rises_as_shelf_empties(self):
        # Same round, same spot price: only the shelf changes.
        ref = make(band_pct=0.9)  # wide band so the skew is not clamped
        before = depot_quote(ref, "ceres", "ORE", "ask")
        shelf0 = ref._reactive["shelf"][("ceres", "ORE")]
        res = order(ref, "sweep", "amos", "bid", 500, before.limit_price, "ORE", "ceres")
        self.assertEqual(res.get("kind"), "market_tick", res)
        with ref.lock, ref.conn:
            ref._refresh_reactive_depots_locked()   # same round: no restock
        # The purchase is read from the ledger and taken off the shelf.
        self.assertEqual(ref._reactive["shelf"][("ceres", "ORE")], shelf0 - 500)
        after = depot_quote(ref, "ceres", "ORE", "ask")
        self.assertGreater(after.limit_price, before.limit_price)

    def test_bid_falls_as_hold_fills(self):
        ref = make(band_pct=0.9)
        ref._reactive["hold"][("earth", "ORE")] = REACTIVE_TARGET  # a full hold
        with ref.lock, ref.conn:
            ref._refresh_reactive_depots_locked()
        full = depot_quote(ref, "earth", "ORE", "bid")
        fresh = make(band_pct=0.9)
        empty = depot_quote(fresh, "earth", "ORE", "bid")
        self.assertIsNone(full)  # full hold: the depot stops buying
        self.assertIsNotNone(empty)
        ref._reactive["hold"][("earth", "ORE")] = REACTIVE_TARGET // 2
        with ref.lock, ref.conn:
            ref._refresh_reactive_depots_locked()
        self.assertLess(depot_quote(ref, "earth", "ORE", "bid").limit_price, empty.limit_price)

    def test_shelf_restocks_each_round(self):
        ref = make()
        ref._reactive["shelf"][("ceres", "ORE")] = 0
        ref.step_round()
        self.assertGreater(ref._reactive["shelf"][("ceres", "ORE")], 0)

    def test_new_game_resets_state(self):
        ref = make()
        ref._reactive["shelf"][("ceres", "ORE")] = 0
        ref.new_game(seed=2, depots=True, asymmetric=True, depot_model="reactive")
        # Reset to half capacity; the first round's production may already be on it.
        self.assertGreaterEqual(ref._reactive["shelf"][("ceres", "ORE")], REACTIVE_TARGET // 2)

    def test_quotes_held_inside_band(self):
        ref = make(band_pct=0.10)
        for _ in range(3):
            ref.step_round()
        for st in ("earth", "ceres"):
            for comm in ("ORE", "FOOD", "FUEL"):
                b = ref.circuit_breaker.get_bands(st, comm)
                for side in ("bid", "ask"):
                    q = depot_quote(ref, st, comm, side)
                    if q is not None and b["upper_limit"] - b["lower_limit"] > 1:
                        self.assertGreaterEqual(q.limit_price, b["lower_limit"] - 1)
                        self.assertLessEqual(q.limit_price, b["upper_limit"] + 1)

    def test_shelf_skew_damps_price_inflation(self):
        # #188: When shelf is depleted to minimum (100 units = 5% of REACTIVE_TARGET),
        # shelf_ratio is 20.0. With REACTIVE_SHELF_SKEW = 0.20, multiplier is 20^0.20 ~= 1.82,
        # rather than 20^0.50 ~= 4.47 (which clamped at 3.0x spot).
        ref = make(band_pct=0.95)
        ref._reactive["shelf"][("ceres", "ORE")] = int(REACTIVE_TARGET * 0.05)
        with ref.lock, ref.conn:
            ref._refresh_reactive_depots_locked()
        q = depot_quote(ref, "ceres", "ORE", "ask")
        spot = BASE_PRICES["ceres"]["ORE"]
        # Ask should be around spot * 1.03 * 1.82 ~= 1.875 * spot (approx 23 CR for 12.4 spot),
        # strictly less than 2.2 * spot (approx 27 CR), whereas old skew yielded ~38 CR (3.0x).
        self.assertLess(q.limit_price, int(round(spot * 2.2)))
        self.assertGreater(q.limit_price, int(round(spot * 1.5)))

    def test_shelf_skew_env_and_kwarg(self):
        ref = make(shelf_skew=0.35)
        self.assertEqual(ref.shelf_skew, 0.35)
        ref.new_game(seed=3, depots=True, asymmetric=True, depot_model="reactive", shelf_skew=0.15)
        self.assertEqual(ref.shelf_skew, 0.15)
        with mock.patch.dict(os.environ, {"AGORA_SHELF_SKEW": "0.28"}):
            ref_env = AgoraReferee(depots=True, depot_model="reactive")
            self.assertEqual(ref_env.shelf_skew, 0.28)

    def test_shelf_skew_summary_reporting(self):
        ref = make(shelf_skew=0.22)
        summary = ref.get_depot_summary()
        self.assertEqual(summary.get("shelf_skew"), 0.22)

    def test_long_run_stays_solvent_and_balanced(self):
        ref = make()
        for i in range(150):
            if i % 10 == 0:
                a = depot_quote(ref, "ceres", "ORE", "ask")
                if a:
                    order(ref, f"b{i}", "amos", "bid", min(200, a.remaining_qty), a.limit_price, "ORE", "ceres")
            ref.step_round()
        for st in ("earth", "luna", "mars", "ceres"):
            self.assertGreaterEqual(ref.get_balance(f"depot_{st}", "CR"), 0)
        valid, errors = ref.verify_ledger_invariants()
        self.assertTrue(valid, errors)


if __name__ == "__main__":
    unittest.main()
