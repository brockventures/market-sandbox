#!/usr/bin/env python3
"""
trader_client.py - Zero Trading Client for Agora Solvency Combine

Submits bids and asks to Station Agora (/referee/orders) using AGORA_TOKEN_ZERO.
Supports single-probe orders and continuous execution runs.
"""

import os
import sys
import time
import json
import uuid
import urllib.request
import urllib.error
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

def load_env():
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

load_env()

BASE_URL = os.environ.get("AGORA_BASE_URL", "https://agora-banana-production.up.railway.app").rstrip("/")
TOKEN = os.environ.get("AGORA_TOKEN_ZERO", "")
AGENT_ID = "zero"

def request(endpoint: str, data: dict = None) -> dict:
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "User-Agent": "ZeroTrader/1.0",
        "Content-Type": "application/json"
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return {"http_code": e.code, "error": json.loads(raw)}
        except Exception:
            return {"http_code": e.code, "raw_error": raw}
    except Exception as e:
        return {"error": str(e)}

def check_health():
    return request("/referee/health")

def get_accounts():
    return request("/referee/accounts")

def get_book():
    return request("/referee/book")

def submit_order(side: str, qty: int, limit_price: int, instrument: str = "FRAG") -> dict:
    seq_res = check_health()
    current_seq = seq_res.get("seq", 0) if isinstance(seq_res, dict) else 0
    order_id = f"ord-{AGENT_ID}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    envelope = {
        "v": 1,
        "kind": "order",
        "reply": "optional",
        "floor": "open",
        "scope": "channel",
        "subject": "agent-collaborative-project",
        "payload": {
            "order_id": order_id,
            "agent_id": AGENT_ID,
            "instrument": instrument,
            "side": side,
            "qty": qty,
            "limit_price": limit_price,
            "seq_seen": current_seq
        }
    }
    return request("/referee/orders", envelope)

def run_loop(duration: int = 1800, interval: float = 10.0, instrument: str = "FRAG"):
    """
    Run continuous trading loop for systems combine.
    Posts alternating bids/asks within conservative bounds to generate socket traffic,
    advance sequence numbers, and exercise matching/settlement.
    """
    print(f"Starting trader loop: duration={duration}s, interval={interval}s, agent={AGENT_ID}")
    start_time = time.time()
    end_time = start_time + duration
    cycle = 0

    while time.time() < end_time:
        cycle += 1
        elapsed = int(time.time() - start_time)
        remaining = int(end_time - time.time())
        
        try:
            health = check_health()
            if not health.get("invariants_valid", False):
                print(f"[{elapsed}s] ⚠️ Invariant failure reported by referee: {health.get('errors')}")
            
            book = get_book().get("book", {})
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            best_bid = bids[0]["limit_price"] if bids else 5
            best_ask = asks[0]["limit_price"] if asks else 25
            
            # Alternate between posting bids and asks around the active spread
            if cycle % 2 == 1:
                price = max(1, best_bid + 1 if best_bid < best_ask - 2 else best_bid)
                res = submit_order("bid", qty=5, limit_price=price, instrument=instrument)
                side_str = f"BID 5 @ {price}"
            else:
                price = max(best_bid + 1, best_ask - 1 if best_ask > best_bid + 2 else best_ask)
                res = submit_order("ask", qty=5, limit_price=price, instrument=instrument)
                side_str = f"ASK 5 @ {price}"
            
            status = res.get("kind", res.get("status", "unknown"))
            print(f"[{elapsed}s / rem {remaining}s] Cycle {cycle}: {side_str} -> {status} (seq={health.get('seq')})")
        except Exception as e:
            print(f"[{elapsed}s] Cycle {cycle} error: {e}")
            
        time.sleep(interval)

    print(f"Trader loop finished after {cycle} cycles.")
    print("Final account status:")
    print(json.dumps(get_accounts(), indent=2))

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Zero Agora Trader Client")
    parser.add_argument("--probe", action="store_true", help="Run a health and balance probe")
    parser.add_argument("--order", choices=["bid", "ask"], help="Place a single test limit order")
    parser.add_argument("--qty", type=int, default=10, help="Order quantity")
    parser.add_argument("--price", type=int, default=10, help="Order limit price")
    parser.add_argument("--run", action="store_true", help="Run continuous trading loop for test run")
    parser.add_argument("--duration", type=int, default=1800, help="Run duration in seconds (default: 1800 = 30m)")
    parser.add_argument("--interval", type=float, default=10.0, help="Interval between orders in seconds (default: 10.0)")
    args = parser.parse_args()

    if args.run:
        run_loop(duration=args.duration, interval=args.interval)
    elif args.order:
        print(f"Placing {args.order} qty={args.qty} @ {args.price} CR...")
        res = submit_order(side=args.order, qty=args.qty, limit_price=args.price)
        print(json.dumps(res, indent=2))
    else:
        print("=== Agora Health ===")
        print(json.dumps(check_health(), indent=2))
        print("=== Zero Account ===")
        accs = get_accounts()
        if isinstance(accs, list):
            zero_acc = [a for a in accs if a.get("agent_id") == AGENT_ID]
            print(json.dumps(zero_acc, indent=2))
        else:
            print(json.dumps(accs, indent=2))

