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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
        return round(net_drift, 4)

    def get_active_shocks(self) -> List[Dict[str, Any]]:
        """Returns list of currently active shock events."""
        return [ev.to_dict() for ev in self.active_shocks]

    def get_feed(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Returns recent news feed in reverse chronological order."""
        return [ev.to_dict() for ev in reversed(self.events[-limit:])]
