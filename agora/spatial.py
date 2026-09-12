"""
agora.spatial - Sol System Multi-Station Spatial Economy & Orbital Route Physics.

Defines the 4-station Sol topology (Earth, Luna, Mars, Ceres), transit latency,
hydrogen propellant burn curves, and mean-reverting price surfaces for Task #20 / Task #53.
"""

import math
import random
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple


STATIONS = ["earth", "luna", "mars", "ceres"]
COMMODITIES = ["FRAG", "FUEL"]

# Base fundamental mean valuations (equilibrium price surface)
BASE_PRICES = {
    "earth": {"FRAG": 10.0, "FUEL": 8.0},    # Scrap consumer, high fuel supply
    "luna":  {"FRAG": 12.0, "FUEL": 16.0},   # Secondary yard, He-3 collection
    "mars":  {"FRAG": 16.0, "FUEL": 14.0},   # Heavy foundries, balanced fuel
    "ceres": {"FRAG": 22.0, "FUEL": 26.0},   # Raw scrap source, remote belt fuel depot
}

# Orbital transit distances, discrete rounds, and fuel burn requirements
ROUTES = {
    ("earth", "luna"):  {"rounds": 1, "fuel": 5},
    ("luna", "earth"):  {"rounds": 1, "fuel": 5},
    ("earth", "mars"):  {"rounds": 2, "fuel": 15},
    ("mars", "earth"):  {"rounds": 2, "fuel": 15},
    ("luna", "mars"):   {"rounds": 2, "fuel": 15},
    ("mars", "luna"):   {"rounds": 2, "fuel": 15},
    ("mars", "ceres"):  {"rounds": 2, "fuel": 20},
    ("ceres", "mars"):  {"rounds": 2, "fuel": 20},
    ("earth", "ceres"): {"rounds": 3, "fuel": 30},
    ("ceres", "earth"): {"rounds": 3, "fuel": 30},
    ("luna", "ceres"):  {"rounds": 3, "fuel": 30},
    ("ceres", "luna"):  {"rounds": 3, "fuel": 30},
}


def get_route(origin: str, destination: str) -> Optional[Dict[str, int]]:
    """Returns route specifications (rounds, fuel) between two stations."""
    orig = origin.lower().strip()
    dest = destination.lower().strip()
    if orig == dest:
        return {"rounds": 0, "fuel": 0}
    return ROUTES.get((orig, dest))


@dataclass
class StationSpotPrice:
    station_id: str
    commodity: str
    round: int
    base_price: float
    drift_bias: float
    spot_price: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StationPriceEngine:
    """
    Mean-reverting random walk engine generating price surfaces for Sol stations.
    Theta tuned to 0.15 (half-life ~5 rounds) to preserve arbitrage viability over 2-3 round transits.
    Additive drift_bias injected deterministically from GalNet breaking news wire.
    """

    def __init__(self, seed: int = 42, theta: float = 0.15, vol: float = 0.8):
        self.seed = seed
        self.rng = random.Random(seed)
        self.theta = theta
        self.vol = vol
        self.current_round = 0
        self.spots: Dict[str, Dict[str, float]] = {
            st: {comm: BASE_PRICES[st][comm] for comm in COMMODITIES}
            for st in STATIONS
        }
        self.history: List[StationSpotPrice] = []

    def step_round(self, round_num: int, galnet_engine: Optional[Any] = None) -> List[StationSpotPrice]:
        """
        Advances the price surface to round_num.
        Computes new spot prices incorporating mean reversion, GalNet drift bias, and Gaussian noise.
        """
        self.current_round = round_num
        new_prices = []

        for st in STATIONS:
            for comm in COMMODITIES:
                base = BASE_PRICES[st][comm]
                old_spot = self.spots[st][comm]
                drift_bias = 0.0
                if galnet_engine:
                    drift_bias = galnet_engine.get_active_drift(st, comm)

                # Ornstein-Uhlenbeck discrete step + news shock drift
                mean_pull = self.theta * (base - old_spot)
                shock_nudge = drift_bias * (base * 0.5)
                noise = self.rng.gauss(0, self.vol)

                new_spot = max(1.0, round(old_spot + mean_pull + shock_nudge + noise, 2))
                self.spots[st][comm] = new_spot

                entry = StationSpotPrice(
                    station_id=st,
                    commodity=comm,
                    round=round_num,
                    base_price=base,
                    drift_bias=drift_bias,
                    spot_price=new_spot
                )
                self.history.append(entry)
                new_prices.append(entry)

        return new_prices

    def get_prices(self) -> Dict[str, Dict[str, float]]:
        """Returns current spot prices across all stations and commodities."""
        return {st: dict(commodities) for st, commodities in self.spots.items()}

    def get_station_price(self, station_id: str, commodity: str = "FRAG") -> float:
        st = station_id.lower().strip()
        comm = commodity.upper().strip()
        return self.spots.get(st, {}).get(comm, 10.0)
