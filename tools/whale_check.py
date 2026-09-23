#!/usr/bin/env python3
"""
tools/whale_check.py - can a rich stock trader buy a takeover through the
exchange alone? (#187 track 1)

The exchange's quotes deepen with traded volume (agora/exchange.py), so a
trader with capital can deploy more of it. The risk: deeper quotes turn the
exchange into a warehouse a raider buys 51% out of. The exchange never
holds more than MAX_SHARES (200) of a stock, and a rival starts with 100,
so a raider reaches at most 100 + 200 from the other two rivals + whatever
the exchange sells it. This check measures that on the live game.

The whale sits in the `styles` scenario's stock-trader seat (seed-rotated,
every other seat is its usual bot) with WHALE_CR extra credits, granted from
SYSTEM on a balanced ledger leg. Every round it sells its goods like the
stock trader and then buys every share of one target stock the book offers,
at up to twice the best ask. It never sells.

Variants:
  styles        the game as it is
  issuer_dumps  the target's issuer also sells its own treasury shares to
                the exchange's bid every round (the briefing allows it). The
                exchange's cap bounds its holdings at any moment, not the
                flow through it, so this measures the conduit.

Measured after every order any fleet submits (every fill goes through
submit_envelope) and after every round (auction uncrosses happen in
step_round): the whale's peak holding and the exchange's peak holding of
the target.

Usage:
  python3 tools/whale_check.py --seeds 20 --seed-start 1 --jobs 8
"""

import argparse
import os
import statistics
import sys
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import economy_sim as sim  # noqa: E402
from agora.exchange import EXCHANGE_ID  # noqa: E402

WHALE_CR = 150_000
# total_shares // 2 + 1 of 1,000. agora/corporate.py's TAKEOVER_SHARES is 510;
# the stricter number is the one checked here.
TAKEOVER_AT = 501


class Whale(sim.StockTrader):
    def __init__(self, agent: str, target: str, issuer_dumps: bool, track: Dict[str, Any]):
        super().__init__(agent)
        self.target, self.issuer_dumps, self.track = target, issuer_dumps, track
        self.sym = sim.EQ_SYM[target]
        self.funded = False

    def _fund(self, ref) -> None:
        with ref.lock, ref.conn:
            seq = ref.current_seq
            for acct, d in ((self.agent, WHALE_CR), ("SYSTEM", -WHALE_CR)):
                ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, 'CR', 0)",
                                 (acct,))
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'",
                                 (d, acct))
                ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) "
                                 "VALUES ('whale-grant', ?, ?, 'CR', ?)", (seq, acct, d))
        # Watch every order any fleet submits from here on.
        orig = ref.submit_envelope

        def watched(env, *a, **kw):
            out = orig(env, *a, **kw)
            self.observe(ref)
            return out
        ref.submit_envelope = watched
        self.funded = True

    def observe(self, ref) -> None:
        t = self.track
        t["whale_max"] = max(t.get("whale_max", 0), ref.get_balance(self.agent, self.sym))
        t["exchange_max"] = max(t.get("exchange_max", 0), ref.get_balance(EXCHANGE_ID, self.sym))
        t["issuer_min"] = min(t.get("issuer_min", 10 ** 9), ref.get_balance(self.target, self.sym))

    def act(self, ref, quotes, stats) -> None:
        if not self.funded:
            self._fund(ref)
        self._liquidate(ref, quotes)
        book = ref.books["ceres"][self.sym]
        if self.issuer_dumps and not ref.fleet_out(self.target):
            bid = book.best_bid()
            own = sim.available(ref, self.target, self.sym)
            if bid and own > 0:
                sim.order(ref, self.target, "ask", own, bid, self.sym, "ceres", "dump")
                sim.cancel_stock_orders(ref, self.target)
        ask = book.best_ask()
        cash = sim.available(ref, self.agent, "CR")
        if ask and cash >= ask:
            limit = 2 * ask
            sim.order(ref, self.agent, "bid", cash // limit, limit, self.sym, "ceres", "whale")
        sim.cancel_stock_orders(ref, self.agent)
        self.observe(ref)


def one(seed: int, rounds: int, variant: str) -> Dict[str, Any]:
    kinds = sim.scenario_kinds("styles", seed)
    whale = next(a for a, k in kinds.items() if k == "stock_trader")
    # Rotate the target with the seed.
    rivals = [a for a in sim.FLEETS if a != whale]
    target = rivals[seed % len(rivals)]
    track: Dict[str, Any] = {}
    bots: List[Whale] = []

    def factory(agent, kind, s, mode):
        bots.append(Whale(agent, target, variant == "issuer_dumps", track))
        return bots[-1]

    def on_round(ref):
        for b in bots:
            b.observe(ref)
        if ref.corporate_enabled and "takeover_round" not in track:
            row = ref.conn.execute("SELECT status, absorbed_by FROM corp_status WHERE agent_id = ?",
                                   (target,)).fetchone()
            if row and row[0] == "absorbed":
                track["takeover_round"] = ref.current_round
                track["absorbed_by"] = row[1]

    r = sim.run("styles", "flat", seed=seed, rounds=rounds, fleet_overrides={whale: factory},
                on_round=on_round)
    return {"seed": seed, "variant": variant, "whale": whale, "target": target,
            "whale_max": track.get("whale_max", 0), "exchange_max": track.get("exchange_max", 0),
            "issuer_min": track.get("issuer_min"), "takeover_round": track.get("takeover_round"),
            "absorbed_by": track.get("absorbed_by"),
            "invariant_failure": r["first_invariant_failure"]}


def _job(kw):
    return one(**kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--seed-start", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--variant", choices=["styles", "issuer_dumps"], action="append")
    ap.add_argument("--jobs", type=int, default=1)
    args = ap.parse_args()
    jobs = [dict(seed=s, rounds=args.rounds, variant=v)
            for v in (args.variant or ["styles", "issuer_dumps"])
            for s in range(args.seed_start, args.seed_start + args.seeds)]
    if args.jobs > 1:
        import multiprocessing
        with multiprocessing.get_context("fork").Pool(args.jobs) as pool:
            res: List[Dict[str, Any]] = pool.map(_job, jobs, chunksize=1)
    else:
        res = [_job(j) for j in jobs]
    print("| variant | seeds | whale peak (median / max) | reached 501 | exchange peak (max) | takeovers | ledger ok |")
    print("|---|---|---|---|---|---|---|")
    for v in dict.fromkeys(r["variant"] for r in res):
        rs = [r for r in res if r["variant"] == v]
        wm = [r["whale_max"] for r in rs]
        print(f"| {v} | {len(rs)} | {statistics.median(wm):.0f} / {max(wm)} | "
              f"{sum(w >= TAKEOVER_AT for w in wm)} | {max(r['exchange_max'] for r in rs)} | "
              f"{sum(r['takeover_round'] is not None for r in rs)} | "
              f"{sum(r['invariant_failure'] is None for r in rs)}/{len(rs)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
