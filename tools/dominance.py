#!/usr/bin/env python3
"""
tools/dominance.py - which purchasable is "too obviously good" (#154)?

Ryan, #agent-chat 2026-09-23 10:38: "have we added any mechanics that are TOO
obviously good, like things that all Corps are buying on turn 1?"

For every purchasable (each upgrade tier, escorts, privateers, exchange stock
buys) this plays seeded paired games of the live referee through
tools/economy_sim.py (which builds it with build_referee_from_env, every live
feature on, #155/#159), and compares one treated fleet across three arms:

  never  - the fleet never buys the item
  r1     - the fleet buys it from round 1 (as soon as it is docked and can pay)
  r100   - the same, from round 100

Everything else about the game is identical between arms: same seed, same
scenario, same rival bots. The treated fleet is a Hauler whose purchase
decisions are replaced (Hauler._buy_upgrades / _want_escort); every purchase
goes through the live method the HTTP route calls (upgrades.buy,
initiate_transit(escort=), piracy.hire, submit_envelope on the stock book).
No rule is copied here.

Arm definitions:
- Upgrade tier 1 (shielding, hold, armor, engines): upgrades.buy(kind) from
  round R. No other upgrades in any arm.
- Upgrade tier 2: tier 1 is bought from round 1 in every arm, and tier 2 from
  R. So its "never" arm is the tier-1-only fleet: the table measures the
  marginal tier. The report also gives tier 2 against the bare fleet.
- Escorts: from R, every loaded trip where an escort lowers the raid chance
  and the fleet can pay fee + toll (an unaffordable escort would get the move
  rejected).
- Privateers: from R, Privateer._hire, the sim's live-method privateer bot
  (hire against the richest rival when it has 3x PRIV_COST and none running).
- Stock buy: from R, once: STOCK_STAKE of available CR into the richest
  rival's shares at the best ask on the live exchange, held to the end.

Net worth: economy_sim.score (cash, goods at reference prices, escrow,
upgrades at book value) plus rival shares held at the board mark, the same
definition in every arm and every round. Each fleet starts with 100 shares
of every rival, so the stock term matters in every arm.

Metrics, per item, pooled over the scenario cells x seeds:
- d_median: median(final NW, r1) - median(final NW, never). Same vs r100.
- win%: share of pairs where r1 ends strictly above the other arm. A seed
  whose buy never happened counts as a loss.
- payback: per seed, rounds from the buy to the first round from which r1's
  NW stays at or above never's to the end. Median over seeds that bought;
  "never" when most of them never pay back.
- FLAG when r1 beats never in more than FLAG_WIN of pairs.

Usage:
  python3 tools/dominance.py                    # 20 seeds, default cells, all items
  python3 tools/dominance.py --seeds 30 --jobs 8 --json out.json
"""

import argparse
import json
import math
import os
import statistics
import sys
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import economy_sim as sim  # noqa: E402
import agora.peer as peer_mod  # noqa: E402
from agora import piracy as piracy_mod  # noqa: E402
from agora.upgrades import CATALOG as UPGRADES  # noqa: E402

TREATED = "amos"
# The sim's default scenarios minus idle4, where no fleet acts. Strict mode
# only: #159 found strict and tolerant play identically at the live band.
CELLS = [("mixed", "flat"), ("mixed", "planet"), ("haulers4", "flat"), ("haulers4", "planet")]
MODE = "strict"
LATE_ROUND = 100
STOCK_STAKE = 0.3   # the sim's StockTrader.STAKE
FLAG_WIN = 0.70

# item -> (kind, detail). Tier-2 items carry their tier-1 prerequisite.
ITEMS: Dict[str, dict] = {}
for _k in ("shielding", "hold", "armor", "engines"):
    for _t, _p in enumerate(UPGRADES[_k]["prices"], start=1):
        ITEMS[f"{_k} t{_t}"] = {"type": "upgrade", "kind": _k, "tier": _t, "price": _p,
                                "prereq": [(_k, _t - 1)] if _t > 1 else []}
ITEMS["escorts"] = {"type": "escort", "price": f"{int(piracy_mod.ESCORT_PCT * 100)}% of cargo a trip"}
ITEMS["privateers"] = {"type": "privateer", "price": piracy_mod.PRIV_COST}
ITEMS["stock buy"] = {"type": "stock", "price": f"{int(STOCK_STAKE * 100)}% of cash"}


def net_worth(ref, agent: str) -> float:
    marks = sim.stock_navs(ref)
    return sim.score(ref, agent) + sim.stock_value(ref, agent, marks, "mark")


class TreatedFleet(sim.Hauler):
    """A hauler that buys exactly one item, from round `start`, and nothing
    else (plus the item's tier-1 prerequisite from round 1)."""

    def __init__(self, agent: str, item: Optional[str], start: Optional[int], tolerate_halts: bool = False):
        super().__init__(agent, tolerate_halts=tolerate_halts)
        self.item = ITEMS[item] if item else None
        self.start = start
        self.r0: Optional[int] = None
        self.bought_round: Optional[int] = None

    def rel_round(self, ref) -> int:
        return ref.current_round - self.r0 + 1

    def active(self, ref) -> bool:
        return self.item is not None and self.rel_round(ref) >= self.start

    def _mark_bought(self, ref) -> None:
        if self.bought_round is None:
            self.bought_round = self.rel_round(ref)

    def act(self, ref, quotes, stats) -> None:
        if self.r0 is None:
            self.r0 = ref.current_round
        if self.item and self.item["type"] == "privateer" and self.active(ref):
            before = self._hires(ref)
            sim.Privateer._hire(self, ref, stats)
            if self._hires(ref) > before:
                self._mark_bought(ref)
        super().act(ref, quotes, stats)

    def _hires(self, ref) -> int:
        return ref.conn.execute("SELECT COUNT(*) FROM piracy_privateers WHERE sponsor = ?",
                                (self.agent,)).fetchone()[0]

    # --- purchase hooks (called by Hauler.act after selling, while docked)
    def _buy_upgrades(self, ref, stats) -> None:
        it = self.item
        if not it or not ref.upgrades_enabled or sim.debt(ref, self.agent) > 0:
            return
        if it["type"] == "upgrade":
            targets = [(k, t) for k, t in it["prereq"]]
            if self.active(ref):
                targets.append((it["kind"], it["tier"]))
            for kind, tier in targets:
                while ref.upgrades.tier(self.agent, kind) < tier:
                    if ref.upgrades.buy(self.agent, kind).get("kind") != "upgrade_ok":
                        return
                    stats["upgrades_bought"] = stats.get("upgrades_bought", 0) + 1
                    if (kind, tier) == (it["kind"], it["tier"]) and ref.upgrades.tier(self.agent, kind) == tier:
                        self._mark_bought(ref)
        elif it["type"] == "stock" and self.active(ref) and self.bought_round is None:
            self._buy_stock(ref)

    def _buy_stock(self, ref) -> None:
        nw = {e["agent_id"]: e["net_worth"] for e in ref.get_leaderboard()}
        rivals = sorted((b for b in sim.FLEETS if b != self.agent and not ref.fleet_out(b)), key=lambda b: -nw.get(b, 0))
        for target in rivals:
            sym = sim.EQ_SYM[target]
            ask = ref.books["ceres"][sym].best_ask()
            if ask is None:
                continue
            qty = int(sim.available(ref, self.agent, "CR") * STOCK_STAKE) // ask
            if qty <= 0:
                return
            before = ref.get_balance(self.agent, sym)
            sim.order(ref, self.agent, "bid", qty, ask, sym, "ceres", "dom")
            sim.cancel_stock_orders(ref, self.agent)
            if ref.get_balance(self.agent, sym) > before:
                self._mark_bought(ref)
            return

    def _want_escort(self, ref, st: str, dest: str, comm: str, qty: int) -> bool:
        if not (self.item and self.item["type"] == "escort" and self.active(ref)):
            return False
        if not ref.piracy.enabled or qty <= 0:
            return False
        r = ref.current_round
        route = sim.get_route(st, dest, r)
        toll = route.get("toll", 0) if route else 0
        tolled = bool(toll)
        bare = ref.piracy.chance(self.agent, st, dest, tolled, comm, qty, False, r)
        guarded = ref.piracy.chance(self.agent, st, dest, tolled, comm, qty, True, r)
        if guarded["odds"] >= bare["odds"]:
            return False
        if sim.available(ref, self.agent, "CR") < ref.piracy.escort_fee(comm, qty) + toll:
            return False
        self._mark_bought(ref)
        return True


class _SequentialIds:
    """Stands in for the uuid module inside agora/peer.py for one game.

    The peer desk names an escrow f"x{uuid4().hex[:6]}" and lists offers
    ORDER BY created_round, escrow_id, so two offers in one round come back
    in random order, and paired games drift apart for no reason. 24 random
    bits also collide (a UNIQUE constraint failure crashed a 1,680-game run).
    A counter in the top bits keeps ids unique and in creation order. This
    changes no rule, only which random name an offer gets."""

    def __init__(self):
        self.n = 0

    def uuid4(self):
        self.n += 1
        return uuid.UUID(int=self.n << 104)


def play(job: Tuple[str, str, int, int, Optional[str], Optional[int]]) -> dict:
    """One game: (scenario, genesis, seed, rounds, item, start round)."""
    saved = peer_mod.uuid
    peer_mod.uuid = _SequentialIds()
    try:
        return _play(job)
    except Exception:
        scenario, genesis, seed, rounds, item, start = job
        return {"scenario": scenario, "genesis": genesis, "seed": seed, "item": item, "start": start,
                "error": traceback.format_exc()}
    finally:
        peer_mod.uuid = saved


def _play(job: Tuple[str, str, int, int, Optional[str], Optional[int]]) -> dict:
    scenario, genesis, seed, rounds, item, start = job
    fleet: Dict[str, TreatedFleet] = {}

    def factory(agent, kind, seed_, mode):
        fleet["f"] = TreatedFleet(agent, item, start, tolerate_halts=(mode == "tolerant"))
        return fleet["f"]

    traj: List[float] = []
    res = sim.run(scenario, genesis, seed, rounds, MODE, fleet_overrides={TREATED: factory},
                  on_round=lambda ref: traj.append(net_worth(ref, TREATED)))
    return {"scenario": scenario, "genesis": genesis, "seed": seed, "item": item, "start": start,
            "traj": traj, "final": traj[-1], "bought_round": fleet["f"].bought_round,
            "holdings": (res.get("upgrades") or {}).get(TREATED),
            "invariant_failure": res["first_invariant_failure"]}


def arms() -> List[Tuple[Optional[str], Optional[int]]]:
    out: List[Tuple[Optional[str], Optional[int]]] = [(None, None)]
    for item in ITEMS:
        out += [(item, 1), (item, LATE_ROUND)]
    return out


def payback(treated: List[float], control: List[float], bought: Optional[int]) -> float:
    """Rounds from the buy until treated stays >= control to the end; inf if it ends behind."""
    if bought is None:
        return math.inf
    diff = [t - c for t, c in zip(treated, control)]
    if diff[-1] < 0:
        return math.inf
    t = len(diff)
    while t > 0 and diff[t - 1] >= 0:
        t -= 1
    # diff[t:] are all >= 0; rounds are 1-based, so round t + 1.
    return max(0, (t + 1) - bought)


def summarize(results: List[dict], cells: List[Tuple[str, str]]) -> Dict[str, Any]:
    idx = {(r["scenario"], r["genesis"], r["seed"], r["item"], r["start"]): r for r in results}
    seeds = sorted({r["seed"] for r in results})
    rows = {}
    for item, spec in ITEMS.items():
        ctrl_item, ctrl_start = (None, None)
        if spec["type"] == "upgrade" and spec["tier"] > 1:
            ctrl_item, ctrl_start = f"{spec['kind']} t{spec['tier'] - 1}", 1
        early, late, never, bare, paybacks, bought_rounds = [], [], [], [], [], []
        wins_never = wins_late = wins_bare = 0
        per_cell = {}
        for sc, gen in cells:
            cw = 0
            for s in seeds:
                e = idx[(sc, gen, s, item, 1)]
                l = idx[(sc, gen, s, item, LATE_ROUND)]
                n = idx[(sc, gen, s, ctrl_item, ctrl_start)]
                b = idx[(sc, gen, s, None, None)]
                early.append(e["final"]); late.append(l["final"]); never.append(n["final"]); bare.append(b["final"])
                bought = e["bought_round"] is not None
                w = bought and e["final"] > n["final"]
                wins_never += w
                cw += w
                wins_late += bought and e["final"] > l["final"]
                wins_bare += bought and e["final"] > b["final"]
                if bought:
                    bought_rounds.append(e["bought_round"])
                    paybacks.append(payback(e["traj"], n["traj"], e["bought_round"]))
            per_cell[f"{sc}/{gen}"] = cw / len(seeds)
        n_pairs = len(early)
        pb = statistics.median(paybacks) if paybacks else math.inf
        rows[item] = {
            "price": spec["price"],
            "control": ctrl_item or "never",
            "pairs": n_pairs,
            "bought": len(bought_rounds),
            "median_buy_round": statistics.median(bought_rounds) if bought_rounds else None,
            "d_median_vs_never": statistics.median(early) - statistics.median(never),
            "win_vs_never": wins_never / n_pairs,
            "d_median_vs_r100": statistics.median(early) - statistics.median(late),
            "win_vs_r100": wins_late / n_pairs,
            "d_median_vs_bare": statistics.median(early) - statistics.median(bare),
            "win_vs_bare": wins_bare / n_pairs,
            "payback": None if math.isinf(pb) else pb,
            "paid_back": sum(1 for p in paybacks if not math.isinf(p)),
            "win_vs_never_by_cell": per_cell,
            "flag": wins_never / n_pairs > FLAG_WIN,
        }
    fails = [(r["scenario"], r["genesis"], r["seed"], r["item"], r["start"], r["invariant_failure"])
             for r in results if r["invariant_failure"]]
    return {"rows": rows, "invariant_failures": fails, "games": len(results), "seeds": seeds,
            "cells": [f"{a}/{b}" for a, b in cells]}


def markdown(summary: Dict[str, Any]) -> str:
    def pct(x):
        return f"{round(x * 100)}%"

    def cr(x):
        return f"{x:+,.0f}"

    out = ["| item | price (CR) | control | bought (of pairs) | median buy round | Δ median NW r1 vs never "
           "| r1 wins vs never | Δ median NW r1 vs r100 | r1 wins vs r100 | payback (rounds after buy) | flag |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for item, r in summary["rows"].items():
        pb = "never" if r["payback"] is None else f"{r['payback']:.0f}"
        pb += f" ({r['paid_back']}/{r['bought']} paid back)"
        price = f"{r['price']:,}" if isinstance(r["price"], int) else r["price"]
        out.append(f"| {item} | {price} | {r['control']} | {r['bought']}/{r['pairs']} | "
                   f"{r['median_buy_round'] if r['median_buy_round'] is not None else '-'} | "
                   f"{cr(r['d_median_vs_never'])} | {pct(r['win_vs_never'])} | {cr(r['d_median_vs_r100'])} | "
                   f"{pct(r['win_vs_r100'])} | {pb} | {'**FLAG**' if r['flag'] else ''} |")
    cells = summary["cells"]
    out += ["", "r1 wins vs never, by cell:", "",
            "| item | " + " | ".join(cells) + " | tier 2 vs bare fleet |",
            "|---|" + "---|" * (len(cells) + 1)]
    for item, r in summary["rows"].items():
        vb = f"{cr(r['d_median_vs_bare'])}, {pct(r['win_vs_bare'])}" if r["control"] != "never" else ""
        out.append(f"| {item} | " + " | ".join(pct(r["win_vs_never_by_cell"][c]) for c in cells) + f" | {vb} |")
    return "\n".join(out)


def main() -> int:
    global ITEMS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--item", action="append", choices=list(ITEMS), help="limit to these items")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--json", help="write the raw summary here")
    ap.add_argument("--price", action="append", default=[], metavar="'ITEM=CR'",
                    help="what-if: price an upgrade tier differently for this run only, e.g. 'hold t1=8000' "
                         "(edits agora.upgrades.CATALOG in this process; the live constant is untouched)")
    args = ap.parse_args()
    if args.rounds <= LATE_ROUND:
        ap.error(f"--rounds must exceed {LATE_ROUND}")
    for spec in args.price:
        name, _, cr = spec.rpartition("=")
        it = ITEMS.get(name.strip())
        if not it or it["type"] != "upgrade":
            ap.error(f"--price takes an upgrade tier, e.g. 'hold t1=8000', not {spec!r}")
        UPGRADES[it["kind"]]["prices"][it["tier"] - 1] = it["price"] = int(cr)
    if args.item:
        keep = set(args.item)
        for it in list(keep):
            for k, t in ITEMS[it].get("prereq", []):
                keep.add(f"{k} t{t}")
        ITEMS = {k: v for k, v in ITEMS.items() if k in keep}
    jobs = [(sc, gen, s, args.rounds, item, start)
            for sc, gen in CELLS for s in range(1, args.seeds + 1) for item, start in arms()]
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        results = list(ex.map(play, jobs, chunksize=4))
    errors = [r for r in results if "error" in r]
    if errors:
        for r in errors[:3]:
            print(r["error"], file=sys.stderr)
        print(f"{len(errors)} of {len(results)} games raised; no table", file=sys.stderr)
        return 1
    summary = summarize(results, CELLS)
    print(markdown(summary))
    flagged = [k for k, r in summary["rows"].items() if r["flag"]]
    print(f"\nflagged (r1 beats never in > {int(FLAG_WIN * 100)}% of pairs): {', '.join(flagged) or 'none'}")
    print(f"games: {summary['games']}, invariant failures: {len(summary['invariant_failures'])}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(summary, f, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
