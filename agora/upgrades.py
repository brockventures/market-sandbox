"""
agora/upgrades.py - ship upgrades bought mid-game (#143).

Ryan, #agent-chat 2026-09-23 09:09: "add mid-game mechanics to the game to
allow corps to upgrade their ships to lower these probabilities" (the
flight hazards in agora/hazards.py and, once live, piracy #145).

Bought with CR while docked at any station, permanent for the game, one
tier at a time. Each tier multiplies an odd:
- shielding: flight-delay chance x0.7 (tier 1), x0.4 (tier 2)
- hold:      cargo-loss chance x0.6 / x0.3, and a loss takes 30% less
- armor:     piracy raid chance x0.7 / x0.4 (read by agora/piracy.py)
- engines:   one tier; trips of 3+ rounds take one round less
Upgrades are sunk costs: they add nothing to net worth, so buying one is a
real trade-off against cash. Prices below are a starting point.
"""

import os
from typing import Any, Dict, List

CATALOG: Dict[str, Dict[str, Any]] = {
    "shielding": {"prices": [4_000, 9_000], "factors": [0.7, 0.4],
                  "what": "cuts the chance of a flight delay"},
    "hold":      {"prices": [4_000, 9_000], "factors": [0.6, 0.3],
                  "what": "cuts the chance of losing cargo in flight, and a loss takes 30% less"},
    "armor":     {"prices": [5_000, 11_000], "factors": [0.7, 0.4],
                  "what": "cuts the chance of a pirate raid"},
    "engines":   {"prices": [12_000], "factors": [1.0],
                  "what": "trips of 3 rounds or more take one round less"},
}


def env_upgrades() -> bool:
    return os.environ.get("AGORA_UPGRADES", "").strip().lower() in ("1", "true", "yes", "on")


SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_upgrades (
    agent_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    tier      INTEGER NOT NULL,
    round     INTEGER NOT NULL,
    PRIMARY KEY (agent_id, kind)
)
"""


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {'v': 1, 'kind': 'reject', 'payload': {'reason': reason, 'detail': detail}}


class UpgradeDesk:
    def __init__(self, ref):
        self.ref = ref
        with ref.conn:
            ref.conn.execute(SCHEMA)

    def tier(self, agent: str, kind: str) -> int:
        r = self.ref.conn.execute("SELECT tier FROM fleet_upgrades WHERE agent_id = ? AND kind = ?",
                                  (agent, kind)).fetchone()
        return r["tier"] if r else 0

    def factor(self, agent: str, kind: str) -> float:
        """Multiplier on the matching odd; 1.0 when upgrades are off or none bought."""
        if not getattr(self.ref, 'upgrades_enabled', False):
            return 1.0
        t = self.tier(agent, kind)
        return CATALOG[kind]["factors"][t - 1] if t else 1.0

    def holdings(self, agent: str) -> Dict[str, int]:
        return {r["kind"]: r["tier"] for r in self.ref.conn.execute(
            "SELECT kind, tier FROM fleet_upgrades WHERE agent_id = ?", (agent,))}

    def catalog(self) -> List[Dict[str, Any]]:
        return [{"kind": k, "tiers": len(v["prices"]), "prices": v["prices"], "factors": v["factors"],
                 "what": v["what"]} for k, v in CATALOG.items()]

    def buy(self, agent: str, kind: str) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(agent)
        out = ref.fleet_out(agent) if hasattr(ref, 'fleet_out') else None
        if out:
            return _reject('fleet_out', out)
        kind = (kind or '').strip().lower()
        if kind not in CATALOG:
            return _reject('invalid_upgrade', f"Unknown upgrade '{kind}'. Options: {sorted(CATALOG)}")
        with ref.lock, ref.conn:
            loc = ref.get_vessel_location(agent)
            if loc.get('status') != 'docked':
                return _reject('vessel_not_docked', 'Upgrades are fitted while docked at a station')
            t = self.tier(agent, kind)
            prices = CATALOG[kind]["prices"]
            if t >= len(prices):
                return _reject('max_tier', f"{kind} is already at its top tier ({t})")
            price = prices[t]
            avail = ref.peer._available(agent, 'CR')
            if avail < price:
                return _reject('insufficient_credits', f"{kind} tier {t + 1} costs {price} CR; available {avail}")
            seq = ref._get_next_seq()
            txn = f"upgrade-{agent}-{kind}-{t + 1}"
            for acct, d in ((agent, -price), ('SYSTEM', price)):
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (d, acct))
                ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                                 (txn, seq, acct, d))
            ref.conn.execute("INSERT OR REPLACE INTO fleet_upgrades (agent_id, kind, tier, round) VALUES (?, ?, ?, ?)",
                             (agent, kind, t + 1, ref.current_round))
        return {'v': 1, 'kind': 'upgrade_ok', 'payload': {'agent_id': agent, 'upgrade': kind, 'tier': t + 1,
                                                          'price': price, 'upgrades': self.holdings(agent)}}
