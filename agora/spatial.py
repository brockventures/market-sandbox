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

# High-density asteroid belt transit routes
BELT_ROUTES = {
    ("earth", "ceres"), ("ceres", "earth"),
    ("luna", "ceres"),  ("ceres", "luna"),
    ("mars", "ceres"),  ("ceres", "mars"),
}

BELT_TOLL_CR = 25  # Belt Authority toll booth surcharge (CR)
BELT_CARGO_DECAY_RATE = 0.05  # 5% perishable cargo degradation per transit round
PERISHABLE_COMMODITIES = {"ORGANICS", "BIO", "HYDROPONICS"}

# Planetary orbital alignment corridors (Task #25)
ALIGNMENT_WINDOWS = [
    {
        "corridor_id": "earth_mars",
        "name": "Earth-Mars Perihelion Opposition",
        "routes": [("earth", "mars"), ("mars", "earth"), ("luna", "mars"), ("mars", "luna")],
        "period_rounds": 8,
        "window_offsets": [4, 5],  # Active when (round % 8) in [4, 5]
        "transit_reduction_pct": 0.50,  # 2 -> 1 round (50% reduction)
        "fuel_reduction_pct": 0.33,     # 15 -> 10 fuel
        "description": "Synodic opposition opens the Hohmann launch corridor, halving transit time to 1 round."
    },
    {
        "corridor_id": "mars_ceres",
        "name": "Martian-Ceres Belt Opposition",
        "routes": [("mars", "ceres"), ("ceres", "mars")],
        "period_rounds": 10,
        "window_offsets": [5, 6],  # Active when (round % 10) in [5, 6]
        "transit_reduction_pct": 0.50,  # 2 -> 1 round (50% reduction)
        "fuel_reduction_pct": 0.40,     # 20 -> 12 fuel
        "description": "Inner Asteroid Belt orbital alignment provides gravitational assist, halving transit rounds."
    },
    {
        "corridor_id": "earth_ceres",
        "name": "Sol Direct Corridor",
        "routes": [("earth", "ceres"), ("ceres", "earth"), ("luna", "ceres"), ("ceres", "luna")],
        "period_rounds": 12,
        "window_offsets": [6, 7],  # Active when (round % 12) in [6, 7]
        "transit_reduction_pct": 0.33,  # 3 -> 2 rounds
        "fuel_reduction_pct": 0.40,     # 30 -> 18 fuel
        "description": "Deep belt gravitational slingshot corridor cutting transit time and propellant burn."
    }
]


def get_alignment_windows(round_num: int = 0) -> List[Dict[str, Any]]:
    """Returns status and schedules for all orbital alignment windows at round_num."""
    results = []
    for w in ALIGNMENT_WINDOWS:
        period = w["period_rounds"]
        offsets = w["window_offsets"]
        mod_rnd = round_num % period
        is_active = mod_rnd in offsets
        if is_active:
            idx = offsets.index(mod_rnd)
            rounds_remaining = len(offsets) - idx
            rounds_until_next = 0
        else:
            rounds_remaining = 0
            first_offset = offsets[0]
            if mod_rnd < first_offset:
                rounds_until_next = first_offset - mod_rnd
            else:
                rounds_until_next = (period - mod_rnd) + first_offset

        results.append({
            "corridor_id": w["corridor_id"],
            "name": w["name"],
            "routes": [list(r) for r in w["routes"]],
            "period_rounds": period,
            "is_active": is_active,
            "rounds_remaining": rounds_remaining,
            "rounds_until_next": rounds_until_next,
            "transit_reduction_pct": w["transit_reduction_pct"],
            "fuel_reduction_pct": w["fuel_reduction_pct"],
            "description": w["description"]
        })
    return results


def get_active_window_for_route(origin: str, destination: str, round_num: int = 0) -> Optional[Dict[str, Any]]:
    """Checks if an active alignment window applies to the specified route at round_num."""
    orig = origin.lower().strip()
    dest = destination.lower().strip()
    windows = get_alignment_windows(round_num)
    for w in windows:
        if w["is_active"]:
            for r in w["routes"]:
                if r[0] == orig and r[1] == dest:
                    return w
    return None


def get_route(origin: str, destination: str, round_num: int = 0) -> Optional[Dict[str, Any]]:
    """Returns route specifications (rounds, fuel, alignment, toll, decay rate) between two stations."""
    orig = origin.lower().strip()
    dest = destination.lower().strip()
    if orig == dest:
        return {"rounds": 0, "fuel": 0}

    base = ROUTES.get((orig, dest))
    if not base:
        return None

    window = get_active_window_for_route(orig, dest, round_num)
    if window and window["is_active"]:
        rounds = max(1, round(base["rounds"] * (1.0 - window["transit_reduction_pct"])))
        fuel = max(1, int(base["fuel"] * (1.0 - window["fuel_reduction_pct"])))
        is_aligned = True
        window_name = window["name"]
        rounds_remaining = window["rounds_remaining"]
    else:
        rounds = base["rounds"]
        fuel = base["fuel"]
        is_aligned = False
        window_name = None
        rounds_remaining = 0

    is_belt = (orig, dest) in BELT_ROUTES
    toll = BELT_TOLL_CR if is_belt else 0
    decay_rate = BELT_CARGO_DECAY_RATE if is_belt else 0.0

    return {
        "rounds": rounds,
        "fuel": fuel,
        "base_rounds": base["rounds"],
        "base_fuel": base["fuel"],
        "is_aligned": is_aligned,
        "window_name": window_name,
        "rounds_remaining": rounds_remaining,
        "is_belt_route": is_belt,
        "toll": toll,
        "decay_rate": decay_rate
    }


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
