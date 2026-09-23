#!/usr/bin/env python3
"""
tools/economy_sim.py - headless economics simulator for Station Agora.

Runs scripted fleets against an in-process referee (no HTTP, no Discord,
no production server) for hundreds of rounds and reports whether the
design produces sustained trading.

Strategies:
  hauler   buys where a good is cheap, flies it, sells where it is dear
  maker    quotes both sides at its home station, joining the depot touch
  idler    does nothing
  novice   a zero-context LLM player reading /referee/briefing: sees the
           whole board, but picks routes loosely (softmax, not argmax),
           sends malformed or misplaced orders, often prices inside the
           spread instead of hitting the depot, forgets to cancel stale
           orders, and sometimes forgets to buy FUEL before a MOVE

Scoring uses cash plus inventory at a FIXED reference price (the mean
BASE_PRICES across stations), not the leaderboard: the leaderboard marks
FUEL at zero, all FRAG at the Ceres mark, and cargo in transit (escrowed
to SYSTEM) at zero. Leaderboard net worth is reported alongside.

Usage:
  python3 tools/economy_sim.py                      # default scenario set
  python3 tools/economy_sim.py --depot-model reactive --band-pct 0.25 --drip 25 5
  python3 tools/economy_sim.py --rounds 500 --seeds 3
  python3 tools/economy_sim.py --scenario haulers4 --genesis planet
"""

import argparse
import faulthandler
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agora.referee as referee_mod  # noqa: E402
from agora.referee import AgoraReferee  # noqa: E402
from agora.spatial import STATIONS, BASE_PRICES, get_route  # noqa: E402

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
}


# ------------------------------------------------------------------ helpers

def order(ref: AgoraReferee, agent: str, side: str, qty: int, price: int, comm: str, st: str, tag: str):
    return ref.submit_envelope({"v": 1, "kind": "order", "payload": {
        "order_id": f"sim-{agent}-{tag}-{ref.current_round}-{ref.current_seq}",
        "agent_id": agent, "side": side, "qty": int(qty), "limit_price": int(price),
        "instrument": comm, "station_id": st, "seq_seen": ref.current_seq}})


def cancel_all(ref: AgoraReferee, agent: str) -> None:
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


# ------------------------------------------------------------ fuel sources (prototype)

FUEL_RESERVE = 40
TANK_SURPLUS = 150  # extra FUEL a hauler loads at a source station to resell  # FUEL a fleet keeps aboard when selling FUEL cargo: enough to leave any station


def fuel_sources(ref: AgoraReferee):
    return getattr(ref, "_sim_fuel_sources", None)


def strip_depot_fuel_asks(ref: AgoraReferee) -> None:
    """Prototype: depots trade FUEL only at the source stations. Everywhere
    else the depot neither buys nor sells it, so the only FUEL market there
    is between fleets. (A first cut left the non-source depots BUYING FUEL:
    haulers then sold hauled FUEL to the Ceres depot at 23 rather than to a
    stranded fleet bidding 20, and fleets piled up stranded.) Runs after
    every depot refresh (step_round re-posts both sides)."""
    sources = fuel_sources(ref)
    if not sources:
        return
    with ref.lock, ref.conn:
        for st in STATIONS:
            if st in sources:
                continue
            depot = f"depot_{st}"
            book = ref.books.get(st, {}).get("FUEL")
            if book is not None:
                book.asks = [o for o in book.asks if o.agent_id != depot]
                book.bids = [o for o in book.bids if o.agent_id != depot]
            ref.conn.execute("DELETE FROM orders WHERE agent_id = ? AND station_id = ? AND instrument = 'FUEL' "
                             "AND status = 'open'", (depot, st))


def buy_fuel_or_bid(ref: AgoraReferee, agent: str, st: str, need: int, quotes, premium: int, stats) -> None:
    """Buy FUEL from the depot if it sells here; otherwise rest a bid above
    the depot's FUEL bid, so a fleet selling FUEL here meets this bid first."""
    fa = quotes[st]["FUEL"]["best_ask"]
    if fa:
        if ref.get_balance(agent, "CR") >= need * fa and band_ok(ref, st, "FUEL", fa):
            order(ref, agent, "bid", need, fa, "FUEL", st, "fuel")
        return
    # A stranded fleet raises its bid 2 CR for every round it has waited.
    waits = ref.__dict__.setdefault("_sim_fuel_wait", {})
    waits[agent] = waits.get(agent, 0) + 1
    floor = min(quotes[s]["FUEL"]["best_ask"] or 99 for s in fuel_sources(ref))
    px = floor + premium + 2 * waits[agent]
    while px > floor and not band_ok(ref, st, "FUEL", px):
        px -= 1
    if ref.get_balance(agent, "CR") >= need * px:
        order(ref, agent, "bid", need, px, "FUEL", st, "fuelbid")
        stats["fuel_bids"] = stats.get("fuel_bids", 0) + 1


# ------------------------------------------------------------ contracts (#74 prototype)

class ContractBoard:
    """Station procurement contracts, simulator-only (issue #74 prototype).

    Every EVERY rounds a random station posts a contract for a good it does
    not produce cheaply: QTY units by DEADLINE rounds from now, paid at
    PREMIUM x the station's base price. A fleet docked there holding the good
    delivers into it (partial delivery allowed, first come first served);
    goods go to SYSTEM and SYSTEM pays, through balanced ledger entries.
    """

    EVERY = 4
    QTY = (300, 800)
    DEADLINE = (6, 10)
    PREMIUM = (1.3, 1.6)

    def __init__(self, ref: AgoraReferee, seed: int):
        import random
        self.ref = ref
        self.rng = random.Random(seed * 7919)
        self.open: List[dict] = []
        self.delivered = 0
        self.paid = 0
        self.expired = 0
        self.posted = 0

    def step(self) -> None:
        r = self.ref.current_round
        for c in self.open:
            if c["deadline"] < r and c["remaining"] > 0:
                self.expired += 1
        self.open = [c for c in self.open if c["deadline"] >= r and c["remaining"] > 0]
        if r % self.EVERY == 0:
            comm = self.rng.choice(["FRAG", "FOOD", "ORE", "FUEL"])
            cheap = min(STATIONS, key=lambda st: BASE_PRICES[st][comm])
            st = self.rng.choice([x for x in STATIONS if x != cheap])
            self.open.append({"id": f"c{r}-{st}-{comm}", "station": st, "comm": comm,
                              "remaining": self.rng.randint(*self.QTY),
                              "deadline": r + self.rng.randint(*self.DEADLINE),
                              "price": int(round(BASE_PRICES[st][comm] * self.rng.uniform(*self.PREMIUM)))})
            self.posted += 1

    def best_for(self, st: str, comm: str, arrival: int) -> Optional[dict]:
        live = [c for c in self.open if c["station"] == st and c["comm"] == comm
                and c["remaining"] > 0 and c["deadline"] >= arrival]
        return max(live, key=lambda c: c["price"]) if live else None

    def deliver(self, agent: str, st: str) -> int:
        ref, got = self.ref, 0
        for c in sorted(self.open, key=lambda c: -c["price"]):
            if c["station"] != st or c["remaining"] <= 0:
                continue
            qty = min(c["remaining"], ref.get_balance(agent, c["comm"]))
            if qty <= 0:
                continue
            with ref.lock, ref.conn:
                txn = f"contract-{c['id']}-{agent}-{ref.current_round}"
                for acct, inst, d in ((agent, c["comm"], -qty), ("SYSTEM", c["comm"], qty),
                                      (agent, "CR", qty * c["price"]), ("SYSTEM", "CR", -qty * c["price"])):
                    ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
                    ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
                    ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                                     (txn, ref.current_seq, acct, inst, d))
            c["remaining"] -= qty
            self.delivered += qty
            self.paid += qty * c["price"]
            got += qty
        return got


def charge_docking_fees(ref: AgoraReferee, fee: int) -> int:
    """Issue #73 prototype: every docked fleet pays `fee` CR per round to
    SYSTEM (balanced ledger entries). A fleet that cannot pay is charged what
    it has. Returns the total collected."""
    total = 0
    with ref.lock, ref.conn:
        for agent in FLEETS:
            if location(ref, agent) is None:
                continue
            due = min(fee, max(0, ref.get_balance(agent, "CR")))
            if due <= 0:
                continue
            txn = f"dockfee-{agent}-{ref.current_round}"
            for acct, d in ((agent, -due), ("SYSTEM", due)):
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (d, acct))
                ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                                 (txn, ref.current_seq, acct, d))
            total += due
    return total


# ------------------------------------------------------------ strategies

class Hauler:
    """Greedy one-hop arbitrage: sell cargo on arrival, then buy the single
    best (commodity, destination) margin net of fuel and toll, and fly."""

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

        board = getattr(ref, "_sim_contracts", None)
        if board is not None and board.deliver(self.agent, st):
            stats["contract_deliveries"] += 1
            inv = inventory(ref, self.agent)

        # 1. Sell cargo here only if this is the best market for it; otherwise
        #    it is cargo to haul (e.g. a per-planet genesis export).
        sell_goods = ("FRAG", "FOOD", "ORE") + (("FUEL",) if fuel_sources(ref) else ())
        for comm in sell_goods:
            qty = inv[comm] - (FUEL_RESERVE if comm == "FUEL" else 0)
            bid = quotes[st][comm]["best_bid"]
            if comm == "FUEL" and st in fuel_sources(ref):
                continue  # never dump FUEL back at a source
            if comm == "FUEL":
                # Tank surplus goes to whichever fleet is bidding here.
                if qty > 0 and bid and band_ok(ref, st, comm, bid):
                    order(ref, self.agent, "ask", min(qty, quotes[st][comm]["bid_depth"] or qty), bid, comm, st, "fuelsell")
                continue
            if qty > 0 and bid and bid >= self._hauling_value(quotes, st, comm) and band_ok(ref, st, comm, bid):
                order(ref, self.agent, "ask", min(qty, quotes[st][comm]["bid_depth"] or qty), bid, comm, st, "sell")
        inv = inventory(ref, self.agent)
        for comm in ("FRAG", "FOOD", "ORE"):
            if inv[comm] > 0:
                dest = max((d for d in STATIONS if d != st), key=lambda d: quotes[d][comm]["best_bid"] or 0)
                self._fly(ref, st, dest, comm, inv, stats)
                return

        # 2. Pick the best margin from here.
        best = None
        for dest in STATIONS:
            if dest == st:
                continue
            route = get_route(st, dest, ref.current_round)
            if not route:
                continue
            goods = ("FRAG", "FOOD", "ORE") + (("FUEL",) if fuel_sources(ref) and dest not in fuel_sources(ref) else ())
            for comm in goods:
                ask = quotes[st][comm]["best_ask"]
                bid = quotes[dest][comm]["best_bid"] or 0
                cap = 500
                if comm == "FUEL":
                    cap = min(cap, quotes[dest]["FUEL"]["bid_depth"] or 0)
                if board is not None:
                    c = board.best_for(dest, comm, ref.current_round + route["rounds"])
                    if c and c["price"] > bid:
                        bid, cap = c["price"], min(500, c["remaining"])
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
            return
        _, dest, comm, qty, ask, route = best

        # 3. Buy; fly now, or wait for the auction if the buy tripped a halt.
        res = order(ref, self.agent, "bid", qty, ask, comm, st, "buy")
        if res.get("status") == "circuit_breaker_halted":
            stats["halts_caused"] += 1
            self.plan = (dest, comm)
            return
        self._fly(ref, st, dest, comm, inventory(ref, self.agent), stats)

    def _fly(self, ref: AgoraReferee, st: str, dest: str, comm: str, inv, stats) -> None:
        route = get_route(st, dest, ref.current_round)
        need = route["fuel"] - inv["FUEL"]
        if fuel_sources(ref) and st in fuel_sources(ref):
            # FUEL is cheap here and scarce elsewhere: fill the tank past this
            # trip's burn, and sell the surplus to fleets stranded downstream.
            need = route["fuel"] + TANK_SURPLUS - inv["FUEL"]
        if need > 0 and fuel_sources(ref) and st not in fuel_sources(ref):
            buy_fuel_or_bid(ref, self.agent, st, need, ref.get_depot_summary()["stations"], 4, stats)
            if ref.get_balance(self.agent, "FUEL") < route["fuel"]:
                stats["fuel_waits"] = stats.get("fuel_waits", 0) + 1
                self.plan = None
                return
            need = 0
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
        held = ref.get_balance(self.agent, comm)
        if comm == "FUEL":
            held -= route["fuel"] + FUEL_RESERVE
        if held > 0:
            cancel_all(ref, self.agent)
            t = ref.initiate_transit(agent_id=self.agent, destination=dest, commodity=comm, cargo_qty=held)
            if t.get("status") == "in_transit":
                stats["transits"] += 1
                getattr(ref, "_sim_fuel_wait", {}).pop(self.agent, None)


class Maker:
    """Joins the depot's best bid and ask at its home station for every good,
    reposting each round. Resting first gives it time priority."""

    def __init__(self, agent: str, clip: int = 50, inside: bool = False):
        self.agent = agent
        self.clip = clip
        # inside: improve the depot's quotes by 1 CR where the spread allows,
        # so fleets trading here meet this fleet before the depot.
        self.inside = inside

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        st = location(ref, self.agent)
        if st is None:
            return
        cancel_all(ref, self.agent)
        inv = inventory(ref, self.agent)
        for comm in TRADED:
            q = quotes[st][comm]
            bid, ask = q["best_bid"], q["best_ask"]
            if self.inside and bid and ask and ask - bid >= 3:
                bid, ask = bid + 1, ask - 1
            if bid and inv["CR"] > bid * self.clip * 4 and band_ok(ref, st, comm, bid):
                order(ref, self.agent, "bid", self.clip, bid, comm, st, "mb")
            if ask and inv[comm] >= self.clip and band_ok(ref, st, comm, ask):
                order(ref, self.agent, "ask", self.clip, ask, comm, st, "ma")


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
        if self.rng.random() >= self.p_keep:
            cancel_all(ref, self.agent)
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
                if px and band_ok(ref, st, comm, px):
                    order(ref, self.agent, "ask", inv[comm], px, comm, st, "sell")
                self.dest = None
                return
            self._fly(ref, st, target, comm, inv, quotes, stats)
            return

        # Empty hold: choose a route by softmax over profit per round.
        options = []
        for dest in STATIONS:
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
            return
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

    def _fly(self, ref: AgoraReferee, st: str, dest: str, comm: str, inv, quotes, stats) -> None:
        route = get_route(st, dest, ref.current_round)
        need = route["fuel"] - inv["FUEL"]
        if need > 0 and self.rng.random() >= self.p_no_fuel:
            buy_fuel_or_bid(ref, self.agent, st, need, quotes, self.rng.randint(1, 6), stats)
            if fuel_sources(ref) and st not in fuel_sources(ref) and ref.get_balance(self.agent, "FUEL") < route["fuel"]:
                stats["fuel_waits"] = stats.get("fuel_waits", 0) + 1
                return  # leave the FUEL bid resting and wait for a seller
        held = ref.get_balance(self.agent, comm)
        cancel_all(ref, self.agent)
        t = ref.initiate_transit(agent_id=self.agent, destination=dest, commodity=comm, cargo_qty=held)
        if t.get("status") == "in_transit":
            stats["transits"] += 1
            getattr(ref, "_sim_fuel_wait", {}).pop(self.agent, None)
        else:
            stats["rejected_moves"] = stats.get("rejected_moves", 0) + 1


STRATEGY = {"hauler": Hauler, "maker": Maker, "idler": Idler,
            "inside_maker": lambda a: Maker(a, clip=100, inside=True)}


# ------------------------------------------------------------ genesis

def apply_planet_genesis(ref: AgoraReferee) -> None:
    """Swap each fleet's FRAG for its home export at equal reference value,
    through SYSTEM with balanced ledger entries."""
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
    return inv["CR"] + sum(inv[c] * REF_PRICE[c] for c in TRADED) + escrow


def depot_floor(ref: AgoraReferee) -> int:
    return min(r[0] for r in ref.conn.execute(
        "SELECT balance FROM accounts WHERE agent_id LIKE 'depot_%'"))


# ------------------------------------------------------------ run

def run(scenario: str, genesis: str, seed: int, rounds: int, mode: str = "strict", check_every: int = 25,
        depot_model: str = "static", band_pct: Optional[float] = None, reactive_bands: bool = True,
        contracts: bool = False, dock_fee: int = 0, fuel_src: Optional[List[str]] = None) -> dict:
    # Reactive depots are the referee's own implementation (agora/referee.py,
    # AGORA_DEPOT_MODEL), so these numbers describe what would ship.
    ref = AgoraReferee(depots=True, asymmetric=True, depot_model=depot_model,
                       band_pct=band_pct, reactive_bands=reactive_bands)
    ref.new_game(seed=seed, depots=True, asymmetric=True, depot_model=depot_model)
    if genesis == "planet":
        apply_planet_genesis(ref)
    def build(a: str, kind: str):
        if kind == "hauler":
            return Hauler(a, tolerate_halts=(mode == "tolerant"))
        if kind == "novice":
            return Novice(a, seed=seed)
        return STRATEGY[kind](a)
    fleets = {a: build(a, kind) for a, kind in SCENARIOS[scenario].items()}
    start = {a: score(ref, a) for a in FLEETS}
    stats = {"transits": 0, "halts_caused": 0, "band_blocked": 0, "stranded_events": 0, "contract_deliveries": 0}
    cboard = ContractBoard(ref, seed) if contracts else None
    ref._sim_contracts = cboard
    ref._sim_fuel_sources = set(fuel_src) if fuel_src else None
    strip_depot_fuel_asks(ref)
    first_negative_depot = None
    first_invariant_failure = None
    spread = []  # Earth ORE bid - Ceres ORE ask over time

    t0 = time.time()
    for _ in range(rounds):
        quotes = ref.get_depot_summary()["stations"]
        if ref._sim_fuel_sources:
            # Players see the whole book, not just the depot: a fleet's resting
            # FUEL bid is the price a FUEL hauler can actually sell at.
            for st in STATIONS:
                book = ref.books.get(st, {}).get("FUEL")
                if book is not None and book.best_bid() is not None:
                    q = quotes[st]["FUEL"]
                    q["best_bid"] = max(q["best_bid"] or 0, book.best_bid())
                    if st not in ref._sim_fuel_sources:
                        q["bid_depth"] = sum(o.remaining_qty for o in book.bids)
        spread.append((quotes["earth"]["ORE"]["best_bid"] or 0) - (quotes["ceres"]["ORE"]["best_ask"] or 0))
        if cboard is not None:
            cboard.step()
        for strat in fleets.values():
            strat.act(ref, quotes, stats)
        ref.step_round()
        strip_depot_fuel_asks(ref)
        if dock_fee:
            stats["dock_fees"] = stats.get("dock_fees", 0) + charge_docking_fees(ref, dock_fee)
        if first_negative_depot is None and depot_floor(ref) < 0:
            first_negative_depot = ref.current_round
        if first_invariant_failure is None and ref.current_round % check_every == 0:
            ok, errs = ref.verify_ledger_invariants()
            if not ok:
                first_invariant_failure = (ref.current_round, errs[:2])
    elapsed = time.time() - t0

    board = {e["agent_id"]: e["net_worth"] for e in ref.get_leaderboard()}
    end = {a: score(ref, a) for a in FLEETS}
    halts = ref.conn.execute("SELECT COUNT(*) FROM circuit_breaker_halts").fetchone()[0]
    q = max(1, len(spread) // 4)
    return {
        "scenario": scenario, "genesis": genesis, "mode": mode, "seed": seed, "rounds": rounds,
        "depot_model": depot_model, "band_pct": ref.circuit_breaker.band_pct,
        "reactive_bands": reactive_bands,
        "dock_fee": dock_fee, "dock_fees_collected": stats.get("dock_fees", 0),
        "contracts": None if cboard is None else {"posted": cboard.posted, "units_delivered": cboard.delivered,
                                                  "paid": cboard.paid, "expired": cboard.expired},
        "depot_cr_end": {st: ref.get_balance(f"depot_{st}", "CR") for st in STATIONS},
        "seconds": round(elapsed, 2),
        "fleets": {a: {"strategy": SCENARIOS[scenario][a], "start": round(start[a]), "end": round(end[a]),
                       "pnl": round(end[a] - start[a]), "leaderboard_nw": board.get(a)} for a in FLEETS},
        "fills": classify_fills(ref),
        "transits": stats["transits"],
        "halts_total": halts,
        "band_blocked_checks": stats["band_blocked"],
        "stranded_events": stats["stranded_events"],
        "bad_orders": stats.get("bad_orders", 0),
        "fuel_sources": sorted(fuel_src) if fuel_src else None,
        "fuel_bids": stats.get("fuel_bids", 0), "fuel_waits": stats.get("fuel_waits", 0),
        "rejected_moves": stats.get("rejected_moves", 0),
        "ore_spread_by_quarter": [round(statistics.mean(spread[i:i + q]), 1) for i in range(0, len(spread), q)][:4],
        "first_negative_depot_round": first_negative_depot,
        "first_invariant_failure": first_invariant_failure,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), action="append")
    ap.add_argument("--genesis", choices=["flat", "planet"], action="append")
    ap.add_argument("--mode", choices=["strict", "tolerant"], action="append",
                    help="hauler behaviour at the circuit-breaker band (default: both)")
    ap.add_argument("--depot-model", choices=["static", "reactive"], default="static",
                    help="static: today's refill-to-spot depots; reactive: finite shelves, drip restock, inventory-skewed prices")
    ap.add_argument("--band-pct", type=float, default=None, help="override the circuit-breaker band (default 0.10)")
    ap.add_argument("--drip", type=int, nargs=2, metavar=("MAIN", "SIDE"), default=None,
                    help="reactive depots: per-round restock/consumption at the main and other stations (default 100 20)")
    ap.add_argument("--contracts", action="store_true", help="enable the #74 station contract prototype")
    ap.add_argument("--fuel-sources", default=None,
                    help="prototype: comma list of stations whose depots SELL FUEL (e.g. earth,mars); others only buy it")
    ap.add_argument("--dock-fee", type=int, default=0, help="#73 prototype: CR charged per round to each docked fleet")
    ap.add_argument("--free-quotes", action="store_true",
                    help="reactive depots: do not hold quotes inside the circuit-breaker band")
    ap.add_argument("--json", action="store_true", help="print raw results as JSON")
    ap.add_argument("--hang-timeout", type=int, default=300, help="dump stacks and exit if a run hangs")
    args = ap.parse_args()

    if args.drip:
        referee_mod.REACTIVE_MAIN_DRIP, referee_mod.REACTIVE_SIDE_DRIP = args.drip
    faulthandler.dump_traceback_later(args.hang_timeout, exit=True)
    results = []
    for scen in args.scenario or ["mixed", "haulers4", "idle4"]:
        for gen in args.genesis or ["flat", "planet"]:
            for mode in (args.mode or ["strict", "tolerant"]) if scen != "idle4" else ["strict"]:
                for seed in range(1, args.seeds + 1):
                    results.append(run(scen, gen, seed, args.rounds, mode,
                                       depot_model=args.depot_model, band_pct=args.band_pct,
                                       reactive_bands=not args.free_quotes, contracts=args.contracts,
                                       dock_fee=args.dock_fee,
                                       fuel_src=args.fuel_sources.split(",") if args.fuel_sources else None))
    faulthandler.cancel_dump_traceback_later()

    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    for r in results:
        print(f"\n== {r['scenario']} / {r['genesis']} genesis / {r['mode']} / {r['depot_model']} depots, band {r['band_pct']:.0%} "
              f"/ seed {r['seed']} / {r['rounds']} rounds ({r['seconds']}s)")
        for a, f in r["fleets"].items():
            print(f"  {a:7} {f['strategy']:7} start {f['start']:>8} end {f['end']:>8} pnl {f['pnl']:>+8}  board {f['leaderboard_nw']}")
        print(f"  fills {r['fills']}  transits {r['transits']}  halts {r['halts_total']}  "
              f"band-blocked checks {r['band_blocked_checks']}  stranded events {r['stranded_events']}")
        print(f"  Earth ORE bid - Ceres ORE ask, by quarter: {r['ore_spread_by_quarter']}")
        print(f"  depot CR at end: {r['depot_cr_end']}  contracts: {r['contracts']}")
        print(f"  first negative depot balance: round {r['first_negative_depot_round']}  "
              f"invariant failure: {r['first_invariant_failure']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
