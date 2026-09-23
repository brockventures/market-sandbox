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
from typing import Optional, Dict, Any

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

BASE_URL = os.environ.get("AGORA_BASE_URL", "https://agora.mikecarmody.net").rstrip("/")
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

def get_ticker_status():
    return request("/referee/ticker/status")

def get_accounts():
    return request("/referee/accounts")

def get_book():
    return request("/referee/book")

def get_galnet_feed(limit: int = 15):
    return request(f"/galnet/feed?limit={limit}")

def get_galnet_drift(station_id: str = "ceres", commodity: str = "FUEL"):
    return request(f"/galnet/drift?station_id={station_id}&commodity={commodity}")

def get_stations_prices(station_id: str = None, commodity: str = None):
    q = []
    if station_id:
        q.append(f"station_id={station_id}")
    if commodity:
        q.append(f"commodity={commodity}")
    suffix = f"?{'&'.join(q)}" if q else ""
    return request(f"/stations/prices{suffix}")

def get_stations_routes(origin: str = None, destination: str = None):
    q = []
    if origin:
        q.append(f"origin={origin}")
    if destination:
        q.append(f"destination={destination}")
    suffix = f"?{'&'.join(q)}" if q else ""
    return request(f"/stations/routes{suffix}")

def get_stations_locations(agent_id: str = None):
    suffix = f"?agent_id={agent_id}" if agent_id else ""
    return request(f"/stations/locations{suffix}")

def post_transit(destination: str, commodity: str = "FRAG", cargo_qty: int = 0):
    return request("/stations/transit", {
        "agent_id": AGENT_ID,
        "destination": destination,
        "commodity": commodity,
        "cargo_qty": cargo_qty
    })

def get_equity_summary():
    return request("/equity/summary")

def get_equity_loans(borrower_id: str = None, lender_id: str = None):
    q = []
    if borrower_id:
        q.append(f"borrower_id={borrower_id}")
    if lender_id:
        q.append(f"lender_id={lender_id}")
    suffix = f"?{'&'.join(q)}" if q else ""
    return request(f"/equity/loans{suffix}")

def borrow_equity(equity_symbol: str, shares: int, lender_id: str = None):
    payload = {
        "agent_id": AGENT_ID,
        "equity_symbol": equity_symbol,
        "shares": shares
    }
    if lender_id:
        payload["lender_id"] = lender_id
    return request("/equity/borrow", payload)

def return_equity_loan(loan_id: str):
    return request("/equity/return", {
        "agent_id": AGENT_ID,
        "loan_id": loan_id
    })

def get_salvage_summary():
    return request("/salvage/summary")

def get_salvage_beacons(status: str = "active"):
    suffix = f"?status={status}" if status else ""
    return request(f"/salvage/beacons{suffix}")

def get_salvage_rfqs(status: str = "open"):
    suffix = f"?status={status}" if status else ""
    return request(f"/salvage/rfqs{suffix}")

def claim_salvage(beacon_id: str):
    return request("/salvage/claim", {
        "agent_id": AGENT_ID,
        "beacon_id": beacon_id
    })

def broadcast_distress(reason: str = "out_of_propellant", cargo_bounty: dict = None):
    return request("/salvage/distress", {
        "agent_id": AGENT_ID,
        "reason": reason,
        "cargo_bounty": cargo_bounty or {}
    })

def cancel_all():
    return request("/referee/orders/cancel_all", {"agent_id": AGENT_ID})

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

def run_loop(duration: int = 900, interval: float = 10.0, instrument: str = "FRAG"):
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
            
            # Prune stale resting orders every 5 cycles to keep free margin liquid
            if cycle % 5 == 0:
                cancel_res = cancel_all()
                cancelled_cnt = cancel_res.get("payload", {}).get("count", 0)
                if cancelled_cnt > 0:
                    print(f"[{elapsed}s] Pruned {cancelled_cnt} stale resting orders")
            
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

    # Clean up all resting orders on loop exit
    cancel_all()
    print(f"Trader loop finished after {cycle} cycles.")
    print("Final account status:")
    print(json.dumps(get_accounts(), indent=2))

def load_strategy_config() -> dict:
    """Load local hot-reload trading configuration merged with defaults."""
    cfg = {
        "station_id": "ceres",
        "instrument": "FRAG",
        "target_bid": 14,
        "target_ask": 16,
        "clip_size": 5,
        "min_liquid_cr": 5000,
        "prune_interval_cycles": 5,
        "enable_spatial_transit": True,
        "min_transit_net_cr": 50.0,
        "transit_cargo_clip": 50
    }
    paths = [
        Path("/workspace/data/strategy_config.json"),
        Path(__file__).resolve().parent.parent / "data" / "strategy_config.json",
        Path("strategy_config.json")
    ]
    for p in paths:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        cfg.update(data)
                        return cfg
            except Exception:
                pass
    return cfg

def poll_round_loop(
    poll_interval: float = 2.0,
    max_rounds: Optional[int] = None,
    max_idle_sec: int = 600,
    instrument: str = "FRAG",
    station_id: str = "ceres",
    dry_run: bool = False
):
    """
    Autonomous round-sync and polling mode for agent runloop (Issue #63).
    Monitors referee round progression via /referee/ticker/status or /referee/health,
    evaluates order books upon round transition, and places tactical bids/asks.
    Dynamically throttles when the market is quiet or paused.
    """
    print(f"Starting autonomous round-sync runloop: agent={AGENT_ID}, station={station_id}, poll_interval={poll_interval}s")
    last_round: Optional[int] = None
    last_seq: Optional[int] = None
    rounds_completed = 0
    idle_start = time.time()

    try:
        while True:
            health = check_health()
            if not isinstance(health, dict) or health.get("status") != "ok":
                print(f"⚠️ Referee health unavailable: {health}")
                time.sleep(poll_interval * 2)
                continue

            current_seq = health.get("seq", 0)
            floor = health.get("floor", "open")

            ticker = get_ticker_status()
            is_ticker_active = isinstance(ticker, dict) and ticker.get("status") == "ok"
            current_round = ticker.get("current_round") if is_ticker_active else None
            is_paused = ticker.get("paused", False) if is_ticker_active else False

            # If ticker status doesn't report round, synthesize pseudo-round from health seq
            if current_round is None:
                current_round = health.get("round", current_seq // 10 if current_seq else 1)

            # Check if round advanced or initial turn
            round_changed = (last_round is None) or (current_round != last_round)
            seq_changed = (last_seq is None) or (current_seq != last_seq)

            if round_changed:
                rounds_completed += 1
                idle_start = time.time()
                print(f"\n🔔 [Round Advance] Round {last_round} -> {current_round} (seq={current_seq}, floor={floor})")

                if floor != "open":
                    print(f"⏸️ Floor is {floor.upper()} — standing down from order generation.")
                else:
                    cfg = load_strategy_config()
                    clip_size = cfg.get("clip_size", 5)
                    min_liquid = cfg.get("min_liquid_cr", 5000)

                    # Account balance parsing
                    accs = get_accounts()
                    balances = {}
                    acc_list = accs.get("accounts", []) if isinstance(accs, dict) else (accs if isinstance(accs, list) else [])
                    for a in acc_list:
                        if a.get("agent_id") == AGENT_ID:
                            inst = a.get("instrument")
                            if inst:
                                balances[inst] = a.get("balance", 0)
                            if "liquid" in a:
                                balances["CR"] = a.get("liquid", 0)
                            if "frags" in a:
                                balances["FRAG"] = a.get("frags", 0)
                            if "fuel" in a:
                                balances["FUEL"] = a.get("fuel", 0)
                    liquid_cr = balances.get("CR", 10000)
                    available_frag = balances.get("FRAG", 0)
                    available_fuel = balances.get("FUEL", 0)

                    # Fleet location check
                    locs_resp = get_stations_locations(AGENT_ID)
                    loc_list = locs_resp.get("locations", []) if isinstance(locs_resp, dict) else []
                    my_loc = loc_list[0] if loc_list else {}
                    is_docked = (my_loc.get("status", "docked") == "docked") if my_loc else True
                    current_station = my_loc.get("station_id", station_id) if is_docked else "in_transit"

                    # Autonomous Spatial Transit Dispatch across all commodities
                    trade_commodities = ["FRAG", "FOOD", "ORE"]
                    if is_docked and cfg.get("enable_spatial_transit", True) and available_fuel >= 20:
                        prices_resp = get_stations_prices()
                        prices_data = prices_resp.get("data", {}).get("prices", {}) if isinstance(prices_resp, dict) else {}

                        routes_resp = get_stations_routes(origin=current_station)
                        routes_list = routes_resp.get("routes", []) if isinstance(routes_resp, dict) else []

                        best_dest = None
                        best_comm = None
                        max_net_margin = 0.0
                        best_req_fuel = 0
                        best_cargo_qty = 0

                        for comm in trade_commodities:
                            avail_qty = balances.get(comm, 0)
                            if avail_qty < 10:
                                continue
                            cur_p = prices_data.get(current_station, {}).get(comm, 0.0)

                            for r in routes_list:
                                dest = r.get("destination")
                                if dest == current_station:
                                    continue
                                dest_p = prices_data.get(dest, {}).get(comm, 0.0)
                                spread = dest_p - cur_p
                                fuel_req = r.get("fuel", 999)
                                toll_req = r.get("toll", 0)

                                transit_qty = min(cfg.get("transit_cargo_clip", 50), avail_qty)
                                gross_profit = spread * transit_qty
                                fuel_cost = fuel_req * 10.0
                                net_profit = gross_profit - toll_req - fuel_cost

                                if (net_profit >= cfg.get("min_transit_net_cr", 50.0)
                                    and net_profit > max_net_margin
                                    and available_fuel >= fuel_req
                                    and liquid_cr >= toll_req):
                                    max_net_margin = net_profit
                                    best_dest = dest
                                    best_comm = comm
                                    best_req_fuel = fuel_req
                                    best_cargo_qty = transit_qty

                        if best_dest and best_comm and not dry_run:
                            tx_res = post_transit(destination=best_dest, commodity=best_comm, cargo_qty=best_cargo_qty)
                            print(f"🚀 [Spatial Transit Dispatched] {current_station.upper()} -> {best_dest.upper()} ({best_cargo_qty} {best_comm}, fuel={best_req_fuel}, proj net={max_net_margin:.1f} CR): {tx_res.get('status')}")

                    # Book inspection & quoting (only when docked at a station)
                    if is_docked:
                        book = get_book().get("book", {})
                        bids = book.get("bids", [])
                        asks = book.get("asks", [])
                        best_bid = bids[0]["limit_price"] if bids else cfg.get("target_bid", 14)
                        best_ask = asks[0]["limit_price"] if asks else cfg.get("target_ask", 16)

                        # Prune stale resting orders
                        cancel_res = cancel_all()
                        pruned = cancel_res.get("payload", {}).get("count", 0) if isinstance(cancel_res, dict) else 0
                        if pruned > 0:
                            print(f"🧹 Pruned {pruned} stale resting orders")

                        # Calculate tactical quote bounds
                        if liquid_cr > min_liquid:
                            bid_p = max(1, best_bid)
                            ask_p = max(bid_p + 1, best_ask)
                            if not dry_run:
                                res_bid = submit_order("bid", qty=clip_size, limit_price=bid_p, instrument=instrument)
                                res_ask = submit_order("ask", qty=clip_size, limit_price=ask_p, instrument=instrument)
                                print(f"⚡ Round {current_round} Quotes Placed ({current_station.upper()}): BID {clip_size} @ {bid_p} CR | ASK {clip_size} @ {ask_p} CR")
                            else:
                                print(f"[DRY-RUN] Would submit: BID {clip_size} @ {bid_p} CR | ASK {clip_size} @ {ask_p} CR")
                        else:
                            print(f"🛡️ Liquid credits ({liquid_cr} CR) below threshold ({min_liquid} CR) — conserving margin.")
                    else:
                        print(f"🛰️ Fleet is in transit ({my_loc.get('transit', {}).get('origin', '?').upper()} -> {my_loc.get('transit', {}).get('destination', '?').upper()}, arrival round {my_loc.get('transit', {}).get('arrival_round', '?')}) — local order quoting paused.")

                last_round = current_round
                last_seq = current_seq

                if max_rounds and rounds_completed >= max_rounds:
                    print(f"🏁 Max rounds reached ({rounds_completed}/{max_rounds}). Exiting runloop.")
                    break

            elif seq_changed:
                last_seq = current_seq
                idle_start = time.time()

            # Dynamic backoff if ticker is paused or floor halted
            if is_paused or floor != "open":
                sleep_duration = poll_interval * 3
            else:
                sleep_duration = poll_interval

            if time.time() - idle_start > max_idle_sec:
                print(f"⏳ Inactivity timeout ({max_idle_sec}s without activity). Exiting runloop.")
                break

            time.sleep(sleep_duration)

    except KeyboardInterrupt:
        print("\n🛑 Runloop interrupted by operator.")
    finally:
        print("Cleaning up resting orders...")
        cancel_all()
        print("Autonomous runloop terminated cleanly.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Zero Agora Trader Client")
    parser.add_argument("--probe", action="store_true", help="Run a health and balance probe")
    parser.add_argument("--ticker-status", action="store_true", help="Probe /referee/ticker/status endpoint")
    parser.add_argument("--galnet", action="store_true", help="Probe GalNet feed and active station drifts")
    parser.add_argument("--order", choices=["bid", "ask"], help="Place a single test limit order")
    parser.add_argument("--qty", type=int, default=10, help="Order quantity")
    parser.add_argument("--price", type=int, default=10, help="Order limit price")
    parser.add_argument("--run", action="store_true", help="Run continuous trading loop for test run")
    parser.add_argument("--poll", "--autonomous", action="store_true", dest="poll", help="Run autonomous round-sync polling loop (Issue #63)")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Interval between health/ticker polls in seconds (default: 2.0)")
    parser.add_argument("--max-rounds", type=int, default=None, help="Maximum number of rounds to participate in before exiting")
    parser.add_argument("--duration", type=int, default=900, help="Run duration in seconds (default: 900 = 15m)")
    parser.add_argument("--interval", type=float, default=10.0, help="Interval between orders in seconds (default: 10.0)")
    parser.add_argument("--prices", action="store_true", help="Probe station spot prices across Sol nodes")
    parser.add_argument("--routes", action="store_true", help="Inspect Sol orbital transit route matrix")
    parser.add_argument("--locations", action="store_true", help="Inspect fleet vessel locations and dock statuses")
    parser.add_argument("--transit", type=str, help="Initiate orbital transit to destination station (e.g. mars)")
    parser.add_argument("--cargo", type=int, default=0, help="Cargo quantity to transport during transit")
    parser.add_argument("--equity-summary", action="store_true", help="Probe synthetic fleet equity prices, shares, and NAVs")
    parser.add_argument("--equity-loans", action="store_true", help="Inspect active bilateral stock borrow loans")
    parser.add_argument("--borrow", type=str, help="Borrow synthetic fleet equity shares (e.g. EQ_MARV)")
    parser.add_argument("--shares", type=int, default=10, help="Number of equity shares to borrow")
    parser.add_argument("--lender", type=str, default=None, help="Lender agent ID for equity loan")
    parser.add_argument("--return-loan", type=str, help="Return equity loan by loan_id")
    parser.add_argument("--salvage-summary", action="store_true", help="Inspect derelict salvage and distress RFQ statistics")
    parser.add_argument("--salvage-beacons", action="store_true", help="List active derelict distress beacons")
    parser.add_argument("--salvage-claim", type=str, help="Claim derelict vessel by beacon_id")
    args = parser.parse_args()

    if args.poll:
        poll_round_loop(poll_interval=args.poll_interval, max_rounds=args.max_rounds)
    elif args.run:
        run_loop(duration=args.duration, interval=args.interval)
    elif args.ticker_status:
        print("=== Agora Ticker Status ===")
        print(json.dumps(get_ticker_status(), indent=2))
    elif args.order:
        print(f"Placing {args.order} qty={args.qty} @ {args.price} CR...")
        res = submit_order(side=args.order, qty=args.qty, limit_price=args.price)
        print(json.dumps(res, indent=2))
    elif args.galnet:
        print("=== GalNet News Feed ===")
        print(json.dumps(get_galnet_feed(), indent=2))
        print("=== Sol Station Active Drifts ===")
        for st, comm in [("ceres", "FUEL"), ("mars", "FRAG"), ("luna", "FUEL"), ("earth", "FRAG")]:
            drift = get_galnet_drift(st, comm)
            print(f"{st.upper()} ({comm}): {drift.get('drift_bias', 0.0)}")
    elif args.prices:
        print("=== Sol Station Spot Prices ===")
        print(json.dumps(get_stations_prices(), indent=2))
    elif args.routes:
        print("=== Sol Orbital Routes ===")
        print(json.dumps(get_stations_routes(), indent=2))
    elif args.locations:
        print("=== Fleet Vessel Locations ===")
        print(json.dumps(get_stations_locations(), indent=2))
    elif args.transit:
        print(f"Initiating transit to {args.transit} with cargo_qty={args.cargo}...")
        res = post_transit(destination=args.transit, cargo_qty=args.cargo)
        print(json.dumps(res, indent=2))
    elif args.equity_summary:
        print("=== Fleet Synthetic Equities ===")
        print(json.dumps(get_equity_summary(), indent=2))
    elif args.equity_loans:
        print("=== Active Equity Loans ===")
        print(json.dumps(get_equity_loans(), indent=2))
    elif args.borrow:
        print(f"Borrowing {args.shares} shares of {args.borrow} from {args.lender or 'default pool'}...")
        res = borrow_equity(equity_symbol=args.borrow, shares=args.shares, lender_id=args.lender)
        print(json.dumps(res, indent=2))
    elif args.return_loan:
        print(f"Returning equity loan {args.return_loan}...")
        res = return_equity_loan(loan_id=args.return_loan)
        print(json.dumps(res, indent=2))
    elif args.salvage_summary:
        print("=== Salvage & Distress Summary ===")
        print(json.dumps(get_salvage_summary(), indent=2))
    elif args.salvage_beacons:
        print("=== Active Distress Beacons ===")
        print(json.dumps(get_salvage_beacons(), indent=2))
    elif args.salvage_claim:
        print(f"Claiming salvage on beacon {args.salvage_claim}...")
        res = claim_salvage(beacon_id=args.salvage_claim)
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

