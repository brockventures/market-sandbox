```python
"""
agora.galnet - GalNet Breaking News Wire, Exogenous Drift Shock Engine,
and Dynamic Station Procurement Contracts / Delivery Bounties (Task #53).
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


@dataclass
class ProcurementContract:
    contract_id: str
    station_id: str
    commodity: str
    required_amount: int
    reward_per_unit: float
    on_time_bonus: float
    expires_round: int
    headline: str
    delivered_amount: int = 0
    completed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Sol orbital salvage lore news templates & procurement profiles
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
    Deterministic news generator, drift shock manager, and dynamic station
    procurement contract / delivery bounty broadcaster.
    Seeded off referee floor state / round counter to preserve fuzz reproducibility.
    """

    def __init__(self, seed: int = 1337, shock_probability: float = 0.30, contract_probability: float = 0.40):
        self.seed = seed
        self.rng = random.Random(seed)
        self.shock_probability = shock_probability
        self.contract_probability = contract_probability
        self.current_round = 0
        self.events: List[GalNetNewsEvent] = []
        self.active_shocks: List[GalNetNewsEvent] = []
        self.active_contracts: List[ProcurementContract] = []
        self.contract_counter = 0

    def step(self, current_round: int) -> List[GalNetNewsEvent]:
        """
        Advances the GalNet engine by one round. Evaluates active shocks duration
        and rolls for new breaking news events and procurement bounties.
        """
        self.current_round = current_round
        
        # Age existing shocks
        surviving_shocks = []
        for shock in self.active_shocks:
            shock.duration_rounds -= 1
            if shock.duration_rounds > 0:
                surviving_shocks.append(shock)
        self.active_shocks = surviving_shocks

        # Age / expire existing contracts
        active_c = []
        for contract in self.active_contracts:
            if not contract.completed and self.current_round <= contract.expires_round:
                active_c.append(contract)
        self.active_contracts = active_c

        new_events: List[GalNetNewsEvent] = []

        # Roll for new shock event
        if self.rng.random() < self.shock_probability:
            template = self.rng.choice(NEWS_TEMPLATES)
            event_id = f"GALNET-{self.current_round}-{self.rng.randint(100, 999)}"
            event = GalNetNewsEvent(
                id=event_id,
                round=self.current_round,
                timestamp=time.time(),
                station_id=template["station_id"],
                commodity=template["commodity"],
                headline=template["headline"],
                body=template["body"],
                drift_bias=template["drift_bias"],
                duration_rounds=template["duration_rounds"]
            )
            self.events.append(event)
            self.active_shocks.append(event)
            new_events.append(event)

        # Roll for new station procurement bounty / RFQ
        if self.rng.random() < self.contract_probability:
            self._generate_procurement_bounty()

        return new_events

    def _generate_procurement_bounty(self) -> ProcurementContract:
        """Generates a dynamic station procurement contract and broadcasts it as a GalNet RFQ."""
        self.contract_counter += 1
        contract_id = f"RFQ-{self.current_round}-{self.contract_counter:03d}"
        
        station_id = self.rng.choice(["ceres", "mars", "luna", "earth"])
        commodity = self.rng.choice(["FUEL", "FRAG"])
        required_amount = self.rng.randint(50, 250)
        duration = self.rng.randint(3, 6)
        expires_round = self.current_round + duration
        
        # Economics: high urgency payout calculation
        reward_per_unit = round(self.rng.uniform(20.0, 45.0), 2)
        on_time_bonus = round(self.rng.uniform(150.0, 500.0), 2)

        if station_id == "mars":
            station_name = "Mars Foundry"
        elif station_id == "ceres":
            station_name = "Ceres Life Support"
        elif station_id == "luna":
            station_name = "Luna Gateway"
        else:
            station_name = "Earth Orbital Hub"

        if commodity == "FRAG":
            headline = f"{station_name.upper()} URGENTLY REQUIRES {required_amount} FRAG WITHIN {duration} ROUNDS."
            body = f"High-priority procurement contract broadcast. Payout: {reward_per_unit} CR/unit + {on_time_bonus} CR on-time delivery bonus."
        else:
            headline = f"{station_name.upper()} CRITICALLY SHORT ON PROPELLANT. BUYING {required_amount} FUEL."
            body = f"Emergency refueling requisition order active. Payout: {reward_per_unit} CR/unit + {on_time_bonus} CR on-time delivery bonus."

        contract = ProcurementContract(
            contract_id=contract_id,
            station_id=station_id,
            commodity=commodity,
            required_amount=required_amount,
            reward_per_unit=reward_per_unit,
            on_time_bonus=on_time_bonus,
            expires_round=expires_round,
            headline=headline
        )
        self.active_contracts.append(contract)

        # Also mirror as a high-urgency GalNet news dispatch so agents notice it on the wire
        rfq_event = GalNetNewsEvent(
            id=contract_id,
            round=self.current_round,
            timestamp=time.time(),
            station_id=station_id,
            commodity=commodity,
            headline=headline,
            body=body,
            drift_bias=0.15, # Slight positive drift pressure from exogenous consumption demand
            duration_rounds=duration
        )
        self.events.append(rfq_event)
        self.active_shocks.append(rfq_event)

        return contract

    def fulfill_contract(self, contract_id: str, amount: int) -> Dict[str, Any]:
        """
        Records commodity delivery toward an active procurement bounty.
        Returns payout details including unit compensation and on-time bonus if fully satisfied.
        """
        for contract in self.active_contracts:
            if contract.contract_id == contract_id and not contract.completed:
                remaining = contract.required_amount - contract.delivered_amount
                fulfilled_now = min(amount, remaining)
                contract.delivered_amount += fulfilled_now

                base_payout = fulfilled_now * contract.reward_per_unit
                bonus_payout = 0.0

                if contract.delivered_amount >= contract.required_amount:
                    contract.completed = True
                    bonus_payout = contract.on_time_bonus

                total_payout = base_payout + bonus_payout
                return {
                    "success": True,
                    "contract_id": contract_id,
                    "delivered": fulfilled_now,
                    "base_payout": base_payout,
                    "on_time_bonus": bonus_payout,
                    "total_payout": total_payout,
                    "completed": contract.completed
                }
        return {"success": False, "error": "Contract not found or already completed."}

    def get_active_contracts(self) -> List[Dict[str, Any]]:
        return [c.to_dict() for c in self.active_contracts if not c.completed]

    def get_aggregate_drift_bias(self, station_id: str, commodity: str) -> float:
        """
        Sums active drift biases affecting a specific station and commodity pair.
        """
        bias = 0.0
        for shock in self.active_shocks:
            if shock.station_id == station_id and shock.commodity == commodity:
                bias += shock.drift_bias
        return bias
```
"""
agora.galnet - GalNet Breaking News Wire, Exogenous Drift Shock Engine,
and Dynamic Station Procurement Contracts / Delivery Bounties (Task #53).
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


@dataclass
class ProcurementContract:
    contract_id: str
    station_id: str
    commodity: str
    required_amount: int
    reward_per_unit: float
    on_time_bonus: float
    expires_round: int
    headline: str
    delivered_amount: int = 0
    completed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Sol orbital salvage lore news templates & procurement profiles
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
    Deterministic news generator, drift shock manager, and dynamic station
    procurement contract / delivery bounty broadcaster.
    Seeded off referee floor state / round counter to preserve fuzz reproducibility.
    """

    def __init__(self, seed: int = 1337, shock_probability: float = 0.30, contract_probability: float = 0.40):
        self.seed = seed
        self.rng = random.Random(seed)
        self.shock_probability = shock_probability
        self.contract_probability = contract_probability
        self.current_round = 0
        self.events: List[GalNetNewsEvent] = []
        self.active_shocks: List[GalNetNewsEvent] = []
        self.active_contracts: List[ProcurementContract] = []
        self.contract_counter = 0

    def step(self, current_round: int) -> List[GalNetNewsEvent]:
        """
        Advances the GalNet engine by one round. Evaluates active shocks duration
        and rolls for new breaking news events and procurement bounties.
        """
        self.current_round = current_round
        
        # Age existing shocks
        surviving_shocks = []
        for shock in self.active_shocks:
            shock.duration_rounds -= 1
            if shock.duration_rounds > 0:
                surviving_shocks.append(shock)
        self.active_shocks = surviving_shocks

        # Age / expire existing contracts
        active_c = []
        for contract in self.active_contracts:
            if not contract.completed and self.current_round <= contract.expires_round:
                active_c.append(contract)
        self.active_contracts = active_c

        new_events: List[GalNetNewsEvent] = []

        # Roll for new shock event
        if self.rng.random() < self.shock_probability:
            template = self.rng.choice(NEWS_TEMPLATES)
            event_id = f"GALNET-{self.current_round}-{self.rng.randint(100, 999)}"
            event = GalNetNewsEvent(
                id=event_id,
                round=self.current_round,
                timestamp=time.time(),
                station_id=template["station_id"],
                commodity=template["commodity"],
                headline=template["headline"],
                body=template["body"],
                drift_bias=template["drift_bias"],
                duration_rounds=template["duration_rounds"]
            )
            self.events.append(event)
            self.active_shocks.append(event)
            new_events.append(event)

        # Roll for new station procurement bounty / RFQ
        if self.rng.random() < self.contract_probability:
            self._generate_procurement_bounty()

        return new_events

    def _generate_procurement_bounty(self) -> ProcurementContract:
        """Generates a dynamic station procurement contract and broadcasts it as a GalNet RFQ."""
        self.contract_counter += 1
        contract_id = f"RFQ-{self.current_round}-{self.contract_counter:03d}"
        
        station_id = self.rng.choice(["ceres", "mars", "luna", "earth"])
        commodity = self.rng.choice(["FUEL", "FRAG"])
        required_amount = self.rng.randint(50, 250)
        duration = self.rng.randint(3, 6)
        expires_round = self.current_round + duration
        
        # Economics: high urgency payout calculation
        reward_per_unit = round(self.rng.uniform(20.0, 45.0), 2)
        on_time_bonus = round(self.rng.uniform(150.0, 500.0), 2)

        if station_id == "mars":
            station_name = "Mars Foundry"
        elif station_id == "ceres":
            station_name = "Ceres Life Support"
        elif station_id == "luna":
            station_name = "Luna Gateway"
        else:
            station_name = "Earth Orbital Hub"

        if commodity == "FRAG":
            headline = f"{station_name.upper()} URGENTLY REQUIRES {required_amount} FRAG WITHIN {duration} ROUNDS."
            body = f"High-priority procurement contract broadcast. Payout: {reward_per_unit} CR/unit + {on_time_bonus} CR on-time delivery bonus."
        else:
            headline = f"{station_name.upper()} CRITICALLY SHORT ON PROPELLANT. BUYING {required_amount} FUEL."
            body = f"Emergency refueling requisition order active. Payout: {reward_per_unit} CR/unit + {on_time_bonus} CR on-time delivery bonus."

        contract = ProcurementContract(
            contract_id=contract_id,
            station_id=station_id,
            commodity=commodity,
            required_amount=required_amount,
            reward_per_unit=reward_per_unit,
            on_time_bonus=on_time_bonus,
            expires_round=expires_round,
            headline=headline
        )
        self.active_contracts.append(contract)

        # Also mirror as a high-urgency GalNet news dispatch so agents notice it on the wire
        rfq_event = GalNetNewsEvent(
            id=contract_id,
            round=self.current_round,
            timestamp=time.time(),
            station_id=station_id,
            commodity=commodity,
            headline=headline,
            body=body,
            drift_bias=0.15, # Slight positive drift pressure from exogenous consumption demand
            duration_rounds=duration
        )
        self.events.append(rfq_event)
        self.active_shocks.append(rfq_event)

        return contract

    def fulfill_contract(self, contract_id: str, amount: int) -> Dict[str, Any]:
        """
        Records commodity delivery toward an active procurement bounty.
        Returns payout details including unit compensation and on-time bonus if fully satisfied.
        """
        for contract in self.active_contracts:
            if contract.contract_id == contract_id and not contract.completed:
                remaining = contract.required_amount - contract.delivered_amount
                fulfilled_now = min(amount, remaining)
                contract.delivered_amount += fulfilled_now

                base_payout = fulfilled_now * contract.reward_per_unit
                bonus_payout = 0.0

                if contract.delivered_amount >= contract.required_amount:
                    contract.completed = True
                    bonus_payout = contract.on_time_bonus

                total_payout = base_payout + bonus_payout
                return {
                    "success": True,
                    "contract_id": contract_id,
                    "delivered": fulfilled_now,
                    "base_payout": base_payout,
                    "on_time_bonus": bonus_payout,
                    "total_payout": total_payout,
                    "completed": contract.completed
                }
        return {"success": False, "error": "Contract not found or already completed."}

    def get_active_contracts(self) -> List[Dict[str, Any]]:
        return [c.to_dict() for c in self.active_contracts if not c.completed]

    def get_aggregate_drift_bias(self, station_id: str, commodity: str) -> float:
        """
        Sums active drift biases affecting a specific station and commodity pair.
        """
        bias = 0.0
        for shock in self.active_shocks:
            if shock.station_id == station_id and shock.commodity == commodity:
                bias += shock.drift_bias
        return bias
