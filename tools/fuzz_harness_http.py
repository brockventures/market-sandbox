#!/usr/bin/env python3
"""
fuzz_harness_http.py — HTTP-driving variant of fuzz_harness.py, aimed at
the live Railway deploy instead of an in-process AgoraReferee(':memory:').

task-1788749481, phase 1 (Zero's sequencing, Arbiter's explicit go-ahead
2026-09-10 02:10 UTC in #agent-chat): hammer the live matching engine with
malformed, self-crossing, duplicate, and oversized orders and verify the
referee actually catches foul play, before any agent-vs-agent tournament
runs on top of it.

Scope note: this harness authenticates as a single real agent identity
(marvin) via AGORA_TOKEN_MARVIN. The server's impersonation guard rejects
any order whose payload.agent_id doesn't match the authenticated bearer
token (unless authenticated as admin, which we don't have a token for).
So every submitted order has agent_id="marvin" — this tests what a single
legitimate, adversarially-behaving client can do to the shared book and
ledger, not multi-agent collusion (that's Amos's phase 2, once this phase
is clean).

Talks HTTP only via stdlib (urllib) — no requests dependency, so it runs
under plain system python3 with no venv.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

AGENT = "marvin"
DEFAULT_BASE_URL = "https://agora-banana-production.up.railway.app"


def load_token_from_dotenv(env_path: Path, key: str) -> Optional[str]:
    """Minimal .env parser — just enough to pull one KEY=value line without
    a python-dotenv dependency. Ignores comments/blank lines, strips
    matching quotes."""
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            return v
    return None


def http_call(base_url: str, method: str, path: str, token: Optional[str] = None,
              body: Optional[dict] = None, timeout: float = 15.0) -> tuple[int, dict]:
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {"error": "unparseable_error_body"}
        return e.code, payload
    except Exception as e:
        return -1, {"error": f"{type(e).__name__}: {e}"}


def random_order(rng: random.Random, i: int) -> dict:
    kind_roll = rng.random()

    if kind_roll < 0.05:
        # malformed: missing fields
        payload = {"order_id": f"httpfz-{i}", "agent_id": AGENT}
    elif kind_roll < 0.10:
        # malformed: wrong types
        payload = {
            "order_id": f"httpfz-{i}",
            "agent_id": AGENT,
            "instrument": "BANANA",
            "side": rng.choice(["bid", "ask"]),
            "qty": rng.choice([-5, 0, "ten", 3.5]),
            "limit_price": rng.choice([-1, 0, "x"]),
        }
    elif kind_roll < 0.15:
        # unsupported / adversarial instrument
        payload = {
            "order_id": f"httpfz-{i}",
            "agent_id": AGENT,
            "instrument": rng.choice(["FRAG", "DOGE", "", None]),
            "side": rng.choice(["bid", "ask"]),
            "qty": rng.randint(1, 50),
            "limit_price": rng.randint(1, 50),
        }
    elif kind_roll < 0.20:
        # deliberately huge order — probes solvency/committed-funds math
        payload = {
            "order_id": f"httpfz-{i}",
            "agent_id": AGENT,
            "instrument": "BANANA",
            "side": rng.choice(["bid", "ask"]),
            "qty": rng.randint(10_000, 1_000_000),
            "limit_price": rng.randint(10_000, 1_000_000),
        }
    else:
        # well-formed order, sometimes deliberately re-using a recent
        # order_id (duplicate/conflicting-dedup path) or crossing our own
        # resting side (self-cross path — only one identity available
        # over this token, so this is the closest self-cross probe)
        reuse = rng.random() < 0.1 and i > 0
        payload = {
            "order_id": f"httpfz-{i - rng.randint(1, min(i, 5))}" if reuse else f"httpfz-{i}",
            "agent_id": AGENT,
            "instrument": "BANANA",
            "side": rng.choice(["bid", "ask"]),
            "qty": rng.randint(1, 200),
            "limit_price": rng.randint(1, 30),
            "seq_seen": rng.choice([0, 1, 999999]),
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


def check_book_not_crossed(book: dict) -> Optional[str]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if bids and asks:
        try:
            best_bid = bids[0]["limit_price"]
            best_ask = asks[0]["limit_price"]
        except Exception:
            return None
        if isinstance(best_bid, (int, float)) and isinstance(best_ask, (int, float)):
            if best_bid >= best_ask:
                return f"crossed resting book: best_bid={best_bid} >= best_ask={best_ask}"
    return None


def run(base_url: str, token: str, seed: int, n: int, sleep_s: float) -> tuple[int, list[str]]:
    rng = random.Random(seed)
    failures: list[str] = []
    last_seq = -1

    for i in range(n):
        envelope = random_order(rng, i)
        status, resp = http_call(base_url, "POST", "/referee/orders", token=token, body=envelope)

        if status == -1:
            failures.append(f"step {i}: transport error calling /referee/orders: {resp} | envelope={envelope}")
            continue
        if status not in (200, 400):
            # 401/403/404/5xx are all noteworthy — a well-behaved referee
            # should only ever answer malformed/adversarial input with a
            # clean 400 reject, never a 5xx or an auth failure on our own
            # legitimate token.
            failures.append(f"step {i}: unexpected HTTP {status} for order submission: {resp} | envelope={envelope}")
        if not isinstance(resp, dict) or "v" not in resp or "kind" not in resp:
            failures.append(f"step {i}: malformed response envelope: {resp!r}")

        h_status, health = http_call(base_url, "GET", "/referee/health")
        if h_status != 200 and h_status != 500:
            failures.append(f"step {i}: /referee/health returned unexpected HTTP {h_status}: {health}")
        else:
            seq = health.get("seq", last_seq)
            if isinstance(seq, (int, float)) and seq < last_seq:
                failures.append(f"step {i}: seq went backwards ({last_seq} -> {seq})")
            last_seq = seq if isinstance(seq, (int, float)) else last_seq
            if not health.get("invariants_valid", True):
                for e in health.get("errors", []):
                    failures.append(f"step {i}: referee-reported invariant violation: {e}")

        b_status, book_resp = http_call(base_url, "GET", "/referee/book")
        if b_status == 200:
            crossed = check_book_not_crossed(book_resp.get("book", {}))
            if crossed:
                failures.append(f"step {i}: {crossed}")
        else:
            failures.append(f"step {i}: /referee/book returned unexpected HTTP {b_status}: {book_resp}")

        if sleep_s:
            time.sleep(sleep_s)

    return n, failures


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    base_url = os.environ.get("AGORA_BASE_URL", DEFAULT_BASE_URL)
    sleep_s = float(os.environ.get("AGORA_FUZZ_SLEEP", "0.05"))

    token = os.environ.get("AGORA_TOKEN_MARVIN") or load_token_from_dotenv(
        Path("/opt/karakos/config/.env"), "AGORA_TOKEN_MARVIN"
    )
    if not token:
        print("FATAL: no AGORA_TOKEN_MARVIN found in env or /opt/karakos/config/.env")
        sys.exit(2)

    print(f"Target: {base_url}  agent={AGENT}  n={n}  seed={seed}")
    count, failures = run(base_url, token, seed, n, sleep_s)

    print()
    if failures:
        print(f"FUZZ FAILED: {len(failures)} violation(s) across {count} live HTTP orders")
        for f in failures[:40]:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print(f"FUZZ PASSED: {count} live HTTP orders/steps against {base_url}, "
              f"0 invariant violations (conservation, non-negativity, reconciliation, "
              f"seq-monotonicity, uncrossed-book, well-formed responses, clean HTTP status)")
        sys.exit(0)
