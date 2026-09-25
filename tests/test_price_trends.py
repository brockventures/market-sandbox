"""
tests/test_price_trends.py - Test price trending on news, contracts, inventory, and momentum (#124).
"""
import random
import unittest

from agora.exchange import EquityExchange, DEFAULT_VOL
from agora.referee import AgoraReferee
from agora.spatial import StationPriceEngine, BASE_PRICES, STATIONS, COMMODITIES


class TestGoodsTrending(unittest.TestCase):
    def test_goods_momentum_continuation(self):
        """A shock in round 1 produces a momentum continuation into round 2 before theta mean-reverts."""
        # Clean engine without noise (vol=0) to isolate deterministic mechanics
        engine = StationPriceEngine(seed=42, theta=0.15, vol=0.0, momentum_factor=0.30)
        st, comm = "ceres", "FRAG"
        base = BASE_PRICES[st][comm]

        # Round 1: inject manual spot increase
        engine.spots[st][comm] = base + 4.0
        # Advance to round 2 without shock: delta_prev is +4.0
        # Momentum pull = 0.30 * 4.0 = +1.20
        # Mean pull = 0.15 * -4.0 = -0.60
        # Net change = +0.60 -> spot should be base + 4.60
        prices_r2 = engine.step_round(2)
        spot_r2 = engine.spots[st][comm]
        self.assertAlmostEqual(spot_r2, base + 4.6, places=2)
        self.assertGreater(spot_r2, base + 4.0)  # Momentum overcame mean pull on round 2

        # Round 3: delta_prev is (24.8 - 24.2) = +0.60
        # Momentum pull = 0.30 * 0.60 = +0.18
        # Mean pull = 0.15 * (20.2 - 24.8) = -0.69
        # Net change = -0.51 -> spot begins mean reverting
        prices_r3 = engine.step_round(3)
        spot_r3 = engine.spots[st][comm]
        self.assertLess(spot_r3, spot_r2)

    def test_goods_depot_inventory_impact(self):
        """Depleted inventory creates scarcity price surges; surplus inventory depresses spot prices."""
        st, comm = "earth", "FRAG"
        base = BASE_PRICES[st][comm]

        engine_scarce = StationPriceEngine(seed=42, vol=0.0, inventory_sensitivity=0.10)
        engine_surplus = StationPriceEngine(seed=42, vol=0.0, inventory_sensitivity=0.10)

        # Scarce: depot shelf only holds 200 units (target 2000) -> ratio = (2000 - 200)/2000 = +0.9
        engine_scarce.step_round(1, depot_inventory={(st, comm): 200.0})
        spot_scarce = engine_scarce.spots[st][comm]

        # Surplus: depot shelf holds 3800 units (target 2000) -> ratio = (2000 - 3800)/2000 = -0.9
        engine_surplus.step_round(1, depot_inventory={(st, comm): 3800.0})
        spot_surplus = engine_surplus.spots[st][comm]

        self.assertGreater(spot_scarce, base)
        self.assertLess(spot_surplus, base)
        self.assertGreater(spot_scarce, spot_surplus)

    def test_goods_delivery_and_contract_demand_flows(self):
        """Deliveries add supply (lower spot), contract demand adds buying pressure (raise spot)."""
        st, comm = "mars", "ORE"
        base = BASE_PRICES[st][comm]

        engine_delivery = StationPriceEngine(seed=42, vol=0.0, delivery_scale=1000.0, flow_sensitivity=1.0)
        engine_demand = StationPriceEngine(seed=42, vol=0.0, delivery_scale=1000.0, flow_sensitivity=1.0)

        # 500 units delivered
        engine_delivery.step_round(1, deliveries={(st, comm): 500})
        spot_delivered = engine_delivery.spots[st][comm]

        # 500 units demanded by open contracts
        engine_demand.step_round(1, contract_demands={(st, comm): 500})
        spot_demanded = engine_demand.spots[st][comm]

        self.assertLess(spot_delivered, base)
        self.assertGreater(spot_demanded, base)


class TestStockTrendingAndNews(unittest.TestCase):
    def test_stock_momentum(self):
        """Stock price with momentum continues upward or downward trend across refresh rounds."""
        ref = AgoraReferee(rival_shares=100, exchange_shares=100, exchange_vol=0.0, exchange_momentum=0.30)
        ref.new_game(seed=7, warmup_rounds=2, rival_shares=100, exchange_shares=100)

        # Prime reference price upward
        sym = "EQ_AMOS"
        p0 = ref.exchange.price[sym]
        ref.exchange.price[sym] = p0 + 10.0
        ref.exchange.prev_price[sym] = p0  # delta_prev = +10.0

        # Step a round: momentum carries price forward
        ref.step_round()
        p1 = ref.exchange.price[sym]
        # p1 should incorporate momentum_nudge = 0.30 * 10.0 = +3.0
        self.assertGreater(p1, p0 + 5.0)

    def test_stock_contract_fulfillment_shock(self):
        """Contract fulfillment event provides positive stock jump (+3%)."""
        ref = AgoraReferee(rival_shares=100, exchange_shares=100, events=True, extended_shocks=True, contracts=True)
        ref.new_game(seed=7, warmup_rounds=2, rival_shares=100, exchange_shares=100)

        sym = "EQ_AMOS"
        p_before = ref.exchange.price[sym]

        with ref.lock, ref.conn:
            ref.events.record_locked("contract_fulfillment", "public", actor="amos", detail="Delivered shipment")

        p_after = ref.exchange.price[sym]
        self.assertAlmostEqual(p_after, p_before * 1.03, places=2)
        shocks = [s for s in ref.exchange.shocks if s["kind"] == "contract_fulfillment"]
        self.assertEqual(len(shocks), 1)
        self.assertEqual(shocks[0]["pct"], 0.03)

    def test_stock_distress_beacon_shock(self):
        """Distress beacon broadcast provides negative stock jump (-5%)."""
        ref = AgoraReferee(rival_shares=100, exchange_shares=100, events=True, extended_shocks=True)
        ref.new_game(seed=7, warmup_rounds=2, rival_shares=100, exchange_shares=100)

        sym = "EQ_ZERO"
        p_before = ref.exchange.price[sym]

        with ref.lock, ref.conn:
            ref.events.record_locked("distress_beacon", "public", actor="zero", detail="Stranded in asteroid belt")

        p_after = ref.exchange.price[sym]
        self.assertAlmostEqual(p_after, p_before * 0.95, places=2)
        shocks = [s for s in ref.exchange.shocks if s["kind"] == "distress_beacon"]
        self.assertEqual(len(shocks), 1)
        self.assertEqual(shocks[0]["pct"], -0.05)

    def test_stock_debt_default_shock(self):
        """Corporate loan default provides negative stock jump (-6%)."""
        ref = AgoraReferee(rival_shares=100, exchange_shares=100, events=True, extended_shocks=True)
        ref.new_game(seed=7, warmup_rounds=2, rival_shares=100, exchange_shares=100)

        sym = "EQ_MARV"
        p_before = ref.exchange.price[sym]

        with ref.lock, ref.conn:
            ref.events.record_locked("debt_default", "public", actor="marvin", detail="Defaulted on corporate loan")

        p_after = ref.exchange.price[sym]
        self.assertAlmostEqual(p_after, p_before * 0.94, places=2)


class TestTrendFollowingSimulator(unittest.TestCase):
    def test_trend_following_beats_random_trading(self):
        """
        Simulator test: A trend-following agent (reads price velocity, momentum, and GalNet news)
        beats a random trader over a multi-round simulation without making prices trivially predictable.
        """
        from agora.galnet import GalNetEngine

        engine = StationPriceEngine(seed=42, theta=0.10, vol=0.3, momentum_factor=0.30)
        galnet = GalNetEngine(seed=42)
        st, comm = "earth", "FRAG"

        # Force news shock on round 1 (earth FRAG surge)
        galnet.force_shock(1, template_idx=0)

        cash_trend, cargo_trend = 1000.0, 0
        cash_rand, cargo_rand = 1000.0, 0
        rng_rand = random.Random(123)

        for r in range(1, 15):
            prices = engine.step_round(r, galnet_engine=galnet)
            galnet.step_round(r)
            spot = engine.get_station_price(st, comm)
            entry = next(p for p in prices if p.station_id == st and p.commodity == comm)

            # Trend trader strategy: reads momentum + news drift signal
            trend_signal = entry.momentum + entry.drift_bias * 2.0
            if trend_signal > 0.05 and cash_trend >= spot * 10:
                cash_trend -= spot * 10
                cargo_trend += 10
            elif trend_signal < -0.05 and cargo_trend >= 10:
                cash_trend += spot * 10
                cargo_trend -= 10

            # Random trader strategy:
            action = rng_rand.choice(["buy", "sell", "hold"])
            if action == "buy" and cash_rand >= spot * 10:
                cash_rand -= spot * 10
                cargo_rand += 10
            elif action == "sell" and cargo_rand >= 10:
                cash_rand += spot * 10
                cargo_rand -= 10

        final_spot = engine.get_station_price(st, comm)
        nav_trend = cash_trend + cargo_trend * final_spot
        nav_rand = cash_rand + cargo_rand * final_spot

        # Trend-following trader outperforms random trader
        self.assertGreater(nav_trend, nav_rand)
        # Prices stayed in a realistic bounded band
        self.assertGreater(final_spot, 5.0)
        self.assertLess(final_spot, 50.0)


if __name__ == "__main__":
    unittest.main()
