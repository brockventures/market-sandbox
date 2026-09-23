#!/usr/bin/env python3
"""
tools/economy_sim.py - headless economics simulator for Station Agora.

Runs scripted fleets against an in-process referee (no HTTP, no Discord,
no production server) for hundreds of rounds and reports whether the
design produces sustained trading.

It plays the live game (#155; Ryan, #agent-chat 2026-09-23: "the simulator
should simulate the same game as our live game"). The referee comes from
agora.server.build_referee_from_env, the live server's own factory, with
every AGORA_* environment variable ignored, so every feature the server
turns on is on here with the same constants: reactive depots, the 25%
band, fog, peer trades, idle fee, rival shares, the stock exchange (with
event shocks), contracts, corporate debt and takeovers, upgrades, hazards,
piracy, and corp-event secrecy and exposure.
The game starts as a bare POST /referee/admin/new_game does. Fleets act
only through the referee and desk methods the HTTP routes call; this file
holds no game rules, only bot strategies, scenarios and reporting.
tests/test_sim_live_parity.py fails if the two drift.

Strategies:
  hauler        buys where a good is cheap, flies it, sells where it is dear;
                claims, trades and delivers contracts, buys upgrades, escorts
                high-value belt trips, pays cheap ransoms and fights the rest,
                takes peer offers and flies to collect them (#126)
  privateer     a hauler that also hires privateers against the leader
  maker         quotes both sides at its home station, joining the depot touch
  idler         does nothing (and pays the live idle fee for it)
  novice        a zero-context LLM player reading /referee/briefing: sees the
                whole board, but picks routes loosely (softmax, not argmax),
                sends malformed or misplaced orders, often prices inside the
                spread instead of hitting the depot, forgets to cancel stale
                orders, sometimes forgets to buy FUEL before a MOVE, overvalues
                contracts, never escorts, and often ignores a pirate demand
  stock_trader  trades rival stocks on the exchange only
  daytrader     never flies; trades its station's goods from price history

Scoring uses cash plus inventory at a FIXED reference price (the mean
BASE_PRICES across stations), not the leaderboard: the leaderboard marks
FUEL at zero, all FRAG at the Ceres mark, and cargo in transit (escrowed
to SYSTEM) at zero. Contract deposits and peer escrow count for their
owner, and fitted upgrades at the live book value (UpgradeDesk.book_value,
half their price). Leaderboard net worth is reported alongside.

Knobs change the live game, never add a rule of their own. Default = live.
Usage:
  python3 tools/economy_sim.py                      # default scenario set
  python3 tools/economy_sim.py --rounds 500 --seeds 3 --table
  python3 tools/economy_sim.py --scenario haulers4 --genesis planet
  python3 tools/economy_sim.py --off piracy corporate --band-pct 0.1
  python3 tools/economy_sim.py --set hazards=0.3,0.1 --const piracy.RANSOM_PCT=0.2
"""

import argparse
import contextlib
import faulthandler
import importlib
import inspect
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agora import contracts as contracts_mod  # noqa: E402
from agora import covert as covert_mod  # noqa: E402
from agora import exchange as exchange_mod  # noqa: E402
from agora import piracy as piracy_mod  # noqa: E402
from agora.referee import AgoraReferee  # noqa: E402
from agora.server import build_referee_from_env  # noqa: E402
from agora.spatial import STATIONS, BASE_PRICES, get_route  # noqa: E402
from agora import upgrades as upgrades_mod  # noqa: E402
from agora.upgrades import CATALOG as UPGRADES  # noqa: E402

FLEETS = ["zero", "amos", "marvin", "aerial"]
TRADED = ["FRAG", "FUEL", "FOOD", "ORE"]
REF_PRICE = {c: sum(BASE_PRICES[s][c] for s in STATIONS) / len(STATIONS) for c in TRADED}

# Home-station export for the "planet" genesis (the #agent-chat proposal of
# 2026-09-22): each fleet's starting FRAG is swapped, at equal reference
# value, for its home station's cheap export. Luna keeps cash.
PLANET_EXPORT = {"ceres": "ORE", "earth": "FOOD", "mars": "FUEL", "luna": None}

SCENARIOS = {
    "mixed": {"zero": "hauler", "amos": "hauler", "marvin": "maker", "aerial": "idler"},
    "haulers4": {f: "hauler" for f in FLEETS},
    "idle4": {f: "idler" for f in FLEETS},
    "market": {"zero": "hauler", "amos": "hauler", "marvin": "inside_maker", "aerial": "hauler"},
    # Raw-LLM players (Ryan, #agent-chat 2026-09-22 20:53): every fleet is a
    # zero-context agent working from the briefing page.
    "novice4": {f: "novice" for f in FLEETS},
    "novice_vs_haulers": {"zero": "hauler", "amos": "hauler", "marvin": "novice", "aerial": "novice"},
    # #119: one fleet trades rival stocks only, against growing haulers or
    # erratic novices, on the live exchange.
    "stocks": {"zero": "hauler", "amos": "hauler", "marvin": "stock_trader", "aerial": "hauler"},
    # #145: one hauler that also funds privateers against the leader.
    "privateer_vs_haulers": {"zero": "hauler", "amos": "hauler", "marvin": "privateer", "aerial": "hauler"},
    "stocks_vs_novices": {"zero": "novice", "amos": "hauler", "marvin": "stock_trader", "aerial": "novice"},
    # Day trading from price history (Ryan, #agent-chat 2026-09-22 22:30).
    "daytrade_vs_haulers": {"zero": "hauler", "amos": "hauler", "marvin": "daytrader", "aerial": "daytrader"},
    # #162: one fleet per play style. Rotated: seed s moves every style s
    # stations along, so over 4k seeds each style starts at each home k times
    # (the home station matters: a hauler starting at Ceres out-earns one
    # starting at Earth by 100k+).
    "styles": {"zero": "hauler", "amos": "stock_trader", "marvin": "maker", "aerial": "privateer"},
    # The same, with a novice in the stock trader's seat (#162, novice survival).
    "styles_novice": {"zero": "hauler", "amos": "novice", "marvin": "maker", "aerial": "privateer"},
    # #174: Covert ops strategies (saboteur and spy).
    "saboteur_vs_haulers": {"zero": "hauler", "amos": "hauler", "marvin": "saboteur", "aerial": "hauler"},
    "spy_vs_haulers": {"zero": "hauler", "amos": "hauler", "marvin": "spy", "aerial": "hauler"},
    "styles_saboteur": {"zero": "hauler", "amos": "saboteur", "marvin": "maker", "aerial": "privateer"},
    "styles_spy": {"zero": "hauler", "amos": "spy", "marvin": "maker", "aerial": "privateer"},
    "styles_covert": {"zero": "hauler", "amos": "spy", "marvin": "maker", "aerial": "saboteur"},
}
# Scenarios whose styles rotate through the home stations with the seed.
ROTATING = {"styles", "styles_novice", "styles_saboteur", "styles_spy", "styles_covert"}


def scenario_kinds(scenario: str, seed: int) -> Dict[str, str]:
    """Which fleet plays which strategy in this seed's game."""
    kinds = SCENARIOS[scenario]
    if scenario not in ROTATING:
        return dict(kinds)
    styles = [kinds[a] for a in FLEETS]
    k = seed % len(FLEETS)
    return {a: styles[(i + k) % len(FLEETS)] for i, a in enumerate(FLEETS)}

# Strategies that fly goods, so the only ones that claim, buy or deliver
# contracts and trade on the peer desk: an idler or a market maker never
# delivers, so a claim by one is a guaranteed penalty.
CONTRACTORS = {"hauler", "novice", "privateer", "saboteur", "spy"}

# Bot thresholds for the live-only mechanics. Behaviour, not rules.
UPGRADE_ORDER = ("armor", "hold", "shielding", "engines")
UPGRADE_CASH_MULT = 5      # a hauler buys an upgrade tier once it has 5x its price in cash
NOVICE_UPGRADE_CASH_MULT = 2
DEADLINE_SLACK = 1         # rounds a hauler leaves for a flight delay when it values a contract
CLAIM_MIN_VALUE = 1_000    # a hauler claims a contract only if it expects this much from it
FUEL_KEEP = 150            # FUEL a fleet never delivers or sells: it needs it to fly
RAID_LOSS = 0.2            # expected share of cargo value a raid costs (ransom 15%, a fight 25% on average)
RANSOM_CASH_SHARE = 0.2    # pay a ransom up to this share of available CR, otherwise fight
NOVICE_IGNORES_DEMAND = 0.5


# ------------------------------------------------------------------ helpers

def order(ref: AgoraReferee, agent: str, side: str, qty: int, price: int, comm: str, st: str, tag: str):
    """POST /referee/orders."""
    return ref.submit_envelope({"v": 1, "kind": "order", "payload": {
        "order_id": f"sim-{agent}-{tag}-{ref.current_round}-{ref.current_seq}",
        "agent_id": agent, "side": side, "qty": int(qty), "limit_price": int(price),
        "instrument": comm, "station_id": st, "seq_seen": ref.current_seq}})


def cancel_all(ref: AgoraReferee, agent: str) -> None:
    """POST /referee/orders/cancel per resting order. Not ref.cancel_all:
    that marks the fleet active even with nothing to cancel, which would
    dodge the idle fee for free."""
    for st_books in ref.books.values():
        for b in st_books.values():
            for o in list(b.bids) + list(b.asks):
                if o.agent_id == agent:
                    ref.cancel_order(agent, o.order_id)


def band_ok(ref: AgoraReferee, st: str, comm: str, price: int) -> bool:
    b = ref.circuit_breaker.get_bands(st, comm)
    if b.get("status") == "halted":
        return False
    lo, hi = b.get("lower_limit"), b.get("upper_limit")
    return (lo is None or price >= lo) and (hi is None or price <= hi)


def location(ref: AgoraReferee, agent: str) -> Optional[str]:
    loc = ref.get_vessel_location(agent)
    return loc["station_id"] if loc.get("status") == "docked" else None


def inventory(ref: AgoraReferee, agent: str) -> Dict[str, int]:
    return {c: ref.get_balance(agent, c) for c in ["CR"] + TRADED}


def available(ref: AgoraReferee, agent: str, inst: str) -> int:
    """Balance less what resting orders commit, as every desk checks it."""
    return ref.peer._available(agent, inst)


def debt(ref: AgoraReferee, agent: str) -> int:
    """GET /referee/corporate."""
    if not ref.corporate_enabled:
        return 0
    return ref.corporate.summary()["corps"].get(agent, {}).get("debt", 0)


def my_contracts(ref: AgoraReferee, agent: str) -> List[dict]:
    """GET /referee/contracts, the ones this fleet owns."""
    if not ref.contracts_enabled:
        return []
    return [c for c in ref.contract_desk.list() if c["owner"] == agent]


def contract_for(ref: AgoraReferee, agent: str, dest: str, comm: str, arrival: int) -> Optional[dict]:
    live = [c for c in my_contracts(ref, agent) if c["station_id"] == dest and c["instrument"] == comm
            and c["qty_remaining"] > 0 and c["deadline"] >= arrival]
    return max(live, key=lambda c: c["price"]) if live else None


def deliver_contracts(ref: AgoraReferee, agent: str, st: str, stats) -> None:
    """POST /referee/contracts/{id}/deliver for each owned contract here."""
    for c in sorted(my_contracts(ref, agent), key=lambda c: -c["price"]):
        have = available(ref, agent, c["instrument"]) - (FUEL_KEEP if c["instrument"] == "FUEL" else 0)
        if c["station_id"] != st or have <= 0:
            continue
        res = ref.contract_desk.deliver(agent, c["contract_id"], have)
        if res.get("kind") == "contract_deliver_ok":
            stats["contract_deliveries"] += 1


def pickups(ref: AgoraReferee, agent: str) -> List[dict]:
    """Peer trades this fleet bought and has yet to collect (#126)."""
    if not ref.peer_trades:
        return []
    return [e for e in ref.peer.list(status="accepted") if e["buyer"] == agent]


def buy_upgrades(ref: AgoraReferee, agent: str, cash_mult: float, stats) -> None:
    """POST /referee/upgrades/buy: the next tier of the first upgrade in
    UPGRADE_ORDER not yet maxed, once available CR is cash_mult x its price.
    At most one a round, never in debt. The board counts an upgrade at half
    its price, so the threshold keeps a fleet's working capital intact."""
    if not ref.upgrades_enabled or location(ref, agent) is None or debt(ref, agent) > 0:
        return
    for kind in UPGRADE_ORDER:
        if upgrades_mod.UNLOCKS.get(kind, 0) > ref.current_round:
            continue  # not on sale yet (the briefing shows the lock)
        t = ref.upgrades.tier(agent, kind)
        prices = UPGRADES[kind]["prices"]
        if t >= len(prices):
            continue
        if available(ref, agent, "CR") >= prices[t] * cash_mult:
            if ref.upgrades.buy(agent, kind).get("kind") == "upgrade_ok":
                stats["upgrades_bought"] = stats.get("upgrades_bought", 0) + 1
        return


def want_escort(ref: AgoraReferee, agent: str, st: str, dest: str, comm: str, qty: int) -> bool:
    """Escort a belt trip when the raid loss it saves beats the fee."""
    if not ref.piracy.enabled or qty <= 0:
        return False
    r = ref.current_round
    route = get_route(st, dest, r)
    toll = route.get("toll", 0) if route else 0
    if not toll:
        return False
    bare = ref.piracy.chance(agent, st, dest, True, comm, qty, False, r)
    guarded = ref.piracy.chance(agent, st, dest, True, comm, qty, True, r)
    fee = ref.piracy.escort_fee(comm, qty)
    saving = (bare["odds"] - guarded["odds"]) * bare["value"] * RAID_LOSS
    return saving > fee and available(ref, agent, "CR") >= fee + toll


def answer_demand(ref: AgoraReferee, agent: str, res: dict, stats, novice_rng: Optional[random.Random] = None) -> None:
    """POST /referee/piracy/{transit_id}/respond. Pay a ransom that is cheap
    against the fleet's cash, fight otherwise. A novice often never answers
    (the referee then fights for it at the next tick)."""
    pir = (res.get("payload") or {}).get("piracy") or {}
    d = pir.get("demand")
    if not d or d.get("status") != "pending":
        return
    if novice_rng is not None and novice_rng.random() < NOVICE_IGNORES_DEMAND:
        stats["demands_ignored"] = stats.get("demands_ignored", 0) + 1
        return
    cash = available(ref, agent, "CR")
    limit = cash if novice_rng is not None else cash * RANSOM_CASH_SHARE
    choice = "pay" if d["ransom"] <= limit else "fight"
    out = ref.piracy.respond(agent, d["transit_id"], choice)
    if out.get("kind") == "reject" and choice == "pay":
        ref.piracy.respond(agent, d["transit_id"], "fight")


def move(ref: AgoraReferee, agent: str, dest: str, comm: str, qty: int, stats, escort: bool = False,
         novice_rng: Optional[random.Random] = None) -> dict:
    """POST /stations/transit, then answer any pirate demand at once."""
    res = ref.initiate_transit(agent_id=agent, destination=dest, commodity=comm, cargo_qty=qty, escort=escort)
    if res.get("status") == "in_transit":
        stats["transits"] += 1
        answer_demand(ref, agent, res, stats, novice_rng)
    return res


@contextlib.contextmanager
def _live_defaults():
    """Hide AGORA_* from the environment: the simulator plays the live
    defaults, not whatever the caller's shell or CI happens to export."""
    saved = {k: os.environ.pop(k) for k in [k for k in os.environ if k.startswith("AGORA_")]}
    try:
        yield
    finally:
        os.environ.update(saved)


def start_game(seed: int, overrides: Optional[Dict[str, Any]] = None) -> AgoraReferee:
    """The live server's referee (build_referee_from_env) with a bare new game:
    what POST /referee/admin/new_game {"confirm": true, "seed": seed} gives.
    warmup_rounds is fixed so a seeded run repeats (left unset, new_game
    draws it from SystemRandom)."""
    with _live_defaults():
        ref = build_referee_from_env(":memory:", **(overrides or {}))
    ref.new_game(seed=seed, warmup_rounds=5)
    return ref


def fleet_views(ref: AgoraReferee) -> Dict[str, dict]:
    """GET /referee/depots as each fleet sees it (fogged when fog is on)."""
    if ref.fog:
        return {a: ref.fog.depot_view(ref, a)["stations"] for a in FLEETS}
    q = ref.get_depot_summary()["stations"]
    return {a: q for a in FLEETS}


# ------------------------------------------------------------ contract market (bots)

def contract_value(ref: AgoraReferee, agent: str, c: dict, view, one_hop: bool = False) -> int:
    """What contract c is worth to `agent`, from where it is and what it can
    see: buy the good at the cheapest station it knows of, fly it in, get
    paid, less the live lapse penalty on whatever part of it one hold cannot
    carry. Zero if it cannot make the deadline. one_hop: only goods bought
    where the fleet is (or will dock), which is all a Hauler plans. Uses the
    agent's own (possibly fogged) quotes, so two corps can honestly disagree."""
    loc = ref.get_vessel_location(agent)
    r = ref.current_round
    if loc.get("status") == "in_transit" and loc.get("transit"):
        st, r = loc["transit"]["destination"], max(r, loc["transit"]["arrival_round"])
    else:
        st = loc["station_id"]
    best = 0
    rem, comm = c["qty_remaining"], c["instrument"]
    cash = available(ref, agent, "CR") - (0 if c.get("owner") == agent else c.get("bond") or
                                           ref.contract_desk.bond_for(c["price"], rem))
    held = ref.get_balance(agent, comm) - (FUEL_KEEP if comm == "FUEL" else 0)
    # A Hauler never hauls FUEL as cargo (it burns it to fly), so to one it
    # a FUEL contract is worth only the FUEL it already holds at the station.
    for src in ([] if one_hop and comm == "FUEL" else [st] if one_hop else STATIONS):
        if src == c["station_id"]:
            continue
        ask = view[src][comm]["best_ask"]
        if not ask:
            continue
        leg1 = get_route(st, src, r) if st != src else {"rounds": 0, "fuel": 0, "toll": 0}
        leg2 = get_route(src, c["station_id"], r)
        slack = DEADLINE_SLACK if one_hop else 0
        if not leg1 or not leg2 or r + leg1["rounds"] + leg2["rounds"] + slack > c["deadline"]:
            continue
        qty = min(rem, 500, max(0, cash // ask))
        fuel_px = view[st]["FUEL"]["best_ask"] or 20
        cost = (leg1["fuel"] + leg2["fuel"]) * fuel_px + leg1.get("toll", 0) + leg2.get("toll", 0)
        short = ref.contract_desk.penalty_rate(agent) * c["price"] * (rem - qty)
        best = max(best, (c["price"] - ask) * qty - cost - short)
    if st == c["station_id"] and held > 0:
        qty = min(rem, held)
        best = max(best, (c["price"] - (view[st][comm]["best_bid"] or 0)) * qty
                   - ref.contract_desk.penalty_rate(agent) * c["price"] * (rem - qty))
    return int(best)


class ContractMarket:
    """How the contractor bots use the live contract desk each round:
    claim (first come, first served; the claim order rotates each round),
    list a contract they own for MIN_GAIN once they can no longer deliver
    it, and buy a listed contract they value MIN_GAIN above its price.
    Novices value contracts at 1.0-2.5x their worth, the overconfidence
    #112 sized."""

    MIN_GAIN = 200
    HOLD = 3  # rounds a buyer keeps a contract before it will resell

    def __init__(self, ref: AgoraReferee, kinds: Dict[str, str], seed: int):
        self.ref, self.kinds = ref, kinds
        self.rng = random.Random(seed * 31337)
        self.bought: Dict[str, int] = {}

    def _value(self, a: str, c: dict, views) -> int:
        v = contract_value(self.ref, a, c, views[a], one_hop=self.kinds.get(a) != "novice")
        if self.kinds.get(a) == "novice":
            v = int(max(v, c["price"] * min(c["qty_remaining"], 500) * 0.1) * self.rng.uniform(1.0, 2.5))
        return v

    def step(self, views: Dict[str, dict], active: List[str]) -> None:
        ref = self.ref
        if not ref.contracts_enabled:
            return
        desk = ref.contract_desk
        bidders = [a for a in active if self.kinds.get(a) in CONTRACTORS and debt(ref, a) <= 0]
        k = ref.current_round % max(1, len(bidders))
        for a in bidders[k:] + bidders[:k]:
            if len(my_contracts(ref, a)) >= contracts_mod.MAX_OPEN:
                continue
            best = None
            for c in desk.list():
                if c["owner"] or c["deadline"] < ref.current_round:
                    continue
                v = self._value(a, c, views)
                floor = 0 if self.kinds.get(a) == "novice" else CLAIM_MIN_VALUE
                if v > floor and (best is None or v > best[0]):
                    best = (v, c["contract_id"])
            if best:
                desk.claim(a, best[1])
        for c in desk.list():
            owner = c["owner"]
            if not owner or owner not in active or ref.current_round - self.bought.get(c["contract_id"], -99) < self.HOLD:
                continue
            own = contract_value(ref, owner, c, views[owner], one_hop=self.kinds.get(owner) != "novice")
            if own > 0:
                if c["list_price"]:
                    desk.list_for_sale(owner, c["contract_id"], None)  # back on track: keep it
                continue
            # It can no longer make this one: sell it before it lapses, cheap.
            ask = self.MIN_GAIN
            if c["list_price"] != ask:
                desk.list_for_sale(owner, c["contract_id"], ask)
            bids = sorted(((self._value(a, c, views), a) for a in bidders if a != owner), reverse=True)
            for v, a in bids:
                if v - ask < self.MIN_GAIN:
                    break
                if desk.buy(a, c["contract_id"]).get("kind") == "contract_buy_ok":
                    self.bought[c["contract_id"]] = ref.current_round
                    break


# ------------------------------------------------------------ peer trades at a distance (bots)

class PeerMarket:
    """How the flying bots use the live peer desk (agora/peer.py) each round.

    Seller: docked at S with goods, offers a lot at the midpoint of its
    floor (what the goods are worth to it otherwise: the depot bid at S, or
    the best bid elsewhere less the trip) and the depot ask at S, so a buyer
    saves against the depot. Buyer: any flying fleet, wherever it is, accepts
    if flying to S to collect and hauling on to its best market still pays at
    the offer price, per its own (possibly fogged) view; it then flies to S to
    collect (#126). Offers nobody took are cancelled at once, so the goods are
    back before the seller plans its own trip."""

    MIN_LOT, MAX_LOT, CR_RESERVE, FUEL_RESERVE = 20, 500, 300, 100

    def __init__(self, ref: AgoraReferee, kinds: Dict[str, str]):
        self.ref, self.kinds = ref, kinds
        self.remote = 0

    @staticmethod
    def _unit_trip_cost(view, st: str, dest: str, qty: int, r: int) -> float:
        route = get_route(st, dest, r)
        if not route or qty <= 0:
            return float("inf")
        return (route["fuel"] * (view[st]["FUEL"]["best_ask"] or 20) + route.get("toll", 0)) / qty

    def _best_haul(self, view, st: str, comm: str, qty: int, r: int) -> float:
        here = view[st][comm]["best_bid"] or 0
        away = max(((view[d][comm]["best_bid"] or 0) - self._unit_trip_cost(view, st, d, qty, r)
                    for d in STATIONS if d != st), default=0)
        return max(here, away)

    def step(self, views: Dict[str, dict], active: List[str]) -> None:
        ref, r = self.ref, self.ref.current_round
        if not ref.peer_trades:
            return
        traders = [a for a in active if self.kinds.get(a) in CONTRACTORS]
        mine = []
        for seller in traders:
            st = location(ref, seller)
            if st is None:
                continue
            sv = views[seller]
            owed = {}
            for c in my_contracts(ref, seller):  # goods it holds for its own contracts are not for sale
                owed[c["instrument"]] = owed.get(c["instrument"], 0) + c["qty_remaining"]
            for comm in ("FRAG", "FOOD", "ORE", "FUEL"):
                have = min(self.MAX_LOT, available(ref, seller, comm) - owed.get(comm, 0)
                           - (self.FUEL_RESERVE if comm == "FUEL" else 0))
                ask = sv[st][comm]["best_ask"]
                if have < self.MIN_LOT or not ask:
                    continue
                floor = self._best_haul(sv, st, comm, have, r)
                price = max(int((floor + ask) // 2), int(floor) + 1)
                if price >= ask:
                    continue
                res = ref.peer.offer(seller, st, comm, have, price)
                if res.get("kind") == "peer_offer_ok":
                    mine.append(res["payload"]["escrow_id"])
        # Never rely on list order: escrow ids are random (uuid4).
        offers = sorted((o for o in ref.peer.list(status="offered") if o["escrow_id"] in mine),
                        key=lambda o: (o["station_id"], o["instrument"], o["seller"], -o["qty"], o["price"]))
        for o in offers:
            st, comm, qty, price = o["station_id"], o["instrument"], o["qty"], o["price"]
            best = None
            for buyer in traders:
                if buyer == o["seller"] or available(ref, buyer, "CR") - self.CR_RESERVE < qty * price:
                    continue
                loc = ref.get_vessel_location(buyer)
                at = loc["transit"]["destination"] if loc.get("status") == "in_transit" else loc["station_id"]
                bv = views[buyer]
                reach = 0.0 if at == st else self._unit_trip_cost(bv, at, st, qty, r)
                gain = max((bv[d][comm]["best_bid"] or 0) - self._unit_trip_cost(bv, st, d, qty, r)
                           for d in STATIONS if d != st) - reach
                if gain > price and (best is None or (gain, buyer) > best[:2]):
                    best = (gain, buyer, at != st or loc.get("status") != "docked")
            if best and ref.peer.accept(best[1], o["escrow_id"]).get("kind") == "peer_accept_ok":
                self.remote += best[2]
        for o in ref.peer.list(status="offered"):
            if o["escrow_id"] in mine:
                ref.peer.cancel(o["seller"], o["escrow_id"])


# ------------------------------------------------------------ strategies

class Hauler:
    """Greedy one-hop arbitrage: sell cargo on arrival, then buy the single
    best (commodity, destination) margin net of fuel and toll, and fly. A
    hauler with a peer pickup waiting elsewhere flies there next (#126)."""

    UPGRADE_CASH_MULT = UPGRADE_CASH_MULT

    def __init__(self, agent: str, tolerate_halts: bool = False):
        self.agent = agent
        self.stranded = False
        # strict: never place an order outside the circuit-breaker band.
        # tolerant: buy at the depot ask even if that trips a halt, then wait
        # for the reopening auction to fill (what a live player has to do
        # for cheap goods, whose 1-CR depot spread exceeds a 10% band).
        self.tolerate_halts = tolerate_halts
        self.plan = None  # (dest, comm) while waiting on an auction fill

    def _hauling_value(self, quotes, st: str, comm: str) -> int:
        """Best bid for `comm` at any other station."""
        return max((quotes[d][comm]["best_bid"] or 0) for d in STATIONS if d != st)

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        st = location(ref, self.agent)
        if st is None:
            return
        inv = inventory(ref, self.agent)

        # Waiting on a halted book's reopening auction: keep the resting bid.
        if self.plan and (ref.circuit_breaker.is_halted(st, self.plan[1])
                          or ref.circuit_breaker.is_halted(st, "FUEL")):
            return
        cancel_all(ref, self.agent)
        if self.plan and inv[self.plan[1]] > 0:
            dest, comm = self.plan
            self.plan = None
            self._fly(ref, st, dest, comm, inv, stats)
            return
        self.plan = None

        deliver_contracts(ref, self.agent, st, stats)
        inv = inventory(ref, self.agent)

        # 1. Sell cargo here only if this is the best market for it; otherwise
        #    it is cargo to haul (e.g. a per-planet genesis export).
        for comm in ("FRAG", "FOOD", "ORE"):
            qty = available(ref, self.agent, comm)
            bid = quotes[st][comm]["best_bid"]
            if qty > 0 and bid and bid >= self._hauling_value(quotes, st, comm) and band_ok(ref, st, comm, bid):
                order(ref, self.agent, "ask", min(qty, quotes[st][comm]["bid_depth"] or qty), bid, comm, st, "sell")
        self._buy_upgrades(ref, stats)
        inv = inventory(ref, self.agent)
        for comm in ("FRAG", "FOOD", "ORE"):
            if inv[comm] > 0:
                dest = max((d for d in STATIONS if d != st), key=lambda d: quotes[d][comm]["best_bid"] or 0)
                c = self._contract_run(ref, st, comm)
                self._fly(ref, st, c["station_id"] if c else dest, comm, inv, stats)
                return

        # 2. Pick the best margin from here: a contract it owns first, then
        #    toward a peer pickup if one waits, then anywhere.
        waiting = sorted({p["station_id"] for p in pickups(ref, self.agent)} - {st})
        owned = [c["station_id"] for c in sorted(my_contracts(ref, self.agent), key=lambda c: -c["price"])
                 if c["station_id"] != st and self._contract_run(ref, st, c["instrument"], c["station_id"])]
        best = None
        for dest in (owned[:1] or waiting[:1] or STATIONS):
            if dest == st:
                continue
            route = get_route(st, dest, ref.current_round)
            if not route:
                continue
            for comm in ("FRAG", "FOOD", "ORE"):
                ask = quotes[st][comm]["best_ask"]
                bid = quotes[dest][comm]["best_bid"] or 0
                cap = 500
                c = contract_for(ref, self.agent, dest, comm, ref.current_round + route["rounds"])
                if c and c["price"] > bid:
                    bid, cap = c["price"], min(500, c["qty_remaining"])
                if not ask or not bid:
                    continue
                if not band_ok(ref, st, comm, ask) and not self.tolerate_halts:
                    stats["band_blocked"] += 1
                    continue
                qty = min(quotes[st][comm]["ask_depth"] or 0, cap, max(0, (inv["CR"] - 200) // ask))
                if qty <= 0:
                    continue
                fuel_cost = route["fuel"] * (quotes[st]["FUEL"]["best_ask"] or 20)
                profit = (bid - ask) * qty - fuel_cost - route.get("toll", 0)
                per_round = profit / max(1, route["rounds"])
                if profit > 0 and (best is None or per_round > best[0]):
                    best = (per_round, dest, comm, qty, ask, route)
        if best is None:
            if waiting:
                stats["pickup_trips"] = stats.get("pickup_trips", 0) + 1
                self._fly(ref, st, waiting[0], "FRAG", inv, stats, empty=True)
            return
        _, dest, comm, qty, ask, route = best
        if waiting:
            stats["pickup_trips"] = stats.get("pickup_trips", 0) + 1

        # 3. Buy; fly now, or wait for the auction if the buy tripped a halt.
        res = order(ref, self.agent, "bid", qty, ask, comm, st, "buy")
        if res.get("status") == "circuit_breaker_halted":
            stats["halts_caused"] += 1
            self.plan = (dest, comm)
            return
        self._fly(ref, st, dest, comm, inventory(ref, self.agent), stats)

    # Purchase decisions, as methods so tools/dominance.py can swap one
    # fleet's policy without copying the hauling loop.
    def _buy_upgrades(self, ref: AgoraReferee, stats) -> None:
        buy_upgrades(ref, self.agent, self.UPGRADE_CASH_MULT, stats)

    def _want_escort(self, ref: AgoraReferee, st: str, dest: str, comm: str, qty: int) -> bool:
        return want_escort(ref, self.agent, st, dest, comm, qty)

    def _contract_run(self, ref: AgoraReferee, st: str, comm: str, dest: Optional[str] = None) -> Optional[dict]:
        """An owned contract for `comm` it can still reach in time from here."""
        for c in sorted(my_contracts(ref, self.agent), key=lambda c: -c["price"]):
            if c["instrument"] != comm or c["station_id"] == st or (dest and c["station_id"] != dest):
                continue
            route = get_route(st, c["station_id"], ref.current_round)
            if route and ref.current_round + route["rounds"] <= c["deadline"]:
                return c
        return None

    def _fly(self, ref: AgoraReferee, st: str, dest: str, comm: str, inv, stats, empty: bool = False) -> None:
        route = get_route(st, dest, ref.current_round)
        need = route["fuel"] - inv["FUEL"]
        if need > 0:
            fa = ref.get_depot_summary()["stations"][st]["FUEL"]["best_ask"]
            if fa and inv["CR"] >= need * fa:
                if band_ok(ref, st, "FUEL", fa):
                    order(ref, self.agent, "bid", need, fa, "FUEL", st, "fuel")
                elif self.tolerate_halts:
                    res = order(ref, self.agent, "bid", need, fa, "FUEL", st, "fuel")
                    if res.get("status") == "circuit_breaker_halted":
                        stats["halts_caused"] += 1
                        self.plan = (dest, comm)
                        return
                else:
                    stats["band_blocked"] += 1
            if ref.get_balance(self.agent, "FUEL") < route["fuel"]:
                self.stranded = True
                stats["stranded_events"] += 1
                return
        self.stranded = False
        held = 0 if empty else ref.get_balance(self.agent, comm)
        if held > 0 or empty:
            cancel_all(ref, self.agent)
            escort = self._want_escort(ref, st, dest, comm, held)
            move(ref, self.agent, dest, comm, held, stats, escort=escort)


class Privateer(Hauler):
    """A hauler that also keeps privateers (POST /referee/privateers) on the
    richest rival whenever it has 3x their fee in cash and none under contract."""

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        self._hire(ref, stats)
        super().act(ref, quotes, stats)

    def _hire(self, ref: AgoraReferee, stats) -> None:
        if not ref.piracy.enabled or debt(ref, self.agent) > 0:
            return
        if available(ref, self.agent, "CR") < piracy_mod.PRIV_COST * 3:
            return
        # GET /referee/piracy as this fleet sees it: its own contracts show their sponsor.
        if any(c["sponsor"] == self.agent for c in ref.piracy.active_contracts(viewer=self.agent)):
            return
        nw = {e["agent_id"]: e["net_worth"] for e in ref.get_leaderboard()}
        for target in sorted((b for b in FLEETS if b != self.agent and not ref.fleet_out(b)),
                             key=lambda b: -nw.get(b, 0)):
            if ref.piracy.hire(self.agent, target).get("kind") == "privateer_hire_ok":
                return


class Maker:
    """A market maker (#162). Sets up at a station where the NPC order flow
    (agora/order_flow.py, GET /referee/order-flow) is two-sided for every
    good: Luna or Mars, where no good is at its cheapest or dearest, so
    buyers and sellers arrive at about the same rate. It flies there first
    if it starts elsewhere. Each round it cancels and requotes every good:
    one CR inside the depot's bid and ask where the spread allows, joining
    them otherwise (a fleet order fills before the depot at the same
    price). Clip size is the station's expected flow a side, so it can take
    all of it. Inventory is bounded: no bid above CAP_ROUNDS rounds of flow
    held, the ask leans one CR lower above half of that, and nothing is
    bought with the last CR_RESERVE. Never claims contracts or hauls.

    Before #162 the maker only joined the depot's quotes at home with a
    fixed clip; "inside_maker" (the "market" scenario) still quotes at home
    with a fixed clip (relocate=False)."""

    CAP_ROUNDS, CR_RESERVE = 6, 500

    def __init__(self, agent: str, clip: int = 50, inside: bool = True, relocate: bool = True):
        self.agent = agent
        self.clip = clip
        self.inside = inside
        self.relocate = relocate
        self.venue: Optional[str] = None

    @staticmethod
    def venues() -> List[str]:
        """Stations that are neither the cheapest nor the dearest for any good."""
        ends = set()
        for c in TRADED:
            ends.add(min(STATIONS, key=lambda st: BASE_PRICES[st][c]))
            ends.add(max(STATIONS, key=lambda st: BASE_PRICES[st][c]))
        return [st for st in STATIONS if st not in ends] or list(STATIONS)

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        st = location(ref, self.agent)
        if st is None:
            return
        if self.relocate and self.venue is None:
            self.venue = min(self.venues(), key=lambda v: (0 if v == st else get_route(st, v, 0)["rounds"], v))
        if self.relocate and st != self.venue:
            cancel_all(ref, self.agent)
            route = get_route(st, self.venue, ref.current_round)
            if route and ref.get_balance(self.agent, "FUEL") >= route["fuel"]:
                move(ref, self.agent, self.venue, "FRAG", 0, stats)
                return
            self.venue = st  # cannot get there: make markets here
        cancel_all(ref, self.agent)
        flow = ref.order_flow if getattr(ref, "order_flow", None) is not None and ref.order_flow.enabled else None
        cash = available(ref, self.agent, "CR") - self.CR_RESERVE
        for comm in TRADED:
            q = quotes[st][comm]
            bid, ask = q["best_bid"], q["best_ask"]
            if not bid or not ask:
                continue
            if self.inside and ask - bid >= 3:
                bid, ask = bid + 1, ask - 1
            if flow is not None and self.relocate:
                exp = flow.expected(st, comm)
                clip_b, clip_a = max(1, int(exp["sell"] * 1.5)), max(1, int(exp["buy"] * 1.5))
                cap = int(max(exp["sell"], exp["buy"]) * self.CAP_ROUNDS)
            else:
                clip_b = clip_a = self.clip
                cap = 10 ** 9
            keep = FUEL_KEEP if comm == "FUEL" else 0
            held = available(ref, self.agent, comm) - keep
            if held > cap // 2 and ask - 1 > bid:
                ask -= 1
            want = min(clip_b, max(0, cap - held))
            if cash > 0 and want > 0 and band_ok(ref, st, comm, bid):
                n = min(want, cash // bid)
                if n > 0 and order(ref, self.agent, "bid", n, bid, comm, st, "mb").get("kind") != "reject":
                    cash -= n * bid
            if held > 0 and band_ok(ref, st, comm, ask):
                order(ref, self.agent, "ask", min(clip_a, held), ask, comm, st, "ma")


class Idler:
    def __init__(self, agent: str):
        self.agent = agent

    def act(self, ref, quotes, stats) -> None:
        return


class Novice:
    """A zero-context LLM player working from /referee/briefing.

    It sees what the briefing shows (every station's depot quotes, routes,
    its own holdings), so information is not the handicap; execution is.
    Each knob below is a behaviour tonight's #agent-chat thread predicted:
      p_bad         order rejected outright (wrong station, no AT, bad qty)
      p_inside      prices inside the spread instead of hitting the depot,
                    which rests on the book where another fleet can take it
      p_keep        leaves last round's resting orders up instead of cancelling
      p_no_fuel     issues MOVE without topping up FUEL first
      temperature   softmax over route margins instead of the single best
    It buys upgrades on impulse (a small cash reserve), never escorts, and
    ignores half the pirate demands it gets.
    """

    def __init__(self, agent: str, seed: int = 0, p_bad: float = 0.12, p_inside: float = 0.4,
                 p_keep: float = 0.5, p_no_fuel: float = 0.15, temperature: float = 0.35):
        self.agent = agent
        self.rng = random.Random(f"{agent}-{seed}")
        self.p_bad, self.p_inside, self.p_keep = p_bad, p_inside, p_keep
        self.p_no_fuel, self.temperature = p_no_fuel, temperature
        self.dest = None  # where the cargo it is buying is meant to go

    def _price(self, side: str, bid: Optional[int], ask: Optional[int]) -> Optional[int]:
        """Hit the depot, or pick a price strictly inside the spread."""
        touch = ask if side == "bid" else bid
        if touch is None:
            return None
        if bid and ask and ask - bid >= 2 and self.rng.random() < self.p_inside:
            return self.rng.randint(bid + 1, ask - 1)
        return touch

    def _bad_order(self, ref: AgoraReferee, st: str, stats) -> None:
        """What a confused first-timer sends: an order for a station it is not
        docked at (the missing-AT case), or one it cannot pay for."""
        stats["bad_orders"] = stats.get("bad_orders", 0) + 1
        comm = self.rng.choice(TRADED)
        if self.rng.random() < 0.6:
            other = self.rng.choice([x for x in STATIONS if x != st])
            order(ref, self.agent, "bid", 100, 20, comm, other, "bad")
        else:
            order(ref, self.agent, "ask", 10 ** 6, 20, comm, st, "bad")

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        st = location(ref, self.agent)
        if st is None:
            return
        if self.rng.random() < self.p_bad:
            self._bad_order(ref, st, stats)
            return
        deliver_contracts(ref, self.agent, st, stats)
        if self.rng.random() >= self.p_keep:
            cancel_all(ref, self.agent)
        buy_upgrades(ref, self.agent, NOVICE_UPGRADE_CASH_MULT, stats)
        inv = inventory(ref, self.agent)

        # Holding cargo: sell it here if this station pays the most, else fly.
        for comm in ("FRAG", "FOOD", "ORE"):
            if inv[comm] <= 0:
                continue
            best_dest = max(STATIONS, key=lambda d: quotes[d][comm]["best_bid"] or 0)
            target = self.dest or best_dest
            if st == target or st == best_dest:
                q = quotes[st][comm]
                px = self._price("ask", q["best_bid"], q["best_ask"])
                have = available(ref, self.agent, comm)
                if px and have > 0 and band_ok(ref, st, comm, px):
                    order(ref, self.agent, "ask", have, px, comm, st, "sell")
                self.dest = None
                return
            self._fly(ref, st, target, comm, inv, quotes, stats)
            return

        # A peer pickup waiting elsewhere: go and get it (#126).
        waiting = sorted({p["station_id"] for p in pickups(ref, self.agent)} - {st})

        # Empty hold: choose a route by softmax over profit per round.
        options = []
        for dest in (waiting[:1] or STATIONS):
            route = get_route(st, dest, ref.current_round) if dest != st else None
            if not route:
                continue
            for comm in ("FRAG", "FOOD", "ORE"):
                ask, bid = quotes[st][comm]["best_ask"], quotes[dest][comm]["best_bid"]
                if not ask or not bid:
                    continue
                qty = min(500, max(0, (inv["CR"] - 300) // ask))
                if qty <= 0:
                    continue
                fuel_cost = route["fuel"] * (quotes[st]["FUEL"]["best_ask"] or 20)
                profit = (bid - ask) * qty - fuel_cost - route.get("toll", 0)
                if profit > 0:
                    options.append((profit / route["rounds"], dest, comm, qty))
        if not options:
            if waiting:
                stats["pickup_trips"] = stats.get("pickup_trips", 0) + 1
                self._fly(ref, st, waiting[0], "FRAG", inv, quotes, stats, empty=True)
            return
        if waiting:
            stats["pickup_trips"] = stats.get("pickup_trips", 0) + 1
        top = max(o[0] for o in options)
        weights = [math.exp((o[0] - top) / (self.temperature * top)) for o in options]
        _, dest, comm, qty = self.rng.choices(options, weights=weights)[0]
        q = quotes[st][comm]
        px = self._price("bid", q["best_bid"], q["best_ask"])
        if px is None or not band_ok(ref, st, comm, px):
            stats["band_blocked"] += 1
            return
        order(ref, self.agent, "bid", qty, px, comm, st, "buy")
        self.dest = dest
        held = ref.get_balance(self.agent, comm)
        if held > 0 and px >= (q["best_ask"] or 10 ** 9):
            self._fly(ref, st, dest, comm, inventory(ref, self.agent), quotes, stats)
        # else: the bid rests inside the spread; next round it flies whatever filled

    def _fly(self, ref: AgoraReferee, st: str, dest: str, comm: str, inv, quotes, stats, empty: bool = False) -> None:
        route = get_route(st, dest, ref.current_round)
        need = route["fuel"] - inv["FUEL"]
        if need > 0 and self.rng.random() >= self.p_no_fuel:
            fa = quotes[st]["FUEL"]["best_ask"]
            if fa and inv["CR"] >= need * fa and band_ok(ref, st, "FUEL", fa):
                order(ref, self.agent, "bid", need, fa, "FUEL", st, "fuel")
        held = 0 if empty else ref.get_balance(self.agent, comm)
        cancel_all(ref, self.agent)
        t = move(ref, self.agent, dest, comm, held, stats, novice_rng=self.rng)
        if t.get("status") != "in_transit":
            stats["rejected_moves"] = stats.get("rejected_moves", 0) + 1


# ------------------------------------------------------------ fleet stocks (#119)

EQ_SYM = {"amos": "EQ_AMOS", "marvin": "EQ_MARV", "zero": "EQ_ZERO", "aerial": "EQ_AERL"}


def stock_navs(ref: AgoraReferee) -> Dict[str, dict]:
    base = {e["agent_id"]: e["net_worth"] - e.get("stocks_value", 0) for e in ref.get_leaderboard()}
    return ref.stock_marks(base)


def stock_value(ref: AgoraReferee, agent: str, marks: Dict[str, dict], basis: str = "nav") -> float:
    """Rival shares held, at NAV (or the board mark). Own shares count for nothing, as on the leaderboard."""
    return sum(ref.get_balance(agent, sym) * marks[sym][basis] for a, sym in EQ_SYM.items() if a != agent)


def cancel_stock_orders(ref: AgoraReferee, agent: str) -> None:
    for sym in EQ_SYM.values():
        b = ref.books["ceres"][sym]
        for o in list(b.bids) + list(b.asks):
            if o.agent_id == agent:
                ref.cancel_order(agent, o.order_id)


class EquityLiquidity:
    """STAND-IN for other players on the stock exchange, on top of the live
    exchange's own quotes: every fleet that is not a stock trader quotes the
    rivals' shares it holds at NAV x (1 +/- SPREAD), DEPTH shares a side a
    round, from its own CR and shares, through ordinary orders. Off unless
    --equity-mm; an assumption about how LLM players will behave."""

    def __init__(self, spread: float, depth: int):
        self.spread, self.depth = spread, depth

    def quote(self, ref: AgoraReferee, providers: List[str]) -> None:
        marks = stock_navs(ref)
        for a in providers:
            cancel_stock_orders(ref, a)
            for issuer, sym in EQ_SYM.items():
                if issuer == a:
                    continue
                nav = marks[sym]["nav"]
                ask = max(2, int(round(nav * (1 + self.spread))))
                bid = max(1, min(ask - 1, int(nav * (1 - self.spread))))
                held = available(ref, a, sym)
                if held > 0:
                    order(ref, a, "ask", min(self.depth, held), ask, sym, "ceres", "eqmm")
                if available(ref, a, "CR") > 2000 + bid * self.depth:
                    order(ref, a, "bid", self.depth, bid, sym, "ceres", "eqmm")


class StockTrader:
    """Trades rival stocks on the exchange, never goods or contracts (#119, #162).

    Fair value is the exchange's own anchor (agora/exchange.py, documented
    there): the mean of the issuer's NAV over the last ANCHOR_ROUNDS rounds,
    carried forward along that window's trend. NAV is public (leaderboard
    net worth / shares), so a player can rebuild it; the exchange's price
    reverts toward it at REVERSION a round. The trader trades that gap:

      mid below fair by ENTRY or more   buy, up to the exchange's depth a round
      mid above fair by ENTRY or more   sell, down to zero shares
      long above its genesis shares     sell back to them once mid >= fair (1 + EXIT)
      short of them                     buy back once mid <= fair (1 - EXIT)

    ENTRY clears the round trip through the exchange's spread (2 x 3%).
    Each name may hold up to MAX_POS shares, and one buy spends at most
    STAKE of the trader's cash. It sells its genesis FRAG and FUEL at its
    home depot in the first rounds, since it never flies: that is its
    capital. Only takes liquidity; any rest is cancelled at once, so every
    stock fill happens inside act() and stock_cash measures exactly the CR
    its stock trades made (goods sales are kept out of it).

    Before #162 it used its own 30-round fair value, bought only at 8% below
    it, never sold its goods, and lost about 9k over 300 rounds at every
    setting tried (#162 sweep): too naive to stand for the style."""

    ENTRY, EXIT, STAKE, MAX_POS = 0.08, 0.0, 0.5, 400

    def __init__(self, agent: str):
        self.agent = agent
        self.hist: Dict[str, List[float]] = {s: [] for s in EQ_SYM.values()}
        self.stock_cash = 0
        self.base: Dict[str, int] = {}

    def fair(self, sym: str) -> Optional[float]:
        """The exchange's anchor, from the NAVs this trader has seen."""
        h = self.hist[sym][-exchange_mod.ANCHOR_ROUNDS:]
        if len(h) < 4:
            return None
        anchor = sum(h) / len(h)
        k = len(h) // 2
        slope = (sum(h[-k:]) / k - sum(h[:k]) / k) / (len(h) - k)
        return max(1.0, anchor + slope * (len(h) - 1) / 2)

    def _liquidate(self, ref: AgoraReferee, quotes) -> None:
        st = location(ref, self.agent)
        if st is None:
            return
        cancel_all(ref, self.agent)
        for comm in TRADED:
            have = available(ref, self.agent, comm)
            bid = quotes[st][comm]["best_bid"]
            if have > 0 and bid and band_ok(ref, st, comm, bid):
                order(ref, self.agent, "ask", min(have, quotes[st][comm]["bid_depth"] or have), bid, comm, st, "liq")

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        self._liquidate(ref, quotes)
        marks = stock_navs(ref)
        before = ref.get_balance(self.agent, "CR")
        for issuer, sym in EQ_SYM.items():
            self.hist[sym].append(marks[sym]["nav"])
            if issuer == self.agent:
                continue
            fair = self.fair(sym)
            if fair is None:
                continue
            base = self.base.setdefault(sym, ref.get_balance(self.agent, sym))
            book = ref.books["ceres"][sym]
            ask, bid = book.best_ask(), book.best_bid()
            pos = available(ref, self.agent, sym)
            # Each side on its own: the exchange stops quoting an ask once it
            # has sold all its shares, and a bid once it is out of CR.
            half = exchange_mod.DEFAULT_SPREAD
            buy_to = sell_to = None
            if ask is not None and ask <= fair * (1 - self.ENTRY + half):
                buy_to = self.MAX_POS
            elif ask is not None and pos < base and ask <= fair * (1 - self.EXIT + half):
                buy_to = base
            if bid is not None and bid >= fair * (1 + self.ENTRY - half):
                sell_to = 0
            elif bid is not None and pos > base and bid >= fair * (1 + self.EXIT - half):
                sell_to = base
            if buy_to is not None and buy_to > pos:
                qty = min(buy_to - pos, int(available(ref, self.agent, "CR") * self.STAKE) // ask)
                if qty > 0:
                    order(ref, self.agent, "bid", qty, ask, sym, "ceres", "stk")
            elif sell_to is not None and sell_to < pos:
                order(ref, self.agent, "ask", pos - sell_to, bid, sym, "ceres", "stk")
            cancel_stock_orders(ref, self.agent)
        self.stock_cash += ref.get_balance(self.agent, "CR") - before


class DayTrader:
    """Never flies. Trades its docked station's goods from price history
    alone, which is what a public /referee/history would give a player:
    buys a good when its ask is DIP below the good's WINDOW-round average
    mid, sells what it bought once the bid is back at that average, or
    after MAX_HOLD rounds whatever the price."""

    WINDOW, DIP, MAX_HOLD, LOT_CR = 20, 0.08, 12, 10 ** 9  # LOT_CR: all its cash, like a hauler

    def __init__(self, agent: str):
        self.agent = agent
        self.mids: Dict[str, List[float]] = {}
        self.pos: Dict[str, List[int]] = {}  # comm -> [qty, rounds_held]

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        st = location(ref, self.agent)
        if st is None:
            return
        cancel_all(ref, self.agent)
        for comm in ("FRAG", "FOOD", "ORE"):
            q = quotes[st][comm]
            bid, ask = q["best_bid"], q["best_ask"]
            if not bid or not ask:
                continue
            hist = self.mids.setdefault(comm, [])
            hist.append((bid + ask) / 2)
            del hist[:-self.WINDOW]
            if len(hist) < self.WINDOW:
                continue
            avg = sum(hist) / len(hist)
            qty, held = self.pos.get(comm, [0, 0])
            qty = min(qty, available(ref, self.agent, comm))  # a distress sale may have taken some
            if qty > 0:
                held += 1
                if (bid >= avg or held >= self.MAX_HOLD) and band_ok(ref, st, comm, bid):
                    before = ref.get_balance(self.agent, comm)
                    order(ref, self.agent, "ask", qty, bid, comm, st, "dtsell")
                    sold = before - ref.get_balance(self.agent, comm)
                    qty -= sold
                    stats["day_trades"] = stats.get("day_trades", 0) + (1 if sold else 0)
                self.pos[comm] = [qty, held if qty else 0]
            elif ask <= avg * (1 - self.DIP) and band_ok(ref, st, comm, ask):
                n = min(q["ask_depth"] or 0, self.LOT_CR // ask, max(0, (available(ref, self.agent, "CR") - 500) // ask))
                if n > 0:
                    before = ref.get_balance(self.agent, comm)
                    order(ref, self.agent, "bid", n, ask, comm, st, "dtbuy")
                    got = ref.get_balance(self.agent, comm) - before
                    if got > 0:
                        self.pos[comm] = [got, 0]


class Saboteur(Hauler):
    """A hauler that also conducts industrial sabotage (agora/covert.py,
    POST /referee/covert/sabotage) against rivals (#174). Strikes when it has
    enough cash to pay the fee and withstand a potential trace fine, targeting
    the richest rival or a rival in flight with cargo."""

    CASH_BUFFER = 25_000
    STRIKE_COOLDOWN = 25

    def __init__(self, agent: str, tolerate_halts: bool = False):
        super().__init__(agent, tolerate_halts=tolerate_halts)
        self.last_strike = -999

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        self._strike(self, ref, stats)
        super().act(ref, quotes, stats)

    @classmethod
    def _strike(cls, fleet, ref: AgoraReferee, stats) -> bool:
        if not getattr(ref, "covert", None) or not ref.covert.enabled or debt(ref, fleet.agent) > 0:
            return False
        if available(ref, fleet.agent, "CR") < cls.CASH_BUFFER:
            return False
        last = getattr(fleet, "last_strike", -999)
        if ref.current_round - last < cls.STRIKE_COOLDOWN:
            return False

        nw = {e["agent_id"]: e["net_worth"] for e in ref.get_leaderboard()}
        rivals = sorted((b for b in FLEETS if b != fleet.agent and not ref.fleet_out(b)),
                        key=lambda b: -nw.get(b, 0))
        for target in rivals:
            res = ref.covert.execute_sabotage(fleet.agent, target, mode="auto")
            if res.get("kind") == "sabotage_ok":
                fleet.last_strike = ref.current_round
                stats["sabotages"] = stats.get("sabotages", 0) + 1
                if res["payload"].get("traced"):
                    stats["sabotages_traced"] = stats.get("sabotages_traced", 0) + 1
                return True
        return False


class Spy(Hauler):
    """A hauler that conducts corporate espionage (agora/covert.py,
    POST /referee/covert/wiretap) and uses intercepted intelligence
    (GET /referee/covert/intel) to execute high-impact targeted sabotages
    against rivals when they are in flight with valuable cargo (#174)."""

    WIRETAP_CASH_BUFFER = 25_000
    SABOTAGE_CASH_BUFFER = 25_000
    WIRETAP_COOLDOWN = 20
    STRIKE_COOLDOWN = 25

    def __init__(self, agent: str, tolerate_halts: bool = False):
        super().__init__(agent, tolerate_halts=tolerate_halts)
        self.last_strike = -999
        self.last_tap_round = -999

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        self._espionage(self, ref, stats)
        super().act(ref, quotes, stats)

    @classmethod
    def _plant(cls, fleet, ref: AgoraReferee, stats) -> bool:
        if not getattr(ref, "covert", None) or not ref.covert.enabled or debt(ref, fleet.agent) > 0:
            return False
        if available(ref, fleet.agent, "CR") < cls.WIRETAP_CASH_BUFFER:
            return False
        last_tap = getattr(fleet, "last_tap_round", -999)
        if ref.current_round - last_tap < cls.WIRETAP_COOLDOWN:
            return False
        nw = {e["agent_id"]: e["net_worth"] for e in ref.get_leaderboard()}
        rivals = sorted((b for b in FLEETS if b != fleet.agent and not ref.fleet_out(b)),
                        key=lambda b: -nw.get(b, 0))
        for target in rivals:
            if not ref.covert.has_wiretap(fleet.agent, target):
                res = ref.covert.plant_wiretap(fleet.agent, target)
                if res.get("kind") == "wiretap_ok":
                    fleet.last_tap_round = ref.current_round
                    stats["wiretaps"] = stats.get("wiretaps", 0) + 1
                    return True
        return False

    @classmethod
    def _espionage(cls, fleet, ref: AgoraReferee, stats) -> None:
        if not getattr(ref, "covert", None) or not ref.covert.enabled or debt(ref, fleet.agent) > 0:
            return
        cls._plant(fleet, ref, stats)

        if available(ref, fleet.agent, "CR") < cls.SABOTAGE_CASH_BUFFER:
            return
        last = getattr(fleet, "last_strike", -999)
        if ref.current_round - last < cls.STRIKE_COOLDOWN:
            return

        tapped = ref.covert.tapped_targets(fleet.agent)
        for target in tapped:
            intel = ref.covert.get_intel(fleet.agent, target)
            if intel.get("kind") != "intel_ok":
                continue
            payload = intel.get("payload", {})
            loc = payload.get("location", {})
            cargo = payload.get("cargo", {})
            cargo_qty = sum(cargo.get(c, 0) for c in ("FRAG", "FOOD", "ORE"))
            if loc.get("status") == "in_transit" or cargo_qty >= 15:
                res = ref.covert.execute_sabotage(fleet.agent, target, mode="auto")
                if res.get("kind") == "sabotage_ok":
                    fleet.last_strike = ref.current_round
                    stats["sabotages"] = stats.get("sabotages", 0) + 1
                    if res["payload"].get("traced"):
                        stats["sabotages_traced"] = stats.get("sabotages_traced", 0) + 1
                    return


def build_fleet(agent: str, kind: str, seed: int, mode: str):
    if kind == "hauler":
        return Hauler(agent, tolerate_halts=(mode == "tolerant"))
    if kind == "privateer":
        return Privateer(agent, tolerate_halts=(mode == "tolerant"))
    if kind == "novice":
        return Novice(agent, seed=seed)
    if kind == "inside_maker":
        return Maker(agent, clip=100, inside=True, relocate=False)
    if kind == "maker":
        return Maker(agent)
    if kind == "saboteur":
        return Saboteur(agent, tolerate_halts=(mode == "tolerant"))
    if kind == "spy":
        return Spy(agent, tolerate_halts=(mode == "tolerant"))
    return {"idler": Idler, "stock_trader": StockTrader, "daytrader": DayTrader}[kind](agent)


# ------------------------------------------------------------ genesis

def apply_planet_genesis(ref: AgoraReferee) -> None:
    """Scenario, not a live rule: swap each fleet's FRAG for its home export
    at equal reference value, through SYSTEM with balanced ledger entries.
    The live game has no such genesis; there is no API for it."""
    with ref.lock, ref.conn:
        for agent in FLEETS:
            home = ref.get_vessel_location(agent)["station_id"]
            export = PLANET_EXPORT.get(home)
            frag = ref.get_balance(agent, "FRAG")
            value = frag * REF_PRICE["FRAG"]
            moves = [("FRAG", -frag)]
            if export:
                moves.append((export, int(value // REF_PRICE[export])))
            else:
                moves.append(("CR", int(value)))
            for inst, delta in moves:
                if delta == 0:
                    continue
                for acct, d in ((agent, delta), ("SYSTEM", -delta)):
                    ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
                    ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
                    ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, 0, ?, ?, ?)",
                                     (f"sim-genesis-{agent}", acct, inst, d))


# ------------------------------------------------------------ metrics

def classify_fills(ref: AgoraReferee) -> Dict[str, int]:
    rows = ref.conn.execute(
        "SELECT txn_id, GROUP_CONCAT(DISTINCT agent_id) AS parties FROM ledger_entries "
        "WHERE txn_id LIKE 'trade-%' OR txn_id LIKE 'auction-%' GROUP BY txn_id").fetchall()
    out = {"fleet_vs_depot": 0, "fleet_vs_fleet": 0, "other": 0}
    for r in rows:
        parties = set(r["parties"].split(","))
        fleets = parties & set(FLEETS)
        depots = {p for p in parties if p.startswith("depot_")}
        if len(fleets) >= 2:
            out["fleet_vs_fleet"] += 1
        elif fleets and depots:
            out["fleet_vs_depot"] += 1
        else:
            out["other"] += 1
    return out


def score(ref: AgoraReferee, agent: str) -> float:
    inv = inventory(ref, agent)
    escrow = sum(r["cargo_qty"] * REF_PRICE.get(r["commodity"], 0) for r in ref.conn.execute(
        "SELECT commodity, cargo_qty FROM transits WHERE agent_id = ? AND status = 'in_transit'", (agent,)))
    held = ref.peer.holdings_adjustment().get(agent, {})
    escrow += held.get("CR", 0) + sum(held.get(c, 0) * REF_PRICE[c] for c in TRADED)
    escrow += ref.contract_desk.holdings_adjustment().get(agent, 0)
    if ref.upgrades_enabled and not ref.fleet_out(agent):
        escrow += ref.upgrades.book_value(agent)
    return inv["CR"] + sum(inv[c] * REF_PRICE[c] for c in TRADED) + escrow


def depot_floor(ref: AgoraReferee) -> int:
    return min(r[0] for r in ref.conn.execute(
        "SELECT balance FROM accounts WHERE agent_id LIKE 'depot_%'"))


def _q(ref: AgoraReferee, sql: str, args=()) -> Any:
    v = ref.conn.execute(sql, args).fetchone()[0]
    return v or 0


def _txns(ref: AgoraReferee, prefix: str) -> int:
    return _q(ref, "SELECT COUNT(DISTINCT txn_id) FROM ledger_entries WHERE txn_id LIKE ?", (prefix + "%",))


def features(ref: AgoraReferee) -> Dict[str, Any]:
    """The live feature set this referee runs, for the report."""
    return {"depots": ref.depots_enabled, "depot_model": ref.depot_model, "band_pct": ref.circuit_breaker.band_pct,
            "peer_trades": ref.peer_trades, "fog": [ref.fog.lag, ref.fog.noise] if ref.fog else None,
            "idle_fee": ref.idle_fee, "rival_shares": ref.rival_shares, "exchange_shares": ref.exchange_shares,
            "exchange_vol": ref.exchange.vol, "contracts": ref.contracts_enabled, "corporate": ref.corporate_enabled,
            "upgrades": ref.upgrades_enabled, "hazards": list(ref.hazards.odds) if ref.hazards.odds else None,
            "piracy": list(ref.piracy.odds) if ref.piracy.odds else None,
            "events": getattr(ref, "events_enabled", None)}


def contract_report(ref: AgoraReferee) -> Optional[dict]:
    if not ref.contracts_enabled:
        return None
    rows = [dict(r) for r in ref.conn.execute("SELECT * FROM station_contracts")]
    return {"posted": len(rows),
            "claims": _txns(ref, "contract-claim-"),
            "units_delivered": sum(r["qty_total"] - r["qty_remaining"] for r in rows),
            "paid": sum((r["qty_total"] - r["qty_remaining"]) * r["price"] for r in rows),
            "fulfilled": sum(r["status"] == "fulfilled" for r in rows),
            "expired": sum(r["status"] == "lapsed" and r["qty_remaining"] > 0 for r in rows),
            "penalties": sum(r["penalty"] > 0 for r in rows), "penalty_cr": sum(r["penalty"] for r in rows),
            "shortfall_cr": sum(r["shortfall"] for r in rows),
            "bonds_cr": _q(ref, "SELECT SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'contract-claim-%' AND agent_id = 'SYSTEM'"),
            "bonds_forfeited_cr": _q(ref, "SELECT SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'contract-lapse-%' "
                                          "AND agent_id LIKE 'depot_%'"),
            "transfers": _txns(ref, "contract-buy-"),
            # price plus the deposit the buyer takes over
            "transfer_cr": _q(ref, "SELECT SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'contract-buy-%' AND delta > 0")}


def corporate_report(ref: AgoraReferee, track: dict) -> Optional[dict]:
    if not ref.corporate_enabled:
        return None
    ev = [dict(e) for e in ref.conn.execute("SELECT round, kind, agent_id, detail FROM corp_events ORDER BY id")]
    status = {r["agent_id"]: dict(r) for r in ref.conn.execute("SELECT * FROM corp_status")}
    return {"claims": _txns(ref, "contract-claim-"),
            "penalties": _q(ref, "SELECT COUNT(*) FROM station_contracts WHERE penalty > 0"),
            "penalty_cr": _q(ref, "SELECT SUM(penalty) FROM station_contracts"),
            "goods_sold_cr": _q(ref, "SELECT SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'distress-goods-%' "
                                     "AND instrument = 'CR' AND delta > 0 AND agent_id NOT LIKE 'depot_%'"),
            "share_auctions": sum(e["kind"] == "share_auction" for e in ev),
            "shares_auctioned": _q(ref, "SELECT SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'distress-auction-%' "
                                        "AND instrument LIKE 'EQ_%' AND delta > 0"),
            "takeovers": [{"round": e["round"], "raider": e["agent_id"], "detail": e["detail"]} for e in ev if e["kind"] == "takeover"],
            "bankruptcies": [{"round": e["round"], "fleet": e["agent_id"]} for e in ev if e["kind"] == "bankrupt"],
            "winner": next(({"fleet": e["agent_id"], "round": e["round"]} for e in ev if e["kind"] == "winner"), None),
            "max_debt": track["max_debt"], "rounds_in_debt": track["rounds_in_debt"],
            "bonds_cr": _q(ref, "SELECT SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'contract-claim-%' AND agent_id = 'SYSTEM'"),
            "debt_end": {a: s["debt"] for a, s in status.items() if s["debt"]},
            "absorbed": {a: (s["absorbed_by"] or s["status"]) for a, s in status.items() if s["status"] != "active"}}


def events_report(ref: AgoraReferee) -> Optional[dict]:
    """Corp events (agora/events.py) and the stock shocks they caused."""
    if not getattr(ref, "events_enabled", False):
        return None
    kinds = {r[0]: r[1] for r in ref.conn.execute("SELECT kind, COUNT(*) FROM corp_events GROUP BY kind")}
    vis = {r[0]: r[1] for r in ref.conn.execute("SELECT visibility, COUNT(*) FROM corp_events GROUP BY visibility")}
    return {"by_kind": kinds, "by_visibility": vis,
            "exposed": _q(ref, "SELECT COUNT(*) FROM corp_events WHERE exposed_round IS NOT NULL"),
            "stock_shocks": len(ref.exchange.shocks)}


def hazard_report(ref: AgoraReferee) -> Optional[dict]:
    if not ref.hazards.odds:
        return None
    return {"trips": _q(ref, "SELECT COUNT(*) FROM transits"),
            "delays": _q(ref, "SELECT COUNT(*) FROM transit_hazards WHERE delay > 0"),
            "delay_rounds": _q(ref, "SELECT SUM(delay) FROM transit_hazards"),
            "losses": _q(ref, "SELECT COUNT(*) FROM transit_hazards WHERE lost_qty > 0"),
            "units_lost": _q(ref, "SELECT SUM(lost_qty) FROM transit_hazards")}


def piracy_report(ref: AgoraReferee) -> Optional[dict]:
    if not ref.piracy.enabled:
        return None
    by = {r[0]: r[1] for r in ref.conn.execute("SELECT status, COUNT(*) FROM piracy_raids GROUP BY status")}
    return {"trips": _q(ref, "SELECT COUNT(*) FROM transits WHERE cargo_qty > 0"),
            "raids": sum(by.values()), "outcomes": by,
            "timed_out": _q(ref, "SELECT COUNT(*) FROM piracy_raids WHERE timed_out = 1"),
            "units_stolen": _q(ref, "SELECT SUM(qty_taken) FROM piracy_raids"),
            "cr_stolen": int(sum((r[1] or 0) * REF_PRICE.get(r[0], 0) for r in ref.conn.execute(
                "SELECT commodity, qty_taken FROM piracy_raids"))),
            "ransom_cr": _q(ref, "SELECT SUM(cr_taken) FROM piracy_raids"),
            "escorts": _txns(ref, "piracy-escort-"),
            "escort_cr": _q(ref, "SELECT SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'piracy-escort-%' AND agent_id = 'SYSTEM'"),
            "privateer_contracts": _q(ref, "SELECT COUNT(*) FROM piracy_privateers"),
            "privateer_raids": _q(ref, "SELECT SUM(raids) FROM piracy_privateers"),
            "privateers_traced": _q(ref, "SELECT COUNT(*) FROM piracy_raids WHERE traced = 1"),
            "fines_cr": _q(ref, "SELECT SUM(fines) FROM piracy_privateers")}


def peer_report(ref: AgoraReferee, pm: PeerMarket) -> Optional[dict]:
    if not ref.peer_trades:
        return None
    rows = [dict(r) for r in ref.conn.execute("SELECT * FROM station_escrow WHERE accepted_round IS NOT NULL")]
    exp = sum(r["qty"] for r in rows if r["status"] == "expired")
    pending = sum(r["qty"] for r in rows if r["status"] == "accepted")
    return {"trades": len(rows), "units": sum(r["qty"] for r in rows), "cr": sum(r["qty"] * r["price"] for r in rows),
            "remote": pm.remote, "collected": sum(r["qty"] for r in rows if r["status"] == "collected"),
            "expired_units": exp, "pending_units": pending,
            # never picked up (refunded at the deadline) plus still waiting at the end (#126)
            "uncollected": exp + pending}


def covert_report(ref: AgoraReferee) -> Optional[dict]:
    """Covert ops (agora/covert.py): wiretaps planted, sabotages executed, traces."""
    if not getattr(ref, "covert", None) or not ref.covert.enabled:
        return None
    return {
        "wiretaps": _q(ref, "SELECT COUNT(*) FROM covert_wiretaps"),
        "wiretap_cr": _q(ref, "SELECT SUM(cost) FROM covert_wiretaps"),
        "sabotages": _q(ref, "SELECT COUNT(*) FROM corp_events WHERE kind = 'sabotage'"),
        "sabotages_traced": _q(ref, "SELECT COUNT(*) FROM corp_events WHERE kind = 'sabotage' AND exposed_round IS NOT NULL"),
    }


# ------------------------------------------------------------ run

def _set_constants(constants: Optional[Dict[str, Any]]) -> Dict[tuple, Any]:
    """Override live module constants, e.g. {"contracts.PENALTY": 0.6}.
    Returns what to restore."""
    saved = {}
    for key, value in (constants or {}).items():
        mod_name, _, name = key.rpartition(".")
        mod = importlib.import_module(mod_name if mod_name.startswith("agora.") else f"agora.{mod_name}")
        if not hasattr(mod, name):
            raise ValueError(f"no live constant {key}")
        saved[(mod, name)] = getattr(mod, name)
        setattr(mod, name, value)
    return saved


def run(scenario: str, genesis: str = "flat", seed: int = 1, rounds: int = 300, mode: str = "strict",
        check_every: int = 25, overrides: Optional[Dict[str, Any]] = None,
        constants: Optional[Dict[str, Any]] = None, equity_mm: Optional[tuple] = None,
        vol: Optional[float] = None, theta: Optional[float] = None,
        spread_scale: Optional[float] = None, fleet_overrides: Optional[Dict[str, Any]] = None,
        on_round: Optional[Any] = None) -> dict:
    """One seeded game of the live referee.

    overrides: build_referee_from_env keyword overrides (e.g. {"piracy": "0.3,0.1"},
    {"corporate": False}). constants: live module constants to change for the
    run (e.g. {"contracts.PENALTY": 0.6}). vol, theta: the price engine's
    per-round sigma and mean reversion. spread_scale compresses each good's
    base price toward its four-station mean (1.0 = live) by editing
    agora.spatial.BASE_PRICES in place for the run. All are restored after.
    fleet_overrides: {agent: factory(agent, kind, seed, mode)} replaces that
    fleet's bot (tools/dominance.py). on_round(ref) is called after every
    step_round. Both default to off and change nothing else."""
    import agora.spatial as spatial_mod
    saved = {st: dict(v) for st, v in spatial_mod.BASE_PRICES.items()}
    if spread_scale is not None:
        for c in TRADED:
            m = sum(saved[st][c] for st in STATIONS) / len(STATIONS)
            for st in STATIONS:
                spatial_mod.BASE_PRICES[st][c] = round(m + spread_scale * (saved[st][c] - m), 1)
    restore = {}
    try:
        restore = _set_constants(constants)
        return _run(scenario, genesis, seed, rounds, mode, check_every, overrides, equity_mm, vol, theta,
                    constants, spread_scale, fleet_overrides, on_round)
    finally:
        for (mod, name), v in restore.items():
            setattr(mod, name, v)
        for st, v in saved.items():
            spatial_mod.BASE_PRICES[st].update(v)


def _run(scenario, genesis, seed, rounds, mode, check_every, overrides, equity_mm, vol, theta,
         constants, spread_scale, fleet_overrides=None, on_round=None) -> dict:
    ref = start_game(seed, overrides)
    if genesis == "planet":
        apply_planet_genesis(ref)
    if vol is not None:
        ref.spatial.vol = vol
    if theta is not None:
        ref.spatial.theta = theta
    kinds = scenario_kinds(scenario, seed)
    fleets = {a: (fleet_overrides or {}).get(a, build_fleet)(a, kind, seed, mode) for a, kind in kinds.items()}
    start = {a: score(ref, a) for a in FLEETS}
    # Rival shares every fleet holds: part of what the leaderboard pays, and
    # all of a stock trader's book. Valued at NAV, not the board mark: the mark falls back
    # to the last trade whenever the exchange quotes one side only (it runs
    # out of shares once a trader buys its float), and then goes stale.
    m_start = stock_navs(ref)
    stocks_mark_start = {a: stock_value(ref, a, m_start, "nav") for a in FLEETS}
    genesis_shares = {a: {sym: ref.get_balance(a, sym) for i, sym in EQ_SYM.items() if i != a} for a in FLEETS}
    eq_liq = EquityLiquidity(*equity_mm) if equity_mm else None
    traders = [a for a, k in kinds.items() if k == "stock_trader"]
    track_stocks = bool(eq_liq or traders)
    stocks_start = {}
    if track_stocks:
        m0 = stock_navs(ref)
        stocks_start = {a: stock_value(ref, a, m0) for a in FLEETS}
    stats = {"transits": 0, "halts_caused": 0, "band_blocked": 0, "stranded_events": 0, "contract_deliveries": 0}
    cmarket = ContractMarket(ref, kinds, seed)
    pmarket = PeerMarket(ref, kinds)
    corp_track = {"max_debt": 0, "rounds_in_debt": 0}
    first_negative_depot = None
    first_invariant_failure = None
    spread = []  # Earth ORE bid - Ceres ORE ask over time

    t0 = time.time()
    for _ in range(rounds):
        quotes = ref.get_depot_summary()["stations"]
        spread.append((quotes["earth"]["ORE"]["best_bid"] or 0) - (quotes["ceres"]["ORE"]["best_ask"] or 0))
        views = fleet_views(ref)
        live = [a for a in fleets if not ref.fleet_out(a)]
        cmarket.step(views, live)
        pmarket.step(views, live)
        # Stock traders act last, after the stand-in players have quoted.
        for a in live:
            if a not in traders:
                fleets[a].act(ref, views[a], stats)
        if eq_liq is not None:
            eq_liq.quote(ref, [a for a in live if a not in traders])
        for a in live:
            if a in traders:
                fleets[a].act(ref, views[a], stats)
        ref.step_round()
        if on_round is not None:
            on_round(ref)
        if ref.corporate_enabled:
            debts = [r[0] for r in ref.conn.execute("SELECT debt FROM corp_status WHERE debt > 0")]
            if debts:
                corp_track["rounds_in_debt"] += 1
                corp_track["max_debt"] = max(corp_track["max_debt"], max(debts))
        if first_negative_depot is None and depot_floor(ref) < 0:
            first_negative_depot = ref.current_round
        if first_invariant_failure is None and ref.current_round % check_every == 0:
            ok, errs = ref.verify_ledger_invariants()
            if not ok:
                first_invariant_failure = (ref.current_round, errs[:2])
    elapsed = time.time() - t0
    if first_invariant_failure is None:
        ok, errs = ref.verify_ledger_invariants()
        if not ok:
            first_invariant_failure = (ref.current_round, errs[:2])

    board = {e["agent_id"]: e["net_worth"] for e in ref.get_leaderboard()}
    end = {a: score(ref, a) for a in FLEETS}
    m_end = stock_navs(ref)
    stocks_mark_end = {a: stock_value(ref, a, m_end, "nav") for a in FLEETS}
    # What the genesis rival shares alone gained, held untouched: the part of
    # pnl_total every fleet gets from its rivals' growth, whatever its style.
    passive = {a: sum(q * (m_end[sym]["nav"] - m_start[sym]["nav"]) for sym, q in genesis_shares[a].items())
               for a in FLEETS}
    halts = ref.conn.execute("SELECT COUNT(*) FROM circuit_breaker_halts").fetchone()[0]
    q = max(1, len(spread) // 4)
    idle = {r[0]: -r[1] for r in ref.conn.execute(
        "SELECT agent_id, SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'idle-fee-%' AND agent_id != 'SYSTEM' "
        "GROUP BY agent_id")}
    extra = {}
    if track_stocks:
        m1 = stock_navs(ref)
        extra["stocks"] = {
            "equity_mm": list(equity_mm) if equity_mm else None,
            "fleets": {a: {"value_start": round(stocks_start[a]), "value_end": round(stock_value(ref, a, m1)),
                           "stock_cash": fleets[a].stock_cash if a in traders else None,
                           "stock_pnl": (round(fleets[a].stock_cash + stock_value(ref, a, m1) - stocks_start[a])
                                         if a in traders else None),
                           # holdings at the board mark (what the leaderboard pays), not NAV
                           "stock_pnl_at_mark": (round(fleets[a].stock_cash + stock_value(ref, a, m1, "mark")
                                                       - stocks_start[a]) if a in traders else None)}
                       for a in FLEETS},
            "nav_end": {sym: m1[sym]["nav"] for sym in EQ_SYM.values()},
        }
    return {**extra,
        "scenario": scenario, "genesis": genesis, "mode": mode, "seed": seed, "rounds": rounds,
        "features": features(ref), "overrides": overrides or {}, "constants": constants or {},
        "spread_scale": spread_scale,
        "depot_model": ref.depot_model, "band_pct": ref.circuit_breaker.band_pct,
        "reactive_bands": ref.reactive_bands,
        "idle_fees": idle, "idle_fees_collected": sum(idle.values()),
        "exchange": dict(ref.exchange.summary(), shares=ref.exchange_shares) if ref.exchange_shares else None,
        "contracts": contract_report(ref),
        "corporate": corporate_report(ref, corp_track),
        "hazards": hazard_report(ref),
        "events": events_report(ref),
        "piracy": piracy_report(ref),
        "upgrades": {a: ref.upgrades.holdings(a) for a in FLEETS} if ref.upgrades_enabled else None,
        "upgrade_cr": _q(ref, "SELECT SUM(delta) FROM ledger_entries WHERE txn_id LIKE 'upgrade-%' AND agent_id = 'SYSTEM'"),
        "fog": [ref.fog.lag, ref.fog.noise] if ref.fog else None,
        "peer": peer_report(ref, pmarket),
        "covert": covert_report(ref),
        "depot_cr_end": {st: ref.get_balance(f"depot_{st}", "CR") for st in STATIONS},
        "seconds": round(elapsed, 2),
        "fleets": {a: {"strategy": kinds[a], "start": round(start[a]), "end": round(end[a]),
                       "pnl": round(end[a] - start[a]),
                       # pnl plus the change in rival shares held, at NAV
                       "pnl_total": round(end[a] - start[a] + stocks_mark_end[a] - stocks_mark_start[a]),
                       "pnl_passive": round(passive[a]),
                       # the rest, so active + passive == total exactly
                       "pnl_active": (round(end[a] - start[a] + stocks_mark_end[a] - stocks_mark_start[a])
                                      - round(passive[a])),
                       "leaderboard_nw": board.get(a),
                       "out": ref.fleet_out(a) is not None} for a in FLEETS},
        "order_flow": ({"totals": dict(ref.order_flow.totals), "by_fleet": dict(ref.order_flow.by_fleet)}
                       if getattr(ref, "order_flow", None) is not None and ref.order_flow.enabled else None),
        "fills": classify_fills(ref),
        "transits": stats["transits"],
        "contract_deliveries": stats["contract_deliveries"],
        "pickup_trips": stats.get("pickup_trips", 0),
        "halts_total": halts,
        "band_blocked_checks": stats["band_blocked"],
        "stranded_events": stats["stranded_events"],
        "bad_orders": stats.get("bad_orders", 0),
        "day_trades": stats.get("day_trades", 0),
        "demands_ignored": stats.get("demands_ignored", 0),
        "vol": ref.spatial.vol, "theta": ref.spatial.theta,
        "rejected_moves": stats.get("rejected_moves", 0),
        "ore_spread_by_quarter": [round(statistics.mean(spread[i:i + q]), 1) for i in range(0, len(spread), q)][:4],
        "first_negative_depot_round": first_negative_depot,
        "first_invariant_failure": first_invariant_failure,
    }


# ------------------------------------------------------------ CLI

def _parse_value(s: str) -> Any:
    try:
        return json.loads(s)
    except ValueError:
        return s


def _referee_params() -> List[str]:
    return [p for p in inspect.signature(AgoraReferee.__init__).parameters if p not in ("self", "db_path")]


def table(results: List[dict]) -> str:
    """Markdown headline table: one row per scenario / genesis / mode, seeds averaged."""
    groups: Dict[tuple, List[dict]] = {}
    for r in results:
        groups.setdefault((r["scenario"], r["genesis"], r["mode"]), []).append(r)

    def mean(rs, f):
        return round(statistics.mean(f(r) for r in rs))

    header = (["scenario", "genesis", "mode", "seeds"] + [f"{a} P&L" for a in FLEETS]
              + ["transits", "halts", "contract units", "raids", "escorts", "upgrades",
                 "peer trades (uncollected)", "out", "invariants"])
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for (scen, gen, mode), rs in groups.items():
        pnl = []
        for a in FLEETS:
            pnl.append(f"{mean(rs, lambda r: r['fleets'][a]['pnl']):+} ({rs[0]['fleets'][a]['strategy']})")
        out = sum(sum(f["out"] for f in r["fleets"].values()) for r in rs)
        ups = sum(sum(sum(v.values()) for v in (r["upgrades"] or {}).values()) for r in rs)
        peer = (f"{mean(rs, lambda r: r['peer']['trades'])} ({mean(rs, lambda r: r['peer']['uncollected'])})"
                if rs[0]["peer"] else "off")
        inv = "ok" if all(r["first_invariant_failure"] is None for r in rs) else "FAIL"
        lines.append(f"| {scen} | {gen} | {mode} | {len(rs)} | " + " | ".join(pnl)
                     + f" | {mean(rs, lambda r: r['transits'])} | {mean(rs, lambda r: r['halts_total'])}"
                     + f" | {mean(rs, lambda r: (r['contracts'] or {}).get('units_delivered', 0))}"
                     + f" | {mean(rs, lambda r: (r['piracy'] or {}).get('raids', 0))}"
                     + f" | {mean(rs, lambda r: (r['piracy'] or {}).get('escorts', 0))}"
                     + f" | {ups / len(rs):.1f} | {peer} | {out} | {inv} |")
    return "\n".join(lines)


def _pct(xs: List[float], p: float) -> float:
    """Linear-interpolated percentile, p in [0, 100]."""
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def style_rows(results: List[dict], key: str = "pnl_total") -> Dict[str, dict]:
    """Per strategy, over every fleet that played it in these results:
    median, p10, p90 and mean of `key`, and how many ended out of the game."""
    by: Dict[str, List[dict]] = {}
    for r in results:
        for f in r["fleets"].values():
            by.setdefault(f["strategy"], []).append(f)
    return {k: {"n": len(fs), "median": _pct([f[key] for f in fs], 50), "p10": _pct([f[key] for f in fs], 10),
                "p90": _pct([f[key] for f in fs], 90), "mean": statistics.mean(f[key] for f in fs),
                "out": sum(f["out"] for f in fs)} for k, fs in by.items()}


def style_table(results: List[dict], key: str = "pnl_total") -> str:
    """Markdown per-style table (#162): median, p10, p90 final P&L."""
    rows = style_rows(results, key)
    lines = [f"| style | fleets | median | p10 | p90 | mean | out |", "|---|---|---|---|---|---|---|"]
    for k in sorted(rows, key=lambda k: -rows[k]["median"]):
        v = rows[k]
        lines.append(f"| {k} | {v['n']} | {v['median']:+,.0f} | {v['p10']:+,.0f} | {v['p90']:+,.0f} "
                     f"| {v['mean']:+,.0f} | {v['out']} |")
    return "\n".join(lines)


def _run_job(kw: dict) -> dict:
    faulthandler.dump_traceback_later(kw.pop("hang_timeout", 600), exit=True)
    return run(**kw)


def run_many(jobs: List[dict], workers: int = 1) -> List[dict]:
    """run(**job) for each job, in parallel processes when workers > 1."""
    if workers <= 1:
        return [_run_job(dict(j)) for j in jobs]
    import multiprocessing
    with multiprocessing.get_context("fork").Pool(workers) as pool:
        return pool.map(_run_job, [dict(j) for j in jobs], chunksize=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), action="append")
    ap.add_argument("--genesis", choices=["flat", "planet"], action="append")
    ap.add_argument("--mode", choices=["strict", "tolerant"], action="append",
                    help="hauler behaviour at the circuit-breaker band (default: both)")
    g = ap.add_argument_group("live game overrides (default: exactly what the live server runs)")
    g.add_argument("--depot-model", choices=["static", "reactive"], default=None, help="live: reactive")
    g.add_argument("--band-pct", type=float, default=None, help="circuit-breaker band (live 0.25)")
    g.add_argument("--fog", type=float, nargs=2, metavar=("LAG", "NOISE"), default=None,
                   help="fog of war (live 3 0.15); 0 0 turns it off")
    g.add_argument("--hazards", type=float, nargs=2, metavar=("P_DELAY", "P_LOSS"), default=None,
                   help="per-trip delay and cargo-loss odds (live 0.2 0.1); 0 0 turns them off")
    g.add_argument("--piracy", type=float, nargs=2, metavar=("P_BELT", "P_INNER"), default=None,
                   help="raid odds on belt and inner routes (live 0.15 0.04); 0 0 turns piracy off")
    g.add_argument("--exchange", type=float, nargs=2, metavar=("SHARES", "VOL"), default=None,
                   help="the exchange market maker's shares of each fleet and per-round vol (live 100 0.12)")
    g.add_argument("--idle-fee", type=int, default=None, help="CR per round for a docked fleet that did nothing (live 10)")
    g.add_argument("--free-quotes", action="store_true",
                   help="reactive depots: do not hold quotes inside the circuit-breaker band")
    g.add_argument("--off", nargs="+", default=[], metavar="FEATURE",
                   help=f"turn live features off, e.g. contracts corporate upgrades piracy; any of {_referee_params()}")
    g.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="any build_referee_from_env override, e.g. hazards=0.3,0.1 or rival_shares=0")
    g.add_argument("--const", action="append", default=[], metavar="MODULE.NAME=VALUE",
                   help="override a live module constant, e.g. contracts.PENALTY=0.6, piracy.RANSOM_PCT=0.2")
    g.add_argument("--drip", type=int, nargs=2, metavar=("MAIN", "SIDE"), default=None,
                   help="reactive depots' per-round restock/consumption (live 100 20)")
    g.add_argument("--penalty", type=float, default=None, help="contract lapse penalty (live contracts.PENALTY 0.5)")
    g.add_argument("--bond", type=float, default=None, help="contract claim deposit (live contracts.BOND_PCT 0.25)")
    g.add_argument("--vol", type=float, default=None, help="price engine per-round sigma in CR (live default 0.8)")
    g.add_argument("--theta", type=float, default=None, help="price engine mean reversion (live default 0.15)")
    g.add_argument("--spread-scale", type=float, default=None,
                   help="compress station price gaps toward each good's mean (1.0 = live)")
    ap.add_argument("--equity-mm", type=float, nargs=2, metavar=("SPREAD", "DEPTH"), default=None,
                    help="bot behaviour: non-trader fleets also quote rival shares at NAV +/- SPREAD, "
                         "DEPTH shares a side a round (e.g. 0.05 20)")
    ap.add_argument("--json", action="store_true", help="print raw results as JSON")
    ap.add_argument("--table", action="store_true", help="print only the markdown headline table")
    ap.add_argument("--styles", action="store_true",
                    help="print only the per-style table: median, p10, p90 of P&L incl. rival shares at NAV (#162)")
    ap.add_argument("--jobs", type=int, default=1, help="run games in this many parallel processes")
    ap.add_argument("--hang-timeout", type=int, default=600, help="dump stacks and exit if a run hangs")
    args = ap.parse_args()

    params = _referee_params()
    overrides: Dict[str, Any] = {}
    for name in args.off:
        if name not in params:
            ap.error(f"--off {name}: not a referee feature; one of {params}")
        overrides[name] = False
    for kv in args.set:
        k, _, v = kv.partition("=")
        if k not in params:
            ap.error(f"--set {k}: not a referee parameter; one of {params}")
        overrides[k] = _parse_value(v)
    if args.depot_model:
        overrides["depot_model"] = args.depot_model
    if args.band_pct is not None:
        overrides["band_pct"] = args.band_pct
    if args.fog:
        overrides["fog"] = {"lag": int(args.fog[0]), "noise": args.fog[1]} if args.fog[0] else False
    if args.hazards:
        overrides["hazards"] = list(args.hazards) if any(args.hazards) else False
    if args.piracy:
        overrides["piracy"] = list(args.piracy) if any(args.piracy) else False
    if args.exchange:
        overrides["exchange_shares"], overrides["exchange_vol"] = int(args.exchange[0]), args.exchange[1]
    if args.idle_fee is not None:
        overrides["idle_fee"] = args.idle_fee
    if args.free_quotes:
        overrides["reactive_bands"] = False
    constants: Dict[str, Any] = {}
    for kv in args.const:
        k, _, v = kv.partition("=")
        constants[k] = _parse_value(v)
    if args.drip:
        constants["referee.REACTIVE_MAIN_DRIP"], constants["referee.REACTIVE_SIDE_DRIP"] = args.drip
    if args.penalty is not None:
        constants["contracts.PENALTY"] = args.penalty
    if args.bond is not None:
        constants["contracts.BOND_PCT"] = args.bond

    jobs = []
    for scen in args.scenario or ["mixed", "haulers4", "idle4"]:
        for gen in args.genesis or ["flat", "planet"]:
            for mode in (args.mode or ["strict", "tolerant"]) if scen != "idle4" else ["strict"]:
                for seed in range(1, args.seeds + 1):
                    jobs.append(dict(scenario=scen, genesis=gen, seed=seed, rounds=args.rounds, mode=mode,
                                     overrides=overrides, constants=constants,
                                     equity_mm=((args.equity_mm[0], int(args.equity_mm[1]))
                                                if args.equity_mm else None),
                                     vol=args.vol, theta=args.theta, spread_scale=args.spread_scale,
                                     hang_timeout=args.hang_timeout))
    results = run_many(jobs, args.jobs)
    faulthandler.cancel_dump_traceback_later()

    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    if args.table:
        print(table(results))
        return 0
    if args.styles:
        print(style_table(results))
        return 0
    for r in results:
        print(f"\n== {r['scenario']} / {r['genesis']} genesis / {r['mode']} / seed {r['seed']} / {r['rounds']} rounds "
              f"({r['seconds']}s)")
        if r["overrides"] or r["constants"]:
            print(f"  overrides {r['overrides']}  constants {r['constants']}")
        for a, f in r["fleets"].items():
            print(f"  {a:7} {f['strategy']:12} start {f['start']:>8} end {f['end']:>8} pnl {f['pnl']:>+8}  "
                  f"board {f['leaderboard_nw']}{'  OUT' if f['out'] else ''}")
        print(f"  fills {r['fills']}  transits {r['transits']}  halts {r['halts_total']}  "
              f"band-blocked checks {r['band_blocked_checks']}  stranded events {r['stranded_events']}")
        print(f"  Earth ORE bid - Ceres ORE ask, by quarter: {r['ore_spread_by_quarter']}")
        print(f"  idle fees {r['idle_fees']}  upgrades {r['upgrades']}")
        print(f"  contracts: {r['contracts']}")
        print(f"  corporate: {r['corporate']}")
        print(f"  hazards: {r['hazards']}")
        print(f"  piracy: {r['piracy']}")
        print(f"  peer: {r['peer']}")
        print(f"  events: {r['events']}")
        print(f"  order flow: {r['order_flow']}")
        print(f"  depot CR at end: {r['depot_cr_end']}")
        print(f"  first negative depot balance: round {r['first_negative_depot_round']}  "
              f"invariant failure: {r['first_invariant_failure']}")
        if r.get("stocks"):
            for a, f in r["stocks"]["fleets"].items():
                if f["stock_pnl"] is not None:
                    print(f"  {a} stocks: pnl {f['stock_pnl']:+} (cash {f['stock_cash']:+}, "
                          f"holdings {f['value_start']} -> {f['value_end']} at NAV)")
    print("\n" + table(results))
    print("\n" + style_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
