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
  (--fog LAG NOISE: stale, jittered remote quotes; --owned-contracts: tradable contracts)
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
import copy
import faulthandler
import json
import random
import math
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
    # #119: one fleet trades rival stocks only, against growing haulers or
    # erratic novices. Needs --equity-mm for anyone to trade with.
    "stocks": {"zero": "hauler", "amos": "hauler", "marvin": "stock_trader", "aerial": "hauler"},
    "stocks_vs_novices": {"zero": "novice", "amos": "hauler", "marvin": "stock_trader", "aerial": "novice"},
}


# Strategies that fly goods into contracts (Hauler and Novice call
# ContractBoard.deliver). Only these claim or buy contracts: an idler or a
# market maker never delivers, so a claim by one is a guaranteed penalty.
CONTRACTORS = {"hauler", "novice"}


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
    HOLD = 3  # rounds a buyer keeps a contract before it will resell

    def __init__(self, ref: AgoraReferee, seed: int, owned: bool = False, claim: bool = False):
        self.ref = ref
        # claim: contracts start unowned and undeliverable; a corp must claim
        # one (Corporate.claim) before it can deliver. on_expire is called
        # with each contract that lapses undelivered.
        self.claim_mode = claim
        self.on_expire = None
        self.rng = random.Random(seed * 7919)
        # owned: each contract is awarded to one corp, only the owner can
        # deliver into it, and owners can sell contracts to other corps.
        self.owned = owned
        # fleets allowed to buy contracts in trade(); None means every fleet
        self.contractors: Optional[set] = None
        self.transfers = 0
        self.transfer_cr = 0
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
                if self.on_expire:
                    self.on_expire(c)
        self.open = [c for c in self.open if c["deadline"] >= r and c["remaining"] > 0]
        if r % self.EVERY == 0:
            comm = self.rng.choice(["FRAG", "FOOD", "ORE", "FUEL"])
            cheap = min(STATIONS, key=lambda st: BASE_PRICES[st][comm])
            st = self.rng.choice([x for x in STATIONS if x != cheap])
            self.open.append({"id": f"c{r}-{st}-{comm}", "station": st, "comm": comm,
                              "remaining": self.rng.randint(*self.QTY),
                              "deadline": r + self.rng.randint(*self.DEADLINE),
                              "price": int(round(BASE_PRICES[st][comm] * self.rng.uniform(*self.PREMIUM))),
                              "owner": (self.rng.choice(FLEETS) if self.owned and not self.claim_mode else None)})
            self.posted += 1

    def _mine(self, c: dict, agent: Optional[str]) -> bool:
        if self.claim_mode:
            return c["owner"] is not None and c["owner"] == agent
        return c["owner"] is None or c["owner"] == agent

    def best_for(self, st: str, comm: str, arrival: int, agent: Optional[str] = None) -> Optional[dict]:
        live = [c for c in self.open if c["station"] == st and c["comm"] == comm
                and c["remaining"] > 0 and c["deadline"] >= arrival and self._mine(c, agent)]
        return max(live, key=lambda c: c["price"]) if live else None

    def deliver(self, agent: str, st: str) -> int:
        ref, got = self.ref, 0
        for c in sorted(self.open, key=lambda c: -c["price"]):
            if c["station"] != st or c["remaining"] <= 0 or not self._mine(c, agent):
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


    # -------------------------------------------------- contract trading

    def value_to(self, agent: str, c: dict, view) -> int:
        """What contract c is worth to `agent`, from where it is and what it can
        see: buy the good at the cheapest station it knows of, fly it in, get
        paid. Zero if it cannot make the deadline. Uses the agent's own
        (possibly fogged) quotes, so two corps can honestly disagree."""
        # A corp in transit plans from where and when it will arrive.
        loc = self.ref.get_vessel_location(agent)
        r = self.ref.current_round
        if loc.get("status") == "in_transit" and loc.get("transit"):
            st, r = loc["transit"]["destination"], max(r, loc["transit"]["arrival_round"])
        else:
            st = loc["station_id"]
        best = 0
        qty = min(c["remaining"], 500)
        held = self.ref.get_balance(agent, c["comm"])
        for src in STATIONS:
            if src == c["station"]:
                continue
            ask = view[src][c["comm"]]["best_ask"]
            if not ask:
                continue
            leg1 = get_route(st, src, r) if st != src else {"rounds": 0, "fuel": 0, "toll": 0}
            leg2 = get_route(src, c["station"], r)
            if not leg1 or not leg2 or r + leg1["rounds"] + leg2["rounds"] > c["deadline"]:
                continue
            fuel_px = view[st]["FUEL"]["best_ask"] or 20
            cost = (leg1["fuel"] + leg2["fuel"]) * fuel_px + leg1.get("toll", 0) + leg2.get("toll", 0)
            v = (c["price"] - ask) * qty - cost
            best = max(best, v)
        if st == c["station"] and held > 0:
            best = max(best, (c["price"] - (view[st][c["comm"]]["best_bid"] or 0)) * min(qty, held))
        return int(best)

    def trade(self, views: Dict[str, dict], min_gain: int = 200) -> None:
        """Owners sell a contract to whichever corp values it most, at the
        midpoint of the two valuations, when the gap is worth the bother.
        CR moves buyer -> owner through balanced ledger entries."""
        ref = self.ref
        for c in self.open:
            if not c["owner"] or c["remaining"] <= 0:
                continue
            # Contracts trade at a distance: neither corp needs to be docked, or
            # at the contract's station (Ryan, #agent-chat 2026-09-22 22:2x).
            # A corp keeps a contract it just bought for HOLD rounds.
            if ref.current_round - c.get("bought", -99) < self.HOLD:
                continue
            own_v = self.value_to(c["owner"], c, views[c["owner"]])
            bids = [(self.value_to(a, c, views[a]), a) for a in FLEETS
                    if a != c["owner"] and (self.contractors is None or a in self.contractors)]
            if not bids:
                continue
            v, buyer = max(bids)
            if v - own_v < min_gain:
                continue
            price = (v + max(0, own_v)) // 2
            if ref.get_balance(buyer, "CR") < price:
                continue
            with ref.lock, ref.conn:
                txn = f"ctrade-{c['id']}-{ref.current_round}"
                for acct, d in ((buyer, -price), (c["owner"], price)):
                    ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (d, acct))
                    ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                                     (txn, ref.current_seq, acct, d))
            c["owner"] = buyer
            c["bought"] = ref.current_round
            self.transfers += 1
            self.transfer_cr += price


# ------------------------------------------------------------ peer trades at a distance

class PeerDesk:
    """Fleet-to-fleet goods trades agreed from anywhere (Ryan, #agent-chat
    2026-09-22 22:18). A fleet docked at station S offers goods it holds
    there; any fleet, wherever it is, may take the offer. The buyer pays
    now, the goods are held at S in the buyer's name (escrowed to SYSTEM),
    and the buyer collects them the next time it is docked at S.

    Seller's floor: what the goods are worth to it otherwise, i.e. the
    depot bid at S, or the best bid elsewhere less the per-unit cost of
    flying them there. Offer price: the midpoint of that floor and the
    depot ask at S, so a buyer saves against the depot.
    Buyer: any fleet, wherever it is, takes it if flying to S to collect
    and hauling the goods on to its best market still pays at the offer
    price, per its own (possibly fogged) view."""

    MIN_LOT = 20

    def __init__(self, ref: AgoraReferee):
        self.ref = ref
        self.pickups: Dict[tuple, int] = {}  # (station, buyer, comm) -> qty
        self.trades = 0
        self.units = 0
        self.cr = 0
        self.remote = 0  # buyer was not docked at S when it agreed

    def _move(self, txn: str, legs) -> None:
        ref = self.ref
        with ref.lock, ref.conn:
            for acct, inst, d in legs:
                ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
                ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                                 (txn, ref.current_seq, acct, inst, d))

    def _available(self, agent: str, inst: str) -> int:
        """Balance less what resting orders commit, as agora/peer.py checks
        it. Using the raw balance let the desk sell goods a resting ask (a
        distress sale, say) had already committed; the ask then filled and
        the seller went negative (seed 5, novice_vs_haulers, all features)."""
        committed = 0
        for books in self.ref.books.values():
            for comm, book in books.items():
                if inst == "CR":
                    committed += sum(o.remaining_qty * o.limit_price for o in book.bids if o.agent_id == agent)
                elif comm == inst:
                    committed += sum(o.remaining_qty for o in book.asks if o.agent_id == agent)
        return self.ref.get_balance(agent, inst) - committed

    def collect(self) -> None:
        for (st, buyer, comm), qty in list(self.pickups.items()):
            if location(self.ref, buyer) == st:
                self._move(f"pickup-{buyer}-{st}-{comm}-{self.ref.current_round}",
                           ((buyer, comm, qty), ("SYSTEM", comm, -qty)))
                del self.pickups[(st, buyer, comm)]

    @staticmethod
    def _unit_trip_cost(view, st: str, dest: str, qty: int, r: int) -> float:
        route = get_route(st, dest, r)
        if not route or qty <= 0:
            return float("inf")
        return (route["fuel"] * (view[st]["FUEL"]["best_ask"] or 20) + route.get("toll", 0)) / qty

    def _best_haul(self, view, st: str, comm: str, qty: int, r: int) -> float:
        """Best per-unit value of `comm` held at st: sell here, or fly it."""
        here = view[st][comm]["best_bid"] or 0
        away = max(((view[d][comm]["best_bid"] or 0) - self._unit_trip_cost(view, st, d, qty, r)
                    for d in STATIONS if d != st), default=0)
        return max(here, away)

    def match(self, views: Dict[str, dict]) -> None:
        ref, r = self.ref, self.ref.current_round
        for seller in FLEETS:
            st = location(ref, seller)
            if st is None:
                continue
            sv = views[seller]
            for comm in ("FRAG", "FOOD", "ORE", "FUEL"):
                have = self._available(seller, comm) - (100 if comm == "FUEL" else 0)
                ask = sv[st][comm]["best_ask"]
                if have < self.MIN_LOT or not ask:
                    continue
                floor = self._best_haul(sv, st, comm, have, r)
                if floor >= ask - 1:
                    continue
                price = int((floor + ask) // 2)
                if price <= floor:
                    price = int(floor) + 1
                if price >= ask:
                    continue
                for buyer in FLEETS:
                    if buyer == seller or have < self.MIN_LOT:
                        continue
                    # From anywhere: a buyer elsewhere prices in the trip to S
                    # to collect (Zero's review of #101: the first cut only let
                    # fleets already at or bound for S agree).
                    loc = ref.get_vessel_location(buyer)
                    at = loc["transit"]["destination"] if loc.get("status") == "in_transit" else loc["station_id"]
                    qty = min(have, 500, max(0, (self._available(buyer, "CR") - 300) // price))
                    if qty < self.MIN_LOT:
                        continue
                    bv = views[buyer]
                    reach = 0.0 if at == st else self._unit_trip_cost(bv, at, st, qty, r)
                    others = [d for d in STATIONS if d != st]
                    gain = max((bv[d][comm]["best_bid"] or 0) - self._unit_trip_cost(bv, st, d, qty, r) for d in others) - reach
                    if gain <= price:
                        continue
                    self._move(f"peer-{seller}-{buyer}-{st}-{comm}-{r}",
                               ((buyer, "CR", -qty * price), (seller, "CR", qty * price),
                                (seller, comm, -qty), ("SYSTEM", comm, qty)))
                    self.pickups[(st, buyer, comm)] = self.pickups.get((st, buyer, comm), 0) + qty
                    self.trades += 1
                    self.units += qty
                    self.cr += qty * price
                    self.remote += at != st or loc.get("status") != "docked"
                    have -= qty


# ------------------------------------------------------------ corporate risk (debt, distress, takeovers)

class Corporate:
    """Ryan, #agent-chat 2026-09-22 23:02: real risk for the corps.

    - Claims: contracts start unowned. Each round the corp that values an
      open contract most (value_to, from its own position and view) claims
      it, at most MAX_CLAIMS open per corp. Novices are overconfident:
      they value contracts at 1.0-2.5x their true worth.
    - Penalty: a contract that lapses undelivered costs its current owner
      PENALTY x the undelivered value.
    - Debt: what a corp owes beyond its CR. Distress settles it each round:
      sell goods to the local depot at its bid, then auction the corp's own
      treasury shares at DISCOUNT x NAV to the richest rival that can pay.
    - Takeover: a rival holding TAKEOVER_SHARES of a corp's stock absorbs it
      (all balances and contracts pass to the acquirer, which also takes on
      its debt). A corp that absorbs every rival wins outright.
    """

    MAX_CLAIMS = 2
    PENALTY = 0.5  # tuned 2026-09-22: at 0.5 the corps that go bust are the overconfident claimers
    DISCOUNT = 0.7
    TAKEOVER_SHARES = 510
    AUCTION_CAP = 100   # treasury shares a distressed corp sells per round
    PRICE_FLOOR = 10    # CR/share floor under the auction price, before DISCOUNT
    BANKRUPT_ROUNDS = 10  # rounds in debt with no treasury shares left before a corp is out

    def __init__(self, ref: AgoraReferee, board: "ContractBoard", kinds: Dict[str, str], seed: int):
        self.ref, self.board, self.kinds = ref, board, kinds
        self.rng = random.Random(seed * 31337)
        self.debt = {a: 0 for a in FLEETS}
        self.out: Dict[str, str] = {}  # absorbed corp -> acquirer, or "bankrupt"
        self._since_debt: Dict[str, int] = {}
        self.stats = {"claims": 0, "penalties": 0, "penalty_cr": 0, "goods_sold_cr": 0,
                      "share_auctions": 0, "shares_auctioned": 0, "takeovers": [], "bankruptcies": [], "winner": None,
                      "max_debt": 0, "rounds_in_debt": 0}
        board.on_expire = self._expired

    def active(self) -> List[str]:
        return [a for a in FLEETS if a not in self.out]

    def _move(self, txn: str, legs) -> None:
        ref = self.ref
        with ref.lock, ref.conn:
            for acct, inst, d in legs:
                if not d:
                    continue
                ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (d, acct, inst))
                ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                                 (txn, ref.current_seq, acct, inst, d))

    def _sym(self, a: str) -> str:
        return {"amos": "EQ_AMOS", "marvin": "EQ_MARV", "zero": "EQ_ZERO", "aerial": "EQ_AERL"}[a]

    # ------------------------------------------------------------ claims and penalties

    def claim(self, views: Dict[str, dict]) -> None:
        held = {a: sum(1 for c in self.board.open if c["owner"] == a) for a in FLEETS}
        for c in self.board.open:
            if c["owner"] is not None or c["remaining"] <= 0:
                continue
            bids = []
            for a in self.active():
                if held[a] >= self.MAX_CLAIMS or self.kinds.get(a) not in CONTRACTORS:
                    continue
                v = self.board.value_to(a, c, views[a])
                if self.kinds.get(a) == "novice":
                    v = int(max(v, c["price"] * min(c["remaining"], 500) * 0.1) * self.rng.uniform(1.0, 2.5))
                if v > 0:
                    bids.append((v, a))
            if bids:
                _, a = max(bids)
                c["owner"] = a
                held[a] += 1
                self.stats["claims"] += 1

    def _expired(self, c: dict) -> None:
        owner = c.get("owner")
        if not owner:
            return
        pen = int(c["price"] * c["remaining"] * self.PENALTY)
        self.stats["penalties"] += 1
        self.stats["penalty_cr"] += pen
        self.debt[owner] += pen

    # ------------------------------------------------------------ distress

    def _pay_down(self, a: str) -> None:
        pay = min(self.debt[a], self.ref.get_balance(a, "CR"))
        if pay > 0:
            self._move(f"debt-{a}-{self.ref.current_round}-{pay}", ((a, "CR", -pay), ("SYSTEM", "CR", pay)))
            self.debt[a] -= pay

    def settle(self) -> None:
        ref = self.ref
        any_debt = False
        for a in self.active():
            self._pay_down(a)
            if self.debt[a] <= 0:
                continue
            # 1. goods to the local depot at its bid
            st = location(ref, a)
            if st:
                q = ref.get_depot_summary()["stations"][st]
                for comm in ("FRAG", "FOOD", "ORE"):
                    have, bid = ref.get_balance(a, comm), q[comm]["best_bid"]
                    if have > 0 and bid and self.debt[a] > 0:
                        before = ref.get_balance(a, "CR")
                        order(ref, a, "ask", min(have, self.debt[a] // bid + 1), bid, comm, st, "distress")
                        self.stats["goods_sold_cr"] += ref.get_balance(a, "CR") - before
                self._pay_down(a)
            # 2. auction own treasury shares at a discount: at most
            #    AUCTION_CAP shares a round, split between rivals in
            #    proportion to their cash, priced off the better of NAV and
            #    the board mark (a bust corp's NAV alone collapses to 1).
            if self.debt[a] > 0:
                sym = self._sym(a)
                base = {e["agent_id"]: e["net_worth"] - e.get("stocks_value", 0) for e in ref.get_leaderboard()}
                m = ref.stock_marks(base)[sym]
                px = max(1, int(max(m["nav"], m["mark"], self.PRICE_FLOOR) * self.DISCOUNT))
                n = min(ref.get_balance(a, sym), -(-self.debt[a] // px), self.AUCTION_CAP)
                rivals = [(ref.get_balance(b, "CR"), b) for b in self.active() if b != a]
                total = sum(c for c, _ in rivals) or 1
                for cash, b in sorted(rivals, reverse=True):
                    if n <= 0:
                        break
                    k = min(n, cash // px, max(1, round(self.AUCTION_CAP * cash / total)))
                    if k <= 0:
                        continue
                    self._move(f"auction-{sym}-{b}-{ref.current_round}",
                               ((a, sym, -k), (b, sym, k), (b, "CR", -k * px), (a, "CR", k * px)))
                    self.stats["share_auctions"] += 1
                    self.stats["shares_auctioned"] += k
                    n -= k
                self._pay_down(a)
            if self.debt[a] > 0:
                any_debt = True
                self.stats["max_debt"] = max(self.stats["max_debt"], self.debt[a])
                self._since_debt[a] = self._since_debt.get(a, 0) + 1
                # Bankrupt: still in debt with no treasury shares left to sell
                # after BANKRUPT_ROUNDS rounds. The corp is out; its remaining
                # assets go to its creditors (SYSTEM).
                if ref.get_balance(a, self._sym(a)) <= 0 and self._since_debt[a] >= self.BANKRUPT_ROUNDS:
                    cancel_all(ref, a)
                    legs = []
                    for r in ref.conn.execute("SELECT instrument, balance FROM accounts WHERE agent_id = ? "
                                              "AND balance != 0", (a,)).fetchall():
                        legs += [(a, r["instrument"], -r["balance"]), ("SYSTEM", r["instrument"], r["balance"])]
                    self._move(f"bankrupt-{a}-{ref.current_round}", legs)
                    for c in self.board.open:
                        if c["owner"] == a:
                            c["owner"] = None
                    self.out[a] = "bankrupt"
                    self.stats["bankruptcies"].append({"round": ref.current_round, "fleet": a, "debt": self.debt[a]})
            else:
                self._since_debt[a] = 0
        if any_debt:
            self.stats["rounds_in_debt"] += 1

    # ------------------------------------------------------------ takeovers

    def takeovers(self) -> None:
        ref = self.ref
        for target in self.active():
            sym = self._sym(target)
            for raider in self.active():
                if raider == target or target in self.out:
                    continue
                if ref.get_balance(raider, sym) < self.TAKEOVER_SHARES:
                    continue
                cancel_all(ref, target)
                legs = []
                for r in ref.conn.execute("SELECT instrument, balance FROM accounts WHERE agent_id = ? AND balance != 0",
                                          (target,)).fetchall():
                    legs += [(target, r["instrument"], -r["balance"]), (raider, r["instrument"], r["balance"])]
                self._move(f"takeover-{raider}-{target}-{ref.current_round}", legs)
                for c in self.board.open:
                    if c["owner"] == target:
                        c["owner"] = raider
                self.debt[raider] += self.debt[target]
                self.debt[target] = 0
                self.out[target] = raider
                self.stats["takeovers"].append({"round": ref.current_round, "raider": raider, "target": target})
        alive = self.active()
        if len(alive) == 1 and self.stats["winner"] is None:
            self.stats["winner"] = {"fleet": alive[0], "round": ref.current_round}


# ------------------------------------------------------------ fog of war

class Fog:
    """Each fleet sees exact depot quotes only where it is docked. Every other
    station shows quotes LAG rounds old, each price jittered by up to NOISE,
    drawn per fleet, so two fleets misread the same station differently."""

    def __init__(self, lag: int, noise: float, seed: int):
        self.lag, self.noise = lag, noise
        self.history: List[dict] = []
        self.rng = random.Random(seed * 104729)

    def record(self, quotes) -> None:
        self.history.append(copy.deepcopy(quotes))
        self.history = self.history[-(self.lag + 1):]

    def view(self, ref: AgoraReferee, agent: str, quotes):
        here = location(ref, agent)
        old = self.history[0]
        out = {}
        for st in STATIONS:
            if st == here:
                out[st] = quotes[st]
                continue
            out[st] = {}
            for comm, q in old[st].items():
                j = dict(q)
                for side in ("best_bid", "best_ask"):
                    if j.get(side):
                        j[side] = max(1, int(round(j[side] * (1 + self.rng.uniform(-self.noise, self.noise)))))
                if j.get("best_bid") and j.get("best_ask") and j["best_bid"] >= j["best_ask"]:
                    j["best_ask"] = j["best_bid"] + 1
                out[st][comm] = j
        return out


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
        for comm in ("FRAG", "FOOD", "ORE"):
            qty = inv[comm]
            bid = quotes[st][comm]["best_bid"]
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
            for comm in ("FRAG", "FOOD", "ORE"):
                ask = quotes[st][comm]["best_ask"]
                bid = quotes[dest][comm]["best_bid"] or 0
                cap = 500
                if board is not None:
                    c = board.best_for(dest, comm, ref.current_round + route["rounds"], self.agent)
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
        if held > 0:
            cancel_all(ref, self.agent)
            t = ref.initiate_transit(agent_id=self.agent, destination=dest, commodity=comm, cargo_qty=held)
            if t.get("status") == "in_transit":
                stats["transits"] += 1


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
        board = getattr(ref, "_sim_contracts", None)
        if board is not None and board.deliver(self.agent, st):
            stats["contract_deliveries"] += 1
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
            fa = quotes[st]["FUEL"]["best_ask"]
            if fa and inv["CR"] >= need * fa and band_ok(ref, st, "FUEL", fa):
                order(ref, self.agent, "bid", need, fa, "FUEL", st, "fuel")
        held = ref.get_balance(self.agent, comm)
        cancel_all(ref, self.agent)
        t = ref.initiate_transit(agent_id=self.agent, destination=dest, commodity=comm, cargo_qty=held)
        if t.get("status") == "in_transit":
            stats["transits"] += 1
        else:
            stats["rejected_moves"] = stats.get("rejected_moves", 0) + 1


# ------------------------------------------------------------ fleet stocks (#119)

EQ_SYM = {"amos": "EQ_AMOS", "marvin": "EQ_MARV", "zero": "EQ_ZERO", "aerial": "EQ_AERL"}


def stock_navs(ref: AgoraReferee) -> Dict[str, dict]:
    base = {e["agent_id"]: e["net_worth"] - e.get("stocks_value", 0) for e in ref.get_leaderboard()}
    return ref.stock_marks(base)


def stock_value(ref: AgoraReferee, agent: str, marks: Dict[str, dict]) -> float:
    """Rival shares held, at NAV. Own shares count for nothing, as on the leaderboard."""
    return sum(ref.get_balance(agent, sym) * marks[sym]["nav"] for a, sym in EQ_SYM.items() if a != agent)


def cancel_stock_orders(ref: AgoraReferee, agent: str) -> None:
    for sym in EQ_SYM.values():
        b = ref.books["ceres"][sym]
        for o in list(b.bids) + list(b.asks):
            if o.agent_id == agent:
                ref.cancel_order(agent, o.order_id)


class EquityLiquidity:
    """STAND-IN for other players on the stock exchange. The live referee
    has no depot on equity books: a stock order fills only against another
    fleet's resting order. Here every fleet that is not a stock trader
    quotes the rivals' shares it holds at NAV x (1 +/- SPREAD), DEPTH shares
    a side a round, from its own CR and shares, so nothing is minted. This
    is an assumption about how LLM players will behave, not something the
    live game provides; stock-trader results scale with it."""

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
                held = ref.get_balance(a, sym)
                if held > 0:
                    order(ref, a, "ask", min(self.depth, held), ask, sym, "ceres", "eqmm")
                if ref.get_balance(a, "CR") > 2000 + bid * self.depth:
                    order(ref, a, "bid", self.depth, bid, sym, "ceres", "eqmm")


class StockTrader:
    """Trades rival stocks only, never goods or contracts (#119).

    NAV is noisy: a hauler's cargo in flight is escrowed and drops out of
    its net worth until it docks, so NAV dips and recovers with every trip.
    Fair value is therefore NAV's LONG-round average carried forward along
    its LONG-round growth, not the latest NAV. Buys when the best ask is
    EDGE below fair, sells when the best bid is EDGE above it, spending at
    most STAKE of its cash on one order. Only takes liquidity: any rest is
    cancelled at once, so every stock fill happens inside act() and its
    cash effect is measured exactly."""

    LONG, EDGE, STAKE = 30, 0.08, 0.3

    def __init__(self, agent: str):
        self.agent = agent
        self.hist: Dict[str, List[float]] = {s: [] for s in EQ_SYM.values()}
        self.stock_cash = 0

    def fair(self, sym: str) -> Optional[float]:
        h = self.hist[sym]
        if len(h) <= self.LONG:
            return None
        w = h[-self.LONG:]
        avg = sum(w) / len(w)
        slope = (sum(w[len(w) // 2:]) - sum(w[:len(w) // 2])) / (len(w) // 2) / (len(w) // 2)
        return max(1.0, avg + slope * self.LONG / 2)

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        marks = stock_navs(ref)
        before = ref.get_balance(self.agent, "CR")
        for issuer, sym in EQ_SYM.items():
            self.hist[sym].append(marks[sym]["nav"])
            fair = self.fair(sym)
            if issuer == self.agent or fair is None:
                continue
            book = ref.books["ceres"][sym]
            ask, bid = book.best_ask(), book.best_bid()
            if ask is not None and ask < fair * (1 - self.EDGE):
                qty = int(ref.get_balance(self.agent, "CR") * self.STAKE) // ask
                if qty > 0:
                    order(ref, self.agent, "bid", qty, ask, sym, "ceres", "stk")
            elif bid is not None and bid > fair * (1 + self.EDGE):
                qty = ref.get_balance(self.agent, sym)
                if qty > 0:
                    order(ref, self.agent, "ask", qty, bid, sym, "ceres", "stk")
            cancel_stock_orders(ref, self.agent)
        self.stock_cash += ref.get_balance(self.agent, "CR") - before


STRATEGY = {"hauler": Hauler, "maker": Maker, "idler": Idler, "stock_trader": StockTrader,
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
        contracts: bool = False, dock_fee: int = 0, owned_contracts: bool = False,
        fog: Optional[tuple] = None, peer: bool = False, corporate: bool = False,
        equity_mm: Optional[tuple] = None) -> dict:
    # Reactive depots are the referee's own implementation (agora/referee.py,
    # AGORA_DEPOT_MODEL), so these numbers describe what would ship.
    # corporate: claimed contracts with penalties, debt, distress share
    # auctions and 51% takeovers (Corporate); implies owned contracts and
    # the live 100-share rival cross-holdings.
    ref = AgoraReferee(depots=True, asymmetric=True, depot_model=depot_model,
                       band_pct=band_pct, reactive_bands=reactive_bands,
                       rival_shares=100 if corporate else 0)
    # warmup_rounds is fixed: left unset, new_game draws it from SystemRandom
    # and identical runs diverge (found 2026-09-22; runs before this were not
    # reproducible run to run, only statistically comparable).
    ref.new_game(seed=seed, warmup_rounds=5, depots=True, asymmetric=True, depot_model=depot_model,
                 rival_shares=100 if corporate else 0)
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
    eq_liq = EquityLiquidity(*equity_mm) if equity_mm else None
    traders = [a for a, k in SCENARIOS[scenario].items() if k == "stock_trader"]
    track_stocks = bool(eq_liq or traders)
    stocks_start = {}
    if track_stocks:
        m0 = stock_navs(ref)
        stocks_start = {a: stock_value(ref, a, m0) for a in FLEETS}
    stats = {"transits": 0, "halts_caused": 0, "band_blocked": 0, "stranded_events": 0, "contract_deliveries": 0}
    cboard = (ContractBoard(ref, seed, owned=owned_contracts or corporate, claim=corporate)
              if (contracts or owned_contracts or corporate) else None)
    if cboard is not None:
        cboard.contractors = {a for a, k in SCENARIOS[scenario].items() if k in CONTRACTORS}
    corp = Corporate(ref, cboard, SCENARIOS[scenario], seed) if corporate else None
    fogger = Fog(fog[0], fog[1], seed) if fog else None
    desk = PeerDesk(ref) if peer else None
    ref._sim_contracts = cboard
    first_negative_depot = None
    first_invariant_failure = None
    spread = []  # Earth ORE bid - Ceres ORE ask over time

    t0 = time.time()
    for _ in range(rounds):
        quotes = ref.get_depot_summary()["stations"]
        spread.append((quotes["earth"]["ORE"]["best_bid"] or 0) - (quotes["ceres"]["ORE"]["best_ask"] or 0))
        if cboard is not None:
            cboard.step()
        if fogger is not None:
            fogger.record(quotes)
        views = {a: (fogger.view(ref, a, quotes) if fogger else quotes) for a in FLEETS}
        if corp is not None:
            corp.claim(views)
        if cboard is not None and cboard.owned:
            cboard.trade(views)
        if desk is not None:
            desk.collect()
            desk.match(views)
        # Stock traders act last, after the stand-in players have quoted;
        # with none in the scenario the order is unchanged.
        live = [a for a in fleets if not (corp is not None and a in corp.out)]
        for a in live:
            if a not in traders:
                fleets[a].act(ref, views[a], stats)
        if eq_liq is not None:
            eq_liq.quote(ref, [a for a in live if a not in traders])
        for a in live:
            if a in traders:
                fleets[a].act(ref, views[a], stats)
        ref.step_round()
        if corp is not None:
            corp.settle()
            corp.takeovers()
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
    extra = {}
    if track_stocks:
        m1 = stock_navs(ref)
        extra["stocks"] = {
            "equity_mm": list(equity_mm) if equity_mm else None,
            "fleets": {a: {"value_start": round(stocks_start[a]), "value_end": round(stock_value(ref, a, m1)),
                           "stock_cash": fleets[a].stock_cash if a in traders else None,
                           "stock_pnl": (round(fleets[a].stock_cash + stock_value(ref, a, m1) - stocks_start[a])
                                         if a in traders else None)} for a in FLEETS},
            "nav_end": {sym: m1[sym]["nav"] for sym in EQ_SYM.values()},
        }
    return {**extra,
        "scenario": scenario, "genesis": genesis, "mode": mode, "seed": seed, "rounds": rounds,
        "depot_model": depot_model, "band_pct": ref.circuit_breaker.band_pct,
        "reactive_bands": reactive_bands,
        "dock_fee": dock_fee, "dock_fees_collected": stats.get("dock_fees", 0),
        "contracts": None if cboard is None else {"posted": cboard.posted, "units_delivered": cboard.delivered,
                                                  "paid": cboard.paid, "expired": cboard.expired,
                                                  "owned": cboard.owned, "transfers": cboard.transfers,
                                                  "transfer_cr": cboard.transfer_cr},
        "fog": list(fog) if fog else None,
        "corporate": None if corp is None else dict(corp.stats, debt_end={a: d for a, d in corp.debt.items() if d},
                                                    absorbed=corp.out),
        "peer": None if desk is None else {"trades": desk.trades, "units": desk.units, "cr": desk.cr,
                                           "remote": desk.remote,
                                           "uncollected": sum(desk.pickups.values())},
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
    ap.add_argument("--owned-contracts", action="store_true",
                    help="contracts are awarded to one corp, only it can deliver, and corps can sell them to each other")
    ap.add_argument("--corporate", action="store_true",
                    help="claimed contracts with penalties, debt, distress share auctions, 51%% takeovers")
    ap.add_argument("--peer", action="store_true",
                    help="fleet-to-fleet goods trades agreed at a distance, collected at the seller's station")
    ap.add_argument("--fog", type=float, nargs=2, metavar=("LAG", "NOISE"), default=None,
                    help="remote stations show quotes LAG rounds old, jittered by +/-NOISE (e.g. 3 0.15)")
    ap.add_argument("--dock-fee", type=int, default=0, help="#73 prototype: CR charged per round to each docked fleet")
    ap.add_argument("--free-quotes", action="store_true",
                    help="reactive depots: do not hold quotes inside the circuit-breaker band")
    ap.add_argument("--equity-mm", type=float, nargs=2, metavar=("SPREAD", "DEPTH"), default=None,
                    help="stand-in stock liquidity: non-trader fleets quote rival shares at NAV +/- SPREAD, "
                         "DEPTH shares a side a round (e.g. 0.05 20); the live game has no equity depot")
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
                                       dock_fee=args.dock_fee, owned_contracts=args.owned_contracts,
                                       fog=(int(args.fog[0]), args.fog[1]) if args.fog else None,
                                       peer=args.peer, corporate=args.corporate,
                                       equity_mm=((args.equity_mm[0], int(args.equity_mm[1]))
                                                  if args.equity_mm else None)))
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
        if r.get("stocks"):
            for a, f in r["stocks"]["fleets"].items():
                if f["stock_pnl"] is not None:
                    print(f"  {a} stocks: pnl {f['stock_pnl']:+} (cash {f['stock_cash']:+}, "
                          f"holdings {f['value_start']} -> {f['value_end']} at NAV)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
