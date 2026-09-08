#!/usr/bin/env python3
"""
fuzz_harness.py — adversarial invariant fuzzer for AgoraReferee.

task-1788749481: build adversarial harness / invariant fuzzers against
the referee's order/book/settlement path. Runs entirely in-process
against a standalone AgoraReferee(':memory:') instance (no HTTP, no
Railway) — no live deploy dependency, matches Zero's "local test
harness first, remote barrage second."

Strategy: random walk of order submissions across the three real
agent_ids (amos, marvin, zero), deliberately including malformed,
duplicate, self-crossing, oversized, and adversarial-price orders,
checking after every single submission (not just at the end) that:

  1. verify_ledger_invariants() reports clean (conservation,
     non-negativity, account/ledger reconciliation — referee's own
     built-in checks).
  2. current_seq is monotonic non-decreasing.
  3. The resting book never rests a crossed spread (best_bid < best_ask
     whenever both sides are non-empty) — a matching-engine invariant
     the referee's own checker doesn't cover.
  4. Every response is a well-formed envelope (`v`, `kind` present).

No hypothesis dependency (not in this repo's requirements) — plain
seeded `random` for reproducibility, which is enough for an in-process
harness this size.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agora.referee import AgoraReferee

AGENTS = ["amos", "marvin", "zero"]


def random_order(rng: random.Random, i: int) -> dict:
    agent = rng.choice(AGENTS)
    kind_roll = rng.random()

    if kind_roll < 0.05:
        # malformed: missing field
        payload = {"order_id": f"fz-{i}", "agent_id": agent}
    elif kind_roll < 0.10:
        # malformed: wrong types
        payload = {
            "order_id": f"fz-{i}",
            "agent_id": agent,
            "instrument": "BANANA",
            "side": rng.choice(["bid", "ask"]),
            "qty": rng.choice([-5, 0, "ten", 3.5]),
            "limit_price": rng.choice([-1, 0, "x"]),
        }
    elif kind_roll < 0.15:
        # unsupported instrument
        payload = {
            "order_id": f"fz-{i}",
            "agent_id": agent,
            "instrument": rng.choice(["FRAG", "DOGE", ""]),
            "side": rng.choice(["bid", "ask"]),
            "qty": rng.randint(1, 50),
            "limit_price": rng.randint(1, 50),
        }
    elif kind_roll < 0.20:
        # deliberately huge order — probes solvency/committed-funds math
        payload = {
            "order_id": f"fz-{i}",
            "agent_id": agent,
            "instrument": "BANANA",
            "side": rng.choice(["bid", "ask"]),
            "qty": rng.randint(10_000, 1_000_000),
            "limit_price": rng.randint(10_000, 1_000_000),
        }
    else:
        # well-formed order in a plausible range, sometimes re-using a
        # recent order_id to exercise the idempotent-dedup / conflicting-
        # duplicate path
        reuse = rng.random() < 0.1 and i > 0
        payload = {
            "order_id": f"fz-{i - rng.randint(1, min(i, 5))}" if reuse else f"fz-{i}",
            "agent_id": agent,
            "instrument": "BANANA",
            "side": rng.choice(["bid", "ask"]),
            "qty": rng.randint(1, 200),
            "limit_price": rng.randint(1, 30),
            "seq_seen": rng.choice([0, 1, 999999]),  # includes deliberately stale/future seq
        }

    return {
        "v": 1,
        "kind": "order",
        "reply": "none",
        "floor": "open",
        "scope": "channel",
        "subject": "agent-collaborative-project",
        "payload": payload,
    }


def check_book_not_crossed(ref: AgoraReferee) -> str | None:
    book = ref.book
    if book.bids and book.asks:
        best_bid = book.bids[0].limit_price
        best_ask = book.asks[0].limit_price
        if best_bid >= best_ask:
            return f"crossed resting book: best_bid={best_bid} >= best_ask={best_ask}"
    return None


def run(seed: int, n: int) -> tuple[int, list[str]]:
    rng = random.Random(seed)
    ref = AgoraReferee(":memory:")
    failures: list[str] = []
    last_seq = -1

    for i in range(n):
        envelope = random_order(rng, i)
        try:
            resp = ref.submit_envelope(envelope)
        except Exception as e:  # a raised exception is itself a finding
            failures.append(f"step {i}: submit_envelope raised {type(e).__name__}: {e} | envelope={envelope}")
            continue

        if not isinstance(resp, dict) or "v" not in resp or "kind" not in resp:
            failures.append(f"step {i}: malformed response envelope: {resp!r}")

        seq = ref.current_seq
        if seq < last_seq:
            failures.append(f"step {i}: seq went backwards ({last_seq} -> {seq})")
        last_seq = seq

        ok, errors = ref.verify_ledger_invariants()
        if not ok:
            for e in errors:
                failures.append(f"step {i}: {e}")

        crossed = check_book_not_crossed(ref)
        if crossed:
            failures.append(f"step {i}: {crossed}")

    return n, failures


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    total_failures = []
    seeds_run = 0
    for seed in range(5):
        seeds_run += 1
        count, failures = run(seed, n)
        if failures:
            print(f"seed={seed}: {len(failures)} invariant violation(s) across {count} orders")
            for f in failures[:20]:
                print(f"  - {f}")
            total_failures.extend(failures)
        else:
            print(f"seed={seed}: {count} orders, 0 invariant violations")

    print()
    if total_failures:
        print(f"FUZZ FAILED: {len(total_failures)} total violations across {seeds_run} seeds x {n} orders")
        sys.exit(1)
    else:
        print(f"FUZZ PASSED: {seeds_run} seeds x {n} orders/steps, 0 invariant violations "
              f"(conservation, non-negativity, reconciliation, seq-monotonicity, uncrossed-book)")
        sys.exit(0)
