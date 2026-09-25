import os
import math
"""
agora.galnet - GalNet Breaking News Wire & Exogenous Drift Shock Engine.
Generates narrative lore-aligned news dispatches and deterministic drift biases
for the Station Agora multi-station economy (Task #53).
"""

import time
import random
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any


@dataclass
class GalNetNewsEvent:
    id: str
    round: int
    timestamp: float
    station_id: str
    commodity: str
    headline: str
    body: str
    drift_bias: float
    duration_rounds: int

    @property
    def expires_round(self) -> int:
        return self.round + self.duration_rounds

    @property
    def direction(self) -> str:
        if self.drift_bias > 0:
            return "up"
        elif self.drift_bias < 0:
            return "down"
        return "neutral"

    @property
    def trend(self) -> str:
        if self.drift_bias > 0:
            return "SURGE"
        elif self.drift_bias < 0:
            return "DROP"
        return "FLAT"

    @property
    def pct_impact(self) -> str:
        pct = int(round(self.drift_bias * 100))
        return f"+{pct}%" if pct > 0 else f"{pct}%"

    def rounds_remaining(self, current_round: int) -> int:
        return max(0, self.expires_round - current_round)

    def to_dict(self, current_round: Optional[int] = None) -> Dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction
        d["trend"] = self.trend
        d["pct_impact"] = self.pct_impact
        d["expires_round"] = self.expires_round
        if current_round is not None:
            d["rounds_remaining"] = self.rounds_remaining(current_round)
        return d


# Sol orbital salvage lore news templates
NEWS_TEMPLATES = [
    {
        "station_id": "ceres",
        "commodity": "FUEL",
        "headline": "CERES ICE-CRACKING COMPRESSOR BLOWOUT",
        "body": "Primary electrolysis manifolds at Piazzi Depot suffered catastrophic blowout. Local propellant reserves depleted; hydrogen refuel prices surging.",
        "drift_bias": 0.40,
        "duration_rounds": 4,
    },
    {
        "station_id": "mars",
        "commodity": "FRAG",
        "headline": "ARCADIA FOUNDRIES ANNOUNCE DEBRIS REQUISITION",
        "body": "Martian orbital shipyards announce emergency structural procurement contracts. Unrefined hull fragments trading at premium spot valuations.",
        "drift_bias": 0.35,
        "duration_rounds": 3,
    },
    {
        "station_id": "luna",
        "commodity": "FUEL",
        "headline": "SOLAR FLUX SATURATION DISRUPTS HE-3 EXTRACTION",
        "body": "Severe coronal mass ejection blinded southern polar collectors on Luna. Station refuel operations throttled to emergency reserves.",
        "drift_bias": 0.30,
        "duration_rounds": 3,
    },
    {
        "station_id": "earth",
        "commodity": "FRAG",
        "headline": "LOW-EARTH ORBIT DEBRIS SWEEP COMPLETED",
        "body": "Orbital Defense Command cleared 12,000 metric tons of dead satellite scrap into atmospheric decay trajectories. Local scrap supply tightened.",
        "drift_bias": 0.25,
        "duration_rounds": 3,
    },
    {
        "station_id": "ceres",
        "commodity": "FRAG",
        "headline": "16 PSYCHE BULK ORE CARRIER ARRIVES AT BELT GATE",
        "body": "A mega-freighter bearing 40,000 tons of nickel-iron scrap docked at Ceres Outer Ring, flooding salvage yards with cheap structural plates.",
        "drift_bias": -0.30,
        "duration_rounds": 4,
    },
    {
        "station_id": "mars",
        "commodity": "FUEL",
        "headline": "DEIMOS FUEL DEPOT PIPELINE EXTENSION COMPLETE",
        "body": "Phobos-Deimos transit consortium brought high-throughput LH2 transfer arrays online, easing fuel constraints across Martian orbits.",
        "drift_bias": -0.25,
        "duration_rounds": 3,
    },
    {
        "station_id": "luna",
        "commodity": "FRAG",
        "headline": "SHACKLETON METEORITE SHOWER COLD TRAP EXPOSED",
        "body": "High-velocity hyper-dense micrometeorites pelted Lunar south pole, scattering salvageable composite fragments across the crater floor.",
        "drift_bias": -0.20,
        "duration_rounds": 3,
    },
    {
        "station_id": "earth",
        "commodity": "FUEL",
        "headline": "KENNEDY EQUATORIAL SPACE ELEVATOR STRIKE RESOLVED",
        "body": "Dockworker syndicates ratified a 3-cycle tariff accord. Cryogenic fuel shipments resume normal orbital ascent schedules.",
        "drift_bias": -0.15,
        "duration_rounds": 2,
    },
    {
        "station_id": "earth",
        "commodity": "FOOD",
        "headline": "MIDWEST HYDROPONIC MEGA-FARM HARVEST SURPLUS",
        "body": "Atmospheric dome yields exceeded quarterly projections. Earth orbital depots flooded with fresh calorie supplies, depressing local food prices.",
        "drift_bias": -0.25,
        "duration_rounds": 3,
    },
    {
        "station_id": "ceres",
        "commodity": "FOOD",
        "headline": "BELT HYDROPONIC BLIGHT SPREADS IN OUTER HABITATS",
        "body": "Aeroponic nutrient failure struck Ceres Sub-Ring 4. Station Commissariat issues urgent food requisitions at elevated spot prices.",
        "drift_bias": 0.35,
        "duration_rounds": 4,
    },
    {
        "station_id": "ceres",
        "commodity": "ORE",
        "headline": "MAIN BELT MASS SPECTROMETRY STRIKES HIGH-GRADE ORE",
        "body": "Autonomous survey drones discovered rich platinum-group vein in Asteroid 101955. Ceres smelting docks operating at full capacity.",
        "drift_bias": -0.30,
        "duration_rounds": 4,
    },
    {
        "station_id": "earth",
        "commodity": "ORE",
        "headline": "TERRESTRIAL CLEAN TECH MANDATES EXHAUST LOCAL ORE RESERVES",
        "body": "Orbital solar satellite constellation fabrication ramps up. Earth yards bidding aggressively for imported raw asteroid ores.",
        "drift_bias": 0.35,
        "duration_rounds": 3,
    },
    {
        "station_id": "mars",
        "commodity": "FOOD",
        "headline": "VALLES MARINERIS AGRI-DOME SEALS RUPTURED",
        "body": "Sub-surface permafrost heave cracked hydroponic bio-sectors in Valles Marineris. Red planet colony importing emergency rations at high premiums.",
        "drift_bias": 0.30,
        "duration_rounds": 4,
    },
    {
        "station_id": "mars",
        "commodity": "ORE",
        "headline": "OLYMPUS STRIP-MINING AUTOMATION EXCEEDS QUOTAS",
        "body": "Heavy autonomous bucket-wheel excavators breached magnetite veins near Olympus Mons. Martian refineries saturated with raw ores.",
        "drift_bias": -0.25,
        "duration_rounds": 3,
    },
    {
        "station_id": "luna",
        "commodity": "FOOD",
        "headline": "CLAVIUS BIO-DOME VERTICAL CROP ROTATION PEAKS",
        "body": "Low-g synthetic protein cultures yielded an unexpected surplus at Clavius Station. Lunar commissary discounting fresh organic nutrient bars.",
        "drift_bias": -0.20,
        "duration_rounds": 3,
    },
    {
        "station_id": "luna",
        "commodity": "ORE",
        "headline": "MARE TRANQUILLITATIS RARE EARTH SINK DISCOVERED",
        "body": "Lunar industrial park announces rapid expansion of mass-driver launch tracks. Raw ore requisitions driving prices higher.",
        "drift_bias": 0.30,
        "duration_rounds": 3,
    },
    {
        "station_id": "mars",
        "commodity": "MACHINERY",
        "headline": "ARCADIA INDUSTRIAL FORGE FOUNDRY EXPANSION",
        "body": "Martian heavy automated fabrication yards brought fourth assembly line online, flooding regional depots with advanced industrial machinery.",
        "drift_bias": -0.30,
        "duration_rounds": 4,
    },
    {
        "station_id": "ceres",
        "commodity": "MACHINERY",
        "headline": "BELT DRILLING RIG BREAKDOWNS SPIKE REPLACEMENT DEMAND",
        "body": "Severe tectonic cavitation in deep asteroid shafts damaged core heavy drilling components. Ceres requisitions emergency replacement machinery at steep premiums.",
        "drift_bias": 0.35,
        "duration_rounds": 4,
    },
    {
        "station_id": "earth",
        "commodity": "MACHINERY",
        "headline": "ORBITAL ELEVATOR ROBOTICS OVERHAUL REQUIRES SPARES",
        "body": "Counterweight gantry stabilization project on Earth elevator hub demands rapid delivery of precision machine components.",
        "drift_bias": 0.25,
        "duration_rounds": 3,
    },
    {
        "station_id": "luna",
        "commodity": "MACHINERY",
        "headline": "SHACKLETON MASS-DRIVER ACCELERATOR TOOLING ACCORD",
        "body": "Lunar infrastructure consortium completes tooling phase for new linear magnetic accelerator. Surplus machinery released to spot markets.",
        "drift_bias": -0.20,
        "duration_rounds": 3,
    },
]


class GalNetEngine:
    """
    Deterministic news generator and drift shock manager.
    Seeded off referee floor state / round counter to preserve fuzz reproducibility.
    """

    def __init__(self, seed: int = 1337, shock_probability: float = 0.30):
        self.seed = seed
        self.rng = random.Random(seed)
        self.shock_probability = shock_probability
        self.current_round = 0
        self.events: List[GalNetNewsEvent] = []
        self.active_shocks: List[GalNetNewsEvent] = []

    def step_round(self, round_num: int) -> Optional[GalNetNewsEvent]:
        """
        Advances the GalNet engine to round_num.
        Prunes expired shocks and deterministically evaluates if a new shock triggers.
        """
        self.current_round = round_num

        # Prune expired shocks (active while round_num < event.round + duration)
        self.active_shocks = [
            ev for ev in self.active_shocks
            if round_num < (ev.round + ev.duration_rounds)
        ]

        # Deterministic roll for this round
        if self.rng.random() < self.shock_probability:
            return self._generate_shock(round_num)
        return None

    def force_shock(self, round_num: int, template_idx: Optional[int] = None) -> GalNetNewsEvent:
        """Force a shock event at round_num for deterministic testing or narrative prompts."""
        self.current_round = round_num
        return self._generate_shock(round_num, template_idx=template_idx)

    def _generate_shock(self, round_num: int, template_idx: Optional[int] = None) -> GalNetNewsEvent:
        if template_idx is not None and 0 <= template_idx < len(NEWS_TEMPLATES):
            tpl = NEWS_TEMPLATES[template_idx]
        else:
            tpl = self.rng.choice(NEWS_TEMPLATES)

        event_id = f"gn-{round_num}-{self.rng.randint(1000, 9999)}"
        event = GalNetNewsEvent(
            id=event_id,
            round=round_num,
            timestamp=time.time(),
            station_id=tpl["station_id"],
            commodity=tpl["commodity"],
            headline=tpl["headline"],
            body=tpl["body"],
            drift_bias=tpl["drift_bias"],
            duration_rounds=tpl["duration_rounds"],
        )

        self.events.append(event)
        self.active_shocks.append(event)
        return event

    def get_active_drift(self, station_id: str, commodity: str = "FRAG") -> float:
        """
        Returns the net additive drift bias currently in effect for a given station & commodity.
        Multiple overlapping events on the same asset sum additively.
        """
        net_drift = 0.0
        for ev in self.active_shocks:
            if ev.station_id == station_id and ev.commodity == commodity:
                net_drift += ev.drift_bias
        # Clamp total stacked drift to prevent runaway compounding (N3)
        net_drift = max(-0.40, min(0.40, net_drift))
        return round(net_drift, 4)

    def infer_trend(self, station_id: str, commodity: str, fogged_spot: Optional[float] = None) -> Dict[str, Any]:
        """
        Calculates the active un-fogged price trajectory for a station & commodity (#123).
        Enables players under Fog of War to deduce whether remote prices are surging or falling.
        """
        station_id = station_id.lower().strip()
        commodity = commodity.upper().strip()
        active = [ev for ev in self.active_shocks if ev.station_id == station_id and ev.commodity == commodity]
        net_drift = self.get_active_drift(station_id, commodity)

        if net_drift > 0:
            trend = "SURGE"
            direction = "up"
            desc = f"Upward price pressure (+{int(round(net_drift * 100))}%); un-fogged spot trending higher than stale quotes."
        elif net_drift < 0:
            trend = "DROP"
            direction = "down"
            desc = f"Downward price pressure ({int(round(net_drift * 100))}%); un-fogged spot trending lower than stale quotes."
        else:
            trend = "FLAT"
            direction = "neutral"
            desc = "Stable mean-reversion drift; no active exogenous shock."

        clean_fogged_spot = None
        if fogged_spot is not None:
            try:
                f_val = float(fogged_spot)
                if math.isfinite(f_val):
                    clean_fogged_spot = f_val
            except (ValueError, TypeError):
                clean_fogged_spot = None

        return {
            "station_id": station_id,
            "commodity": commodity,
            "net_drift": net_drift,
            "trend": trend,
            "direction": direction,
            "description": desc,
            "active_stories": [ev.to_dict(self.current_round) for ev in active],
            "fogged_spot": clean_fogged_spot,
        }

    def get_active_shocks(self) -> List[Dict[str, Any]]:
        """Returns list of currently active shock events."""
        return [ev.to_dict(self.current_round) for ev in self.active_shocks]

    def get_feed(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Returns recent news feed in reverse chronological order."""
        return [ev.to_dict(self.current_round) for ev in reversed(self.events[-limit:])]

def env_galnet_auto_step() -> bool:
    return os.environ.get("AGORA_GALNET_AUTO_STEP", "").strip().lower() in ("1", "true", "yes", "on")
