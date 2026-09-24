"""
agora/hazards.py - bad luck in flight (#139).

Ryan, #agent-chat 2026-09-23 08:37: "there needs to be pressure and bad
luck events"; 09:09 approved the starting odds. Sized in the simulator
(#141): with these odds and the 50% contract lapse penalty, about 1 in 5
corps went under per 300-round game when fleets never reacted.

Each trip is rolled once, when it leaves:
- P_DELAY: a storm or engine fault adds 1-3 rounds to the trip, which can
  blow a contract deadline.
- P_LOSS: a hull breach or spoilage loses 10-20% of the cargo (#194). The lost
  goods stay with SYSTEM (they were escrowed there at departure), so the
  ledger balances.

The result is told to the fleet at once, in the move's response, the
transit event and the briefing, so a fleet can work its way out: sell the
contract it will now miss, raise cash, change plans. Rolls are seeded from
the game seed. Upgrades that lower the odds are #143.
"""

import os
import random
from typing import Any, Dict, Optional, Tuple

DEFAULT_P_DELAY = 0.20
DEFAULT_P_LOSS = 0.10
DELAY_ROUNDS = (1, 3)
LOSS_FRACTION = (0.10, 0.20)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transit_hazards (
    transit_id  TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    round       INTEGER NOT NULL,
    delay       INTEGER NOT NULL DEFAULT 0,
    lost_qty    INTEGER NOT NULL DEFAULT 0,
    commodity   TEXT,
    note        TEXT
)
"""


def parse_hazards(v: Any) -> Optional[Tuple[float, float]]:
    """'0.2,0.1' / [0.2, 0.1] / {'delay':..,'loss':..} -> (p_delay, p_loss); '0'/False/None -> None."""
    if v is None or v is False:
        return None
    if isinstance(v, dict):
        v = (v.get('delay', 0), v.get('loss', 0))
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ('', '0', 'off', 'false', 'no'):
            return None
        if s in ('1', 'on', 'true', 'yes'):
            return (DEFAULT_P_DELAY, DEFAULT_P_LOSS)
        v = [x for x in s.replace(' ', '').split(',') if x]
    try:
        d, l = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError):
        return None
    d, l = min(1.0, max(0.0, d)), min(1.0, max(0.0, l))
    return (d, l) if (d or l) else None


def env_hazards() -> Optional[Tuple[float, float]]:
    return parse_hazards(os.environ.get('AGORA_HAZARDS'))


class HazardEngine:
    def __init__(self, conn, odds: Optional[Tuple[float, float]] = None, seed: int = 0):
        self.conn = conn
        with conn:
            conn.execute(SCHEMA)
        self.odds = odds
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.rng = random.Random(f"hazards-{seed}")

    def roll(self, cargo_qty: int, delay_factor: float = 1.0, loss_factor: float = 1.0,
             loss_size_factor: float = 1.0) -> Tuple[int, int, str]:
        """(extra rounds, units lost, note). Always draws the same number of
        values so one trip's outcome does not shift the next trip's."""
        if not self.odds:
            return 0, 0, ''
        p_delay, p_loss = self.odds
        a, d, b, f = self.rng.random(), self.rng.randint(*DELAY_ROUNDS), self.rng.random(), self.rng.uniform(*LOSS_FRACTION)
        # *_factor: ship upgrades (agora/upgrades.py) scale the odds and the loss.
        delay = d if a < p_delay * delay_factor else 0
        lost = int(cargo_qty * f * loss_size_factor) if (cargo_qty > 0 and b < p_loss * loss_factor) else 0
        notes = []
        if delay:
            notes.append(f"storm on the route: arrival {delay} round{'s' if delay > 1 else ''} late")
        if lost:
            notes.append(f"hull breach: {lost} of {cargo_qty} units lost")
        return delay, lost, '; '.join(notes)

    def record(self, transit_id: str, agent_id: str, round_num: int, delay: int, lost: int,
               commodity: str, note: str) -> None:
        if delay or lost:
            self.conn.execute("INSERT OR REPLACE INTO transit_hazards VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (transit_id, agent_id, round_num, delay, lost, commodity, note))

    def recent(self, since_round: int) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM transit_hazards WHERE round >= ? ORDER BY round DESC, transit_id", (since_round,))]
