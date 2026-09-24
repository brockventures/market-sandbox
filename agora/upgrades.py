"""
agora/upgrades.py - ship upgrades bought mid-game (#143).

Ryan, #agent-chat 2026-09-23 09:09: "add mid-game mechanics to the game to
allow corps to upgrade their ships to lower these probabilities" (the
flight hazards in agora/hazards.py and, once live, piracy #145).

Bought with CR while docked at any station, permanent for the game, one
tier at a time and in order. Tiers go on sale on one shared, staggered
shipyard schedule (#181, Ryan 2026-09-23: staged tiers across the game
rather than any one must-buy; no two unlocks in the same round):

    round   0  shielding t1, armor t1      125  hold t2
           40  engines t1                  175  shielding t3
           50  hold t1                     200  armor t3
           75  shielding t2                225  hold t3
          100  armor t2                    250  engines t2

Every unlock after round 0 is announced on GalNet that round. Each tier multiplies an odd (the factor replaces the
previous tier's, it does not stack on it):
- shielding: flight-delay chance x0.85 / x0.6 / x0.35
- hold:      cargo-loss chance x0.75 / x0.45 / x0.25; tiers 2 and 3 also
             make a loss take 30% less (LOSS_SIZE)
- armor:     piracy raid chance x0.7 / x0.45 / x0.25 (read by agora/piracy.py)
- engines:   tier 1: trips of 3+ rounds take one round less (ENGINE_CUTS);
             tier 2: every trip burns 40% less FUEL (ENGINE_FUEL_CUT, #189 --
             the old tier 2, a further round off trips of 5+ rounds, did
             nothing: no route is longer than 3 rounds)
Upgrades are capitalized at CAPITAL_PCT (50%) of what was paid: net worth,
and so the stock's NAV, counts half the cost as a fitted asset (#151,
settled with Zero 2026-09-23 10:33: "a 50% haircut for custom fitting").
Buying one still costs half its price on the board, so it stays a bet, and
the purchase is a public event the stock exchange reads as +2% (see
agora/exchange.py SHOCKS).
"""

import os
from typing import Any, Dict, List

CAPITAL_PCT = 0.5

# Per tier: prices, odds factors, and the round the tier goes on sale.
CATALOG: Dict[str, Dict[str, Any]] = {
    "shielding": {"prices": [3_000, 7_000, 14_000], "factors": [0.85, 0.6, 0.35], "unlocks": [0, 75, 175],
                  "what": "cuts the chance of a flight delay"},
    "hold":      {"prices": [4_000, 9_000, 16_000], "factors": [0.75, 0.45, 0.25], "unlocks": [50, 125, 225],
                  "what": "cuts the chance of losing cargo in flight; from tier 2 a loss also takes 30% less"},
    "armor":     {"prices": [7_500, 11_000, 18_000], "factors": [0.7, 0.45, 0.25], "unlocks": [0, 100, 200],
                  "what": "cuts the chance of a pirate raid"},
    "engines":   {"prices": [12_000, 24_000], "factors": [1.0, 1.0], "unlocks": [40, 250],
                  "what": "tier 1: trips of 3+ rounds take one round less; tier 2: every trip "
                          "burns 40% less fuel"},
}

# hold: the size of a cargo loss, by tier held (tier 0 first).
LOSS_SIZE = [1.0, 1.0, 0.7, 0.7]
# engines: (minimum trip length in rounds, rounds cut) that each tier adds.
# Tier 2 adds no round cut; it cuts fuel instead (ENGINE_FUEL_CUT).
ENGINE_CUTS = [(3, 1), (0, 0)]
# engines: share of a trip's FUEL burn cut, by tier held (tier 0 first) (#189).
ENGINE_FUEL_CUT = [0.0, 0.0, 0.4]

# Lane tech (#187 track 2): an entry may carry "standing": one capability per
# tier (or None), checked with ref.standing.allows() at purchase time only:
# factor() and the other effect paths never look at standing.


def standing_gate(kind: str, tier: int):
    """The standing capability tier `tier` (1-based) of `kind` needs, or None."""
    gates = CATALOG[kind].get("standing") or []
    return gates[tier - 1] if tier - 1 < len(gates) else None


NEWS_NOUN = {"shielding": "SHIELDING", "hold": "HARDENED HOLDS", "armor": "ARMOR PLATING", "engines": "ENGINES"}


def unlock_round(kind: str, tier: int) -> int:
    """The round tier `tier` (1-based) of `kind` goes on sale."""
    return CATALOG[kind]["unlocks"][tier - 1]


def unlock_news(kind: str, tier: int) -> tuple:
    """(headline, body) of the GalNet story for this tier's unlock."""
    c = CATALOG[kind]
    return (f"SHIPYARDS NOW FITTING {NEWS_NOUN.get(kind, kind.upper())} TIER {tier}",
            f"Sol's shipyards have finished retooling and can now fit {kind} tier {tier} at every station, "
            f"for {c['prices'][tier - 1]:,} CR. {kind.capitalize()} {c['what']}. "
            f"Tiers are fitted in order.")


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

    def loss_size_factor(self, agent: str) -> float:
        """Share of the rolled cargo loss a hold upgrade leaves (LOSS_SIZE)."""
        if not getattr(self.ref, 'upgrades_enabled', False):
            return 1.0
        return LOSS_SIZE[min(self.tier(agent, 'hold'), len(LOSS_SIZE) - 1)]

    def engine_cut(self, agent: str, rounds: int) -> int:
        """Rounds the engines upgrade takes off a trip of `rounds` rounds."""
        if not getattr(self.ref, 'upgrades_enabled', False):
            return 0
        t = self.tier(agent, 'engines')
        return sum(cut for need, cut in ENGINE_CUTS[:t] if rounds >= need)

    def engine_fuel(self, agent: str, fuel: int) -> int:
        """FUEL a trip whose route burns `fuel` actually burns for `agent`:
        engines tier 2 cuts it by ENGINE_FUEL_CUT (40%), rounded, never below
        1 on a trip that burns any (#189)."""
        if fuel <= 0 or not getattr(self.ref, 'upgrades_enabled', False):
            return fuel
        cut = ENGINE_FUEL_CUT[min(self.tier(agent, 'engines'), len(ENGINE_FUEL_CUT) - 1)]
        return max(1, int(round(fuel * (1.0 - cut)))) if cut else fuel

    def book_value(self, agent: str) -> int:
        """Fitted upgrades as an asset: CAPITAL_PCT of every tier's price."""
        total = 0
        for kind, tier in self.holdings(agent).items():
            total += sum(CATALOG[kind]["prices"][:tier]) if kind in CATALOG else 0
        return int(total * CAPITAL_PCT)

    def holdings(self, agent: str) -> Dict[str, int]:
        return {r["kind"]: r["tier"] for r in self.ref.conn.execute(
            "SELECT kind, tier FROM fleet_upgrades WHERE agent_id = ?", (agent,))}

    def catalog(self) -> List[Dict[str, Any]]:
        """GET /referee/upgrades catalog: every tier's price, factor, unlock
        round and whether it is still locked. unlock_round / locked are tier 1's."""
        r = getattr(self.ref, 'current_round', 0)
        out = []
        for k, v in CATALOG.items():
            tiers = [{"tier": i + 1, "price": p, "factor": v["factors"][i], "unlock_round": v["unlocks"][i],
                      "locked": v["unlocks"][i] > r, "standing": standing_gate(k, i + 1)}
                     for i, p in enumerate(v["prices"])]
            out.append({"kind": k, "tiers": len(v["prices"]), "prices": v["prices"], "factors": v["factors"],
                        "unlock_rounds": v["unlocks"], "what": v["what"], "tier_detail": tiers,
                        "unlock_round": v["unlocks"][0], "locked": tiers[0]["locked"]})
        return out

    def step_locked(self, round_num: int) -> List[str]:
        """Called from step_round under ref.lock inside its transaction: post
        the GalNet story for every tier that unlocks by this round and has not
        had one yet (once a game each; round-0 tiers are on sale from the start)."""
        import json
        import time
        from agora.galnet import GalNetNewsEvent
        posted = []
        for kind, c in CATALOG.items():
            for i, at in enumerate(c["unlocks"]):
                if at <= 0 or round_num < at:
                    continue
                eid = f"gn-shipyard-{kind}-t{i + 1}"
                if self.ref.conn.execute("SELECT 1 FROM book_events WHERE kind = 'news' AND payload LIKE ?",
                                         (f'%"{eid}"%',)).fetchone():
                    continue
                headline, body = unlock_news(kind, i + 1)
                ev = GalNetNewsEvent(id=eid, round=round_num, timestamp=time.time(), station_id='', commodity='',
                                     headline=headline, body=body, drift_bias=0.0, duration_rounds=0)
                galnet = getattr(self.ref, 'galnet', None)
                if galnet is not None:
                    galnet.events.append(ev)
                self.ref.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'news', ?)",
                                      (self.ref.current_seq + 1, json.dumps(ev.to_dict())))
                posted.append(f"{kind} t{i + 1}")
        return posted

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
            # Fleet-wide upgrades (they fit every ship, #175): any ship docked will do.
            if not ref.docked_stations(agent) and ref.get_vessel_location(agent).get('status') != 'docked':
                return _reject('vessel_not_docked', 'Upgrades are fitted while docked at a station')
            t = self.tier(agent, kind)
            prices = CATALOG[kind]["prices"]
            if t >= len(prices):
                return _reject('max_tier', f"{kind} is already at its top tier ({t})")
            unlock = unlock_round(kind, t + 1)
            if ref.current_round < unlock:
                return _reject('upgrade_locked', f"{kind} tier {t + 1} is not on sale until round {unlock}: the "
                                                 f"shipyards are retooling (round now {ref.current_round}). "
                                                 f"GalNet will announce it.")
            gate = standing_gate(kind, t + 1)
            standing = getattr(ref, 'standing', None)
            if gate and standing is not None and not standing.allows(agent, gate):
                return _reject('standing_required', f"{kind} tier {t + 1} is lane tech: it needs {gate} standing "
                                                    f"(GET /referee/standing)")
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
            if getattr(ref, 'events_enabled', False):
                ref.events.record_locked('upgrade', 'public', actor=agent, amount=price,
                                         detail=f"{agent} fitted {kind} tier {t + 1} for {price} CR")
        return {'v': 1, 'kind': 'upgrade_ok', 'payload': {'agent_id': agent, 'upgrade': kind, 'tier': t + 1,
                                                          'price': price, 'upgrades': self.holdings(agent)}}
