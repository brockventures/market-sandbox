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
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
                bid = quotes[dest][comm]["best_bid"]
                if not ask or not bid:
                    continue
                if not band_ok(ref, st, comm, ask) and not self.tolerate_halts:
                    stats["band_blocked"] += 1
                    continue
                qty = min(quotes[st][comm]["ask_depth"] or 0, 500, max(0, (inv["CR"] - 200) // ask))
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

    def __init__(self, agent: str, clip: int = 50):
        self.agent = agent
        self.clip = clip

    def act(self, ref: AgoraReferee, quotes, stats) -> None:
        st = location(ref, self.agent)
        if st is None:
            return
        cancel_all(ref, self.agent)
        inv = inventory(ref, self.agent)
        for comm in TRADED:
            q = quotes[st][comm]
            if q["best_bid"] and inv["CR"] > q["best_bid"] * self.clip * 4 and band_ok(ref, st, comm, q["best_bid"]):
                order(ref, self.agent, "bid", self.clip, q["best_bid"], comm, st, "mb")
            if q["best_ask"] and inv[comm] >= self.clip and band_ok(ref, st, comm, q["best_ask"]):
                order(ref, self.agent, "ask", self.clip, q["best_ask"], comm, st, "ma")


class Idler:
    def __init__(self, agent: str):
        self.agent = agent

    def act(self, ref, quotes, stats) -> None:
        return


STRATEGY = {"hauler": Hauler, "maker": Maker, "idler": Idler}


# ------------------------------------------------------------ reactive depots

class ReactiveDepots:
    """Prototype depot model, simulator-only (the referee is untouched):

    * finite shelf: each depot sells from a shelf of at most TARGET units,
      restocked by a per-round production drip (the cheapest station for a
      good produces the most);
    * finite appetite: each depot buys into a hold of at most TARGET units,
      drained by a per-round consumption drip (the dearest station consumes
      the most);
    * inventory-skewed prices: the ask rises as the shelf empties and the
      bid falls as the hold fills.

    Installed by replacing the referee's _refresh_depot_orders_locked, which
    step_round() already calls every round.
    """

    TARGET = 2000
    MAIN_DRIP = 100
    SIDE_DRIP = 20
    SKEW = 0.5

    def __init__(self, ref: AgoraReferee):
        from agora.order_book import Order  # noqa: F401  (import check)
        self.ref = ref
        self.shelf = {}
        self.hold = {}
        self.last = {}
        self.prod = {}
        self.cons = {}
        for c in TRADED:
            cheap = min(STATIONS, key=lambda s: BASE_PRICES[s][c])
            dear = max(STATIONS, key=lambda s: BASE_PRICES[s][c])
            for st in STATIONS:
                self.shelf[(st, c)] = self.TARGET // 2
                self.hold[(st, c)] = 0
                self.prod[(st, c)] = self.MAIN_DRIP if st == cheap else self.SIDE_DRIP
                self.cons[(st, c)] = self.MAIN_DRIP if st == dear else self.SIDE_DRIP

    def install(self) -> None:
        self.ref._refresh_depot_orders_locked = self.refresh_locked
        with self.ref.lock, self.ref.conn:
            self.refresh_locked()

    def refresh_locked(self) -> None:
        from agora.order_book import Order, OrderBook
        ref = self.ref
        for st in STATIONS:
            depot = f"depot_{st}"
            cr_budget = max(0, ref.get_balance(depot, "CR"))
            for c in TRADED:
                key = (st, c)
                bal = ref.get_balance(depot, c)
                if key in self.last:
                    moved = bal - self.last[key]
                    if moved < 0:
                        self.shelf[key] = max(0, self.shelf[key] + moved)
                    elif moved > 0:
                        self.hold[key] += moved
                self.shelf[key] = min(self.TARGET, self.shelf[key] + self.prod[key])
                self.hold[key] = max(0, self.hold[key] - self.cons[key])

                book = ref.books.setdefault(st, {}).setdefault(c, OrderBook(instrument=c))
                book.bids = [o for o in book.bids if o.agent_id != depot]
                book.asks = [o for o in book.asks if o.agent_id != depot]
                ref.conn.execute("DELETE FROM orders WHERE agent_id = ? AND station_id = ? AND instrument = ? AND status = 'open'",
                                 (depot, st, c))

                spot = ref.spatial.get_station_price(st, c) if ref.spatial else BASE_PRICES[st][c]
                shelf_ratio = self.TARGET / max(self.shelf[key], self.TARGET * 0.05)
                ask = max(2, round(spot * 1.03 * min(3.0, shelf_ratio ** self.SKEW)))
                bid = max(1, min(ask - 1, round(spot * 0.97 * (self.TARGET / (self.TARGET + self.hold[key])) ** self.SKEW)))
                ask_qty = min(self.shelf[key], max(0, bal))
                bid_qty = min(max(0, self.TARGET - self.hold[key]), cr_budget // bid)
                cr_budget -= bid_qty * bid

                seq = ref.current_seq
                for side, price, qty in (("bid", bid, bid_qty), ("ask", ask, ask_qty)):
                    if qty <= 0:
                        continue
                    oid = f"{depot}-{c.lower()}-{side}-r{ref.current_round}-rx"
                    o = Order(order_id=oid, agent_id=depot, instrument=c, side=side,
                              qty=qty, limit_price=price, seq_seen=seq)
                    (book._insert_bid if side == "bid" else book._insert_ask)(o)
                    ref.conn.execute(
                        "INSERT OR REPLACE INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen, status, resolved_seq, filled_qty, station_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, 0, ?)", (oid, depot, c, side, qty, price, seq, st))
                self.last[key] = bal


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
        depot_model: str = "static", band_pct: Optional[float] = None) -> dict:
    ref = AgoraReferee(depots=True, asymmetric=True)
    ref.new_game(seed=seed, depots=True, asymmetric=True)
    if genesis == "planet":
        apply_planet_genesis(ref)
    if band_pct is not None:
        ref.circuit_breaker.band_pct = band_pct
    if depot_model == "reactive":
        ReactiveDepots(ref).install()
    fleets = {a: (Hauler(a, tolerate_halts=(mode == "tolerant")) if kind == "hauler" else STRATEGY[kind](a))
              for a, kind in SCENARIOS[scenario].items()}
    start = {a: score(ref, a) for a in FLEETS}
    stats = {"transits": 0, "halts_caused": 0, "band_blocked": 0, "stranded_events": 0}
    first_negative_depot = None
    first_invariant_failure = None
    spread = []  # Earth ORE bid - Ceres ORE ask over time

    t0 = time.time()
    for _ in range(rounds):
        quotes = ref.get_depot_summary()["stations"]
        spread.append((quotes["earth"]["ORE"]["best_bid"] or 0) - (quotes["ceres"]["ORE"]["best_ask"] or 0))
        for strat in fleets.values():
            strat.act(ref, quotes, stats)
        ref.step_round()
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
        "seconds": round(elapsed, 2),
        "fleets": {a: {"strategy": SCENARIOS[scenario][a], "start": round(start[a]), "end": round(end[a]),
                       "pnl": round(end[a] - start[a]), "leaderboard_nw": board.get(a)} for a in FLEETS},
        "fills": classify_fills(ref),
        "transits": stats["transits"],
        "halts_total": halts,
        "band_blocked_checks": stats["band_blocked"],
        "stranded_events": stats["stranded_events"],
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
    ap.add_argument("--json", action="store_true", help="print raw results as JSON")
    ap.add_argument("--hang-timeout", type=int, default=300, help="dump stacks and exit if a run hangs")
    args = ap.parse_args()

    if args.drip:
        ReactiveDepots.MAIN_DRIP, ReactiveDepots.SIDE_DRIP = args.drip
    faulthandler.dump_traceback_later(args.hang_timeout, exit=True)
    results = []
    for scen in args.scenario or ["mixed", "haulers4", "idle4"]:
        for gen in args.genesis or ["flat", "planet"]:
            for mode in (args.mode or ["strict", "tolerant"]) if scen != "idle4" else ["strict"]:
                for seed in range(1, args.seeds + 1):
                    results.append(run(scen, gen, seed, args.rounds, mode,
                                       depot_model=args.depot_model, band_pct=args.band_pct))
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
        print(f"  first negative depot balance: round {r['first_negative_depot_round']}  "
              f"invariant failure: {r['first_invariant_failure']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
