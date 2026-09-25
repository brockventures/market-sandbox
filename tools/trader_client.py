#!/usr/bin/env python3
"""
trader_client.py - Autonomous Multi-Agent Trading Client & WebSocket Runloop for Agora

Supports Zero, Amos, Aerial, and Marvin participating in server-ticked rounds
via HTTP polling or real-time WebSocket differential streaming (/ws/terminal)
without relying on Discord message-driven IPC (Issue #63).
"""

import os
import sys
import time
import json
import uuid
import base64
import socket
import select
import struct
import random
import threading
import urllib.request
import urllib.parse
import urllib.error
import ssl
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

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
AGENT_ID = os.environ.get("AGORA_AGENT_ID", "zero").lower().strip()
TOKEN = os.environ.get(f"AGORA_TOKEN_{AGENT_ID.upper()}", os.environ.get("AGORA_TOKEN", os.environ.get("AGORA_TOKEN_ZERO", "")))

KNOWN_AGENTS = ["zero", "amos", "aerial", "marvin"]
TRADE_COMMODITIES = ["FRAG", "FOOD", "ORE"]


def set_agent(agent: str, token: Optional[str] = None):
    """Dynamically set the active agent identity and bearer token."""
    global AGENT_ID, TOKEN
    AGENT_ID = (agent or "zero").lower().strip()
    if token is not None:
        TOKEN = token
    else:
        TOKEN = (
            os.environ.get(f"AGORA_TOKEN_{AGENT_ID.upper()}")
            or os.environ.get("AGORA_TOKEN")
            or os.environ.get("AGORA_TOKEN_ZERO", "")
        )


def request(endpoint: str, data: dict = None, agent_id: Optional[str] = None, token: Optional[str] = None) -> dict:
    url = f"{BASE_URL}{endpoint}"
    act_agent = (agent_id or AGENT_ID).lower().strip()
    act_token = token if token is not None else (
        os.environ.get(f"AGORA_TOKEN_{act_agent.upper()}") or TOKEN
    )
    headers = {
        "User-Agent": f"{act_agent.capitalize()}Trader/1.0",
        "Content-Type": "application/json"
    }
    if act_token:
        headers["Authorization"] = f"Bearer {act_token}"

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


def get_book(station_id: Optional[str] = None, instrument: Optional[str] = None):
    q = []
    if station_id:
        q.append(f"station_id={station_id}")
    if instrument:
        q.append(f"instrument={instrument}")
    suffix = f"?{'&'.join(q)}" if q else ""
    return request(f"/referee/book{suffix}")


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


def post_transit(destination: str, commodity: str = "FRAG", cargo_qty: int = 0, agent_id: Optional[str] = None):
    act_agent = (agent_id or AGENT_ID).lower().strip()
    return request("/stations/transit", {
        "agent_id": act_agent,
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


def borrow_equity(equity_symbol: str, shares: int, lender_id: str = None, agent_id: Optional[str] = None):
    act_agent = (agent_id or AGENT_ID).lower().strip()
    payload = {
        "agent_id": act_agent,
        "equity_symbol": equity_symbol,
        "shares": shares
    }
    if lender_id:
        payload["lender_id"] = lender_id
    return request("/equity/borrow", payload)


def return_equity_loan(loan_id: str, agent_id: Optional[str] = None):
    act_agent = (agent_id or AGENT_ID).lower().strip()
    return request("/equity/return", {
        "agent_id": act_agent,
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


def claim_salvage(beacon_id: str, agent_id: Optional[str] = None):
    act_agent = (agent_id or AGENT_ID).lower().strip()
    return request("/salvage/claim", {
        "agent_id": act_agent,
        "beacon_id": beacon_id
    })


def broadcast_distress(reason: str = "out_of_propellant", cargo_bounty: dict = None, agent_id: Optional[str] = None):
    act_agent = (agent_id or AGENT_ID).lower().strip()
    return request("/salvage/distress", {
        "agent_id": act_agent,
        "reason": reason,
        "cargo_bounty": cargo_bounty or {}
    })


def cancel_all(agent_id: Optional[str] = None):
    act_agent = (agent_id or AGENT_ID).lower().strip()
    return request("/referee/orders/cancel_all", {"agent_id": act_agent})


def submit_order(side: str, qty: int, limit_price: int, instrument: str = "FRAG", agent_id: Optional[str] = None, station_id: Optional[str] = None, vessel_id: Optional[str] = None) -> dict:
    act_agent = (agent_id or AGENT_ID).lower().strip()
    seq_res = check_health()
    current_seq = seq_res.get("seq", 0) if isinstance(seq_res, dict) else 0
    order_id = f"ord-{act_agent}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    payload = {
        "order_id": order_id,
        "agent_id": act_agent,
        "instrument": instrument,
        "side": side,
        "qty": qty,
        "limit_price": limit_price,
        "seq_seen": current_seq
    }
    if station_id:
        payload["station_id"] = station_id
    if vessel_id:
        payload["vessel_id"] = vessel_id
    envelope = {
        "v": 1,
        "kind": "order",
        "reply": "optional",
        "floor": "open",
        "scope": "channel",
        "subject": "agent-collaborative-project",
        "payload": payload
    }
    return request("/referee/orders", envelope)


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
        "transit_cargo_clip": 50,
        "participate_probability": 1.0,
        "sleep_probability": 0.0
    }
    paths = [
        Path("/workspace/data/strategy_config.json"),
        Path(__file__).resolve().parent.parent / "data" / "strategy_config.json",
        Path("strategy_config.json"),
        Path(__file__).resolve().parent / "strategy_config.json"
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


# -- RFC 6455 Pure-Standard-Library WebSocket Client --------------------------

WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_accept_token(key: str) -> str:
    import hashlib
    concat = key.strip() + WS_MAGIC_GUID
    sha = hashlib.sha1(concat.encode("utf-8")).digest()
    return base64.b64encode(sha).decode("utf-8")


def encode_client_ws_frame(payload: bytes | str, opcode: int = 0x1) -> bytes:
    """Encode a masked client-to-server WebSocket frame (RFC 6455 section 5.1)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    length = len(payload)
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    mask_key = os.urandom(4)
    if length <= 125:
        header.append(0x80 | length)
    elif length <= 65535:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask_key)
    masked = bytearray(payload)
    for i in range(len(masked)):
        masked[i] ^= mask_key[i % 4]
    return bytes(header) + bytes(masked)


def decode_ws_frame_client(sock) -> Tuple[Optional[int], Optional[bytes]]:
    """
    Read and decode a single server-to-client (unmasked) or client-to-server (masked) frame.
    RFC 6455 compliant for trader client runtime.
    """
    try:
        head = sock.recv(2)
        if not head or len(head) < 2:
            return None, None
        b1, b2 = head[0], head[1]
        opcode = b1 & 0x0F
        is_masked = bool(b2 & 0x80)
        payload_len = b2 & 0x7F

        if payload_len == 126:
            ext = sock.recv(2)
            if len(ext) < 2:
                return None, None
            payload_len = struct.unpack("!H", ext)[0]
        elif payload_len == 127:
            ext = sock.recv(8)
            if len(ext) < 8:
                return None, None
            payload_len = struct.unpack("!Q", ext)[0]

        mask_key = None
        if is_masked:
            mask_key = sock.recv(4)
            if len(mask_key) < 4:
                return None, None

        payload = bytearray()
        remaining = payload_len
        while remaining > 0:
            chunk = sock.recv(min(remaining, 65536))
            if not chunk:
                return None, None
            payload.extend(chunk)
            remaining -= len(chunk)

        if is_masked and mask_key:
            for i in range(len(payload)):
                payload[i] ^= mask_key[i % 4]

        return opcode, bytes(payload)
    except (BlockingIOError, InterruptedError):
        return None, None
    except Exception:
        return None, None


def open_terminal_websocket(base_url: str = BASE_URL, token: Optional[str] = None, timeout: float = 5.0) -> socket.socket:
    """Establish RFC 6455 WebSocket handshake with /ws/terminal."""
    parsed = urllib.parse.urlparse(base_url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme in ("https", "wss") else 80)
    use_ssl = scheme in ("https", "wss")

    raw_sock = socket.create_connection((host, port), timeout=timeout)
    if use_ssl:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(raw_sock, server_hostname=host)
    else:
        sock = raw_sock

    sec_key = base64.b64encode(os.urandom(16)).decode("utf-8")
    auth_tok = token if token is not None else TOKEN
    headers = [
        "GET /ws/terminal HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {sec_key}",
        "Sec-WebSocket-Version: 13",
        f"User-Agent: {AGENT_ID.capitalize()}Trader/1.0",
    ]
    if auth_tok:
        headers.append(f"Authorization: Bearer {auth_tok}")

    req_data = "\r\n".join(headers) + "\r\n\r\n"
    sock.sendall(req_data.encode("utf-8"))

    # Read handshake response
    resp = bytearray()
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(1024)
        if not chunk:
            sock.close()
            raise ConnectionError("Server closed connection during WebSocket handshake")
        resp.extend(chunk)

    head, _ = resp.split(b"\r\n\r\n", 1)
    status_line = head.split(b"\r\n")[0].decode("utf-8", errors="replace")
    if "101" not in status_line:
        sock.close()
        raise ConnectionError(f"WebSocket upgrade rejected: {status_line}")

    sock.setblocking(False)
    return sock


# -- Core Agent Turn Execution Logic ------------------------------------------

def execute_agent_turn(
    agent_id: str = AGENT_ID,
    current_round: Optional[int] = None,
    current_seq: int = 0,
    floor: str = "open",
    station_id: str = "ceres",
    instrument: str = "FRAG",
    cfg: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    trigger_reason: str = "round_advance"
) -> Dict[str, Any]:
    """
    Core modular trading turn evaluation.
    Evaluates order books, balances, and spatial transit arbitrage, submitting orders via REST.
    """
    act_agent = (agent_id or AGENT_ID).lower().strip()

    if floor != "open":
        print(f"⏸️ Floor is {floor.upper()} — standing down from order generation.")
        return {"status": "floor_halted", "agent_id": act_agent, "round": current_round}

    config = cfg or load_strategy_config()

    # Dynamic sleep / stochastic participation (Issue #63)
    sleep_prob = float(config.get("sleep_probability", 0.0))
    participate_prob = float(config.get("participate_probability", 1.0))
    if sleep_prob > 0.0 and random.random() < sleep_prob:
        print(f"💤 [Dynamic Sleep] {act_agent.upper()} sleeping this turn (prob={sleep_prob})")
        return {"status": "sleeping", "agent_id": act_agent, "round": current_round}
    if participate_prob < 1.0 and random.random() > participate_prob:
        print(f"⏭️ [Dynamic Skip] {act_agent.upper()} yielding this turn (participate_prob={participate_prob})")
        return {"status": "skipped", "agent_id": act_agent, "round": current_round}

    clip_size = config.get("clip_size", 5)
    min_liquid = config.get("min_liquid_cr", 5000)

    # Account balances
    accs = get_accounts()
    balances = {}
    acc_list = accs.get("accounts", []) if isinstance(accs, dict) else (accs if isinstance(accs, list) else [])
    for a in acc_list:
        if a.get("agent_id") == act_agent:
            inst = a.get("instrument")
            if inst:
                balances[inst] = a.get("balance", 0)
            if "liquid" in a:
                balances["CR"] = a.get("liquid", 0)
            if "frags" in a:
                balances["FRAG"] = a.get("frags", 0)
            if "fuel" in a:
                balances["FUEL"] = a.get("fuel", 0)
            if "food" in a:
                balances["FOOD"] = a.get("food", 0)
            if "ore" in a:
                balances["ORE"] = a.get("ore", 0)

    liquid_cr = balances.get("CR", 10000)
    available_fuel = balances.get("FUEL", 0)

    # Fleet location
    locs_resp = get_stations_locations(act_agent)
    loc_list = locs_resp.get("locations", []) if isinstance(locs_resp, dict) else []
    my_loc = loc_list[0] if loc_list else {}
    is_docked = (my_loc.get("status", "docked") == "docked") if my_loc else True
    current_station = my_loc.get("station_id", station_id) if is_docked else "in_transit"

    actions_taken = {
        "status": "active",
        "agent_id": act_agent,
        "round": current_round,
        "station_id": current_station,
        "is_docked": is_docked,
        "transit": None,
        "quotes": None
    }

    # Autonomous Spatial Transit Dispatch across all commodities (#75, #241: FRAG, FOOD, ORE)
    if is_docked and config.get("enable_spatial_transit", True) and available_fuel >= 20:
        prices_resp = get_stations_prices()
        prices_data = prices_resp.get("data", {}).get("prices", {}) if isinstance(prices_resp, dict) else {}

        routes_resp = get_stations_routes(origin=current_station)
        routes_list = routes_resp.get("routes", []) if isinstance(routes_resp, dict) else []

        best_dest = None
        best_comm = None
        max_net_margin = 0.0
        best_req_fuel = 0
        best_cargo_qty = 0

        for comm in TRADE_COMMODITIES:
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

                transit_qty = min(config.get("transit_cargo_clip", 50), avail_qty)
                gross_profit = spread * transit_qty
                fuel_cost = fuel_req * 10.0
                net_profit = gross_profit - toll_req - fuel_cost

                if (net_profit >= config.get("min_transit_net_cr", 50.0)
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
            actions_taken["transit"] = tx_res
        elif best_dest and best_comm and dry_run:
            print(f"[DRY-RUN] Would transit: {current_station.upper()} -> {best_dest.upper()} ({best_cargo_qty} {best_comm})")

    # Book inspection & quoting (only when docked at a station)
    if is_docked:
        book_resp = get_book()
        book = book_resp.get("book", {}) if isinstance(book_resp, dict) else {}
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = bids[0]["limit_price"] if bids else config.get("target_bid", 14)
        best_ask = asks[0]["limit_price"] if asks else config.get("target_ask", 16)

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
                actions_taken["quotes"] = {"bid": res_bid, "ask": res_ask, "bid_price": bid_p, "ask_price": ask_p}
            else:
                print(f"[DRY-RUN] Would submit: BID {clip_size} @ {bid_p} CR | ASK {clip_size} @ {ask_p} CR")
        else:
            print(f"🛡️ Liquid credits ({liquid_cr} CR) below threshold ({min_liquid} CR) — conserving margin.")
            actions_taken["quotes"] = {"status": "conserving_margin"}
    else:
        print(f"🛰️ Fleet is in transit ({my_loc.get('transit', {}).get('origin', '?').upper()} -> {my_loc.get('transit', {}).get('destination', '?').upper()}) — local order quoting paused.")
        actions_taken["quotes"] = {"status": "in_transit"}

    return actions_taken


def ws_round_loop(
    base_url: str = BASE_URL,
    max_rounds: Optional[int] = None,
    max_idle_sec: int = 600,
    instrument: str = "FRAG",
    station_id: str = "ceres",
    agent_id: Optional[str] = None,
    dry_run: bool = False
):
    """
    Autonomous WebSocket-driven runloop for agent traders (Issue #63).
    Listens to /ws/terminal differential stream, triggering immediate turn execution
    upon round advancement, burst initiation, or circuit halts.
    """
    act_agent = (agent_id or AGENT_ID).lower().strip()
    set_agent(act_agent)
    print(f"Starting autonomous WebSocket runloop: agent={act_agent}, station={station_id}, base_url={base_url}")

    last_round: Optional[int] = None
    last_seq: Optional[int] = None
    rounds_completed = 0
    idle_start = time.time()
    sock: Optional[socket.socket] = None
    reconnect_delay = 1.0

    try:
        while True:
            if sock is None:
                try:
                    sock = open_terminal_websocket(base_url=base_url)
                    reconnect_delay = 1.0
                    print(f"🔌 Connected to WebSocket stream at {base_url}/ws/terminal")
                except Exception as e:
                    print(f"⚠️ WebSocket connection failed ({e}). Retrying in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 1.5, 10.0)
                    continue

            # Poll socket for incoming frames with 0.5s timeout
            rlist, _, _ = select.select([sock], [], [], 0.5)
            if not rlist:
                if time.time() - idle_start > max_idle_sec:
                    print(f"⏳ Inactivity timeout ({max_idle_sec}s without activity). Exiting runloop.")
                    break
                continue

            opcode, payload = decode_ws_frame_client(sock)
            if opcode is None or opcode == 0x8:
                print("🔌 WebSocket disconnected by server. Reconnecting...")
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None
                time.sleep(1.0)
                continue
            elif opcode == 0x9:
                try:
                    sock.sendall(encode_client_ws_frame(payload or b"", opcode=0xA))
                except Exception:
                    pass
                continue
            elif opcode == 0x1 and payload:
                try:
                    frame = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue

                frame_type = frame.get("type")
                curr_seq = frame.get("seq", last_seq or 0)
                if curr_seq != last_seq:
                    last_seq = curr_seq
                    idle_start = time.time()

                if frame_type == "snapshot":
                    snap_round = frame.get("round", 1)
                    snap_floor = frame.get("floor", "open")
                    if last_round is None:
                        last_round = snap_round
                        rounds_completed += 1
                        print(f"🔔 [WS Initial Snapshot] Round {snap_round} (seq={curr_seq}, floor={snap_floor})")
                        execute_agent_turn(
                            agent_id=act_agent,
                            current_round=snap_round,
                            current_seq=curr_seq,
                            floor=snap_floor,
                            station_id=station_id,
                            instrument=instrument,
                            dry_run=dry_run,
                            trigger_reason="ws_snapshot"
                        )
                        if max_rounds and rounds_completed >= max_rounds:
                            print(f"🏁 Max rounds reached ({rounds_completed}/{max_rounds}). Exiting runloop.")
                            break

                elif frame_type == "round":
                    curr_round = frame.get("round", (last_round or 0) + 1)
                    curr_floor = frame.get("floor", "open")
                    if curr_round != last_round:
                        rounds_completed += 1
                        print(f"\n🔔 [WS Round Advance] Round {last_round} -> {curr_round} (floor={curr_floor})")
                        execute_agent_turn(
                            agent_id=act_agent,
                            current_round=curr_round,
                            current_seq=curr_seq,
                            floor=curr_floor,
                            station_id=station_id,
                            instrument=instrument,
                            dry_run=dry_run,
                            trigger_reason="ws_round_advance"
                        )
                        last_round = curr_round
                        if max_rounds and rounds_completed >= max_rounds:
                            print(f"🏁 Max rounds reached ({rounds_completed}/{max_rounds}). Exiting runloop.")
                            break

                elif frame_type == "ticks":
                    ticks = frame.get("ticks", [])
                    has_burst = any(t.get("kind") == "burst" for t in ticks)
                    if has_burst:
                        print("⚡ [WS Burst Event Detected] Executing burst order evaluation...")
                        execute_agent_turn(
                            agent_id=act_agent,
                            current_round=last_round or 1,
                            current_seq=curr_seq,
                            floor="open",
                            station_id=station_id,
                            instrument=instrument,
                            dry_run=dry_run,
                            trigger_reason="ws_burst_tick"
                        )

    except KeyboardInterrupt:
        print("\n🛑 WebSocket runloop interrupted by operator.")
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        print(f"Cleaning up resting orders for {act_agent}...")
        cancel_all()
        print("Autonomous WebSocket runloop terminated cleanly.")


def poll_round_loop(
    poll_interval: float = 2.0,
    max_rounds: Optional[int] = None,
    max_idle_sec: int = 600,
    instrument: str = "FRAG",
    station_id: str = "ceres",
    agent_id: Optional[str] = None,
    dry_run: bool = False
):
    """
    Autonomous round-sync and polling mode for agent runloop (Issue #63).
    Monitors referee round progression and burst triggers via /referee/ticker/status
    and /referee/health, evaluating order books upon transition.
    """
    act_agent = (agent_id or AGENT_ID).lower().strip()
    set_agent(act_agent)
    print(f"Starting autonomous round-sync runloop: agent={act_agent}, station={station_id}, poll_interval={poll_interval}s")
    last_round: Optional[int] = None
    last_seq: Optional[int] = None
    last_burst_active: bool = False
    last_burst_id: Optional[str] = None
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
            burst_active = bool(ticker.get("burst_active", False)) if is_ticker_active else False
            burst_id = ticker.get("burst_id") if is_ticker_active else None

            # Synthesize pseudo-round if ticker doesn't report it
            if current_round is None:
                current_round = health.get("round", current_seq // 10 if current_seq else 1)

            round_changed = (last_round is None) or (current_round != last_round)
            seq_changed = (last_seq is None) or (current_seq != last_seq)
            burst_started = (burst_active and not last_burst_active) or (burst_id and burst_id != last_burst_id)

            if round_changed or burst_started:
                trigger_reason = "burst_start" if burst_started else "round_advance"
                rounds_completed += 1
                idle_start = time.time()

                if burst_started:
                    print(f"\n⚡ [Burst Initiated] ID: {burst_id} (remaining: {ticker.get('rounds_remaining')})")
                else:
                    print(f"\n🔔 [Round Advance] Round {last_round} -> {current_round} (seq={current_seq}, floor={floor})")

                execute_agent_turn(
                    agent_id=act_agent,
                    current_round=current_round,
                    current_seq=current_seq,
                    floor=floor,
                    station_id=station_id,
                    instrument=instrument,
                    dry_run=dry_run,
                    trigger_reason=trigger_reason
                )

                last_round = current_round
                last_seq = current_seq
                last_burst_active = burst_active
                last_burst_id = burst_id

                if max_rounds and rounds_completed >= max_rounds:
                    print(f"🏁 Max rounds reached ({rounds_completed}/{max_rounds}). Exiting runloop.")
                    break

            elif seq_changed:
                last_seq = current_seq
                idle_start = time.time()

            # Dynamic polling interval:
            # - When burst is active, poll aggressively (0.5s)
            # - When paused or halted, back off
            if burst_active:
                sleep_duration = min(poll_interval, 0.5)
            elif is_paused or floor != "open":
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
        print(f"Cleaning up resting orders for {act_agent}...")
        cancel_all()
        print("Autonomous runloop terminated cleanly.")


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


def run_autoworkers(
    agents: List[str],
    mode: str = "poll",
    poll_interval: float = 2.0,
    max_rounds: Optional[int] = None,
    dry_run: bool = False
):
    """Run concurrent autoworkers across multiple agents (Issue #63)."""
    threads = []
    print(f"🚀 Launching Agora Autoworkers for {agents} (mode={mode.upper()})...")
    for ag in agents:
        ag_clean = ag.lower().strip()
        if mode == "ws":
            target = ws_round_loop
            kwargs = {
                "agent_id": ag_clean,
                "max_rounds": max_rounds,
                "dry_run": dry_run
            }
        else:
            target = poll_round_loop
            kwargs = {
                "agent_id": ag_clean,
                "poll_interval": poll_interval,
                "max_rounds": max_rounds,
                "dry_run": dry_run
            }
        t = threading.Thread(target=target, kwargs=kwargs, name=f"autoworker-{ag_clean}", daemon=True)
        threads.append(t)
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nStopping all autoworkers...")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agora Autonomous Trader Client & Runloop")
    parser.add_argument("--agent", choices=KNOWN_AGENTS, default=None, help="Agent identity (zero, amos, aerial, marvin)")
    parser.add_argument("--token", type=str, default=None, help="Bearer token override for referee authentication")
    parser.add_argument("--all-agents", action="store_true", help="Launch concurrent autoworkers for all agents (zero, amos, aerial, marvin)")
    parser.add_argument("--probe", action="store_true", help="Run a health and balance probe")
    parser.add_argument("--ticker-status", action="store_true", help="Probe /referee/ticker/status endpoint")
    parser.add_argument("--galnet", action="store_true", help="Probe GalNet feed and active station drifts")
    parser.add_argument("--order", choices=["bid", "ask"], help="Place a single test limit order")
    parser.add_argument("--qty", type=int, default=10, help="Order quantity")
    parser.add_argument("--price", type=int, default=10, help="Order limit price")
    parser.add_argument("--instrument", type=str, default="FRAG", help="Commodity instrument (FRAG, FUEL, FOOD, ORE)")
    parser.add_argument("--station", type=str, default="ceres", help="Station ID (ceres, mars, luna, earth)")
    parser.add_argument("--run", action="store_true", help="Run continuous trading loop for test run")
    parser.add_argument("--poll", "--autonomous", action="store_true", dest="poll", help="Run autonomous round-sync polling loop (Issue #63)")
    parser.add_argument("--ws", "--websocket", action="store_true", help="Run autonomous WebSocket listener loop (Issue #63)")
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
    parser.add_argument("--dry-run", action="store_true", help="Simulate quotes and transits without submitting orders")
    args = parser.parse_args()

    if args.agent or args.token:
        set_agent(args.agent or AGENT_ID, token=args.token)

    if args.all_agents:
        mode = "ws" if args.ws else "poll"
        run_autoworkers(KNOWN_AGENTS, mode=mode, poll_interval=args.poll_interval, max_rounds=args.max_rounds, dry_run=args.dry_run)
    elif args.ws:
        ws_round_loop(max_rounds=args.max_rounds, instrument=args.instrument, station_id=args.station, dry_run=args.dry_run)
    elif args.poll:
        poll_round_loop(poll_interval=args.poll_interval, max_rounds=args.max_rounds, instrument=args.instrument, station_id=args.station, dry_run=args.dry_run)
    elif args.run:
        run_loop(duration=args.duration, interval=args.interval, instrument=args.instrument)
    elif args.ticker_status:
        print("=== Agora Ticker Status ===")
        print(json.dumps(get_ticker_status(), indent=2))
    elif args.order:
        print(f"Placing {args.order} qty={args.qty} @ {args.price} CR on {args.instrument}...")
        res = submit_order(side=args.order, qty=args.qty, limit_price=args.price, instrument=args.instrument)
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
        res = post_transit(destination=args.transit, commodity=args.instrument, cargo_qty=args.cargo)
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
        print(f"=== {AGENT_ID.capitalize()} Account ===")
        accs = get_accounts()
        if isinstance(accs, list):
            my_acc = [a for a in accs if a.get("agent_id") == AGENT_ID]
            print(json.dumps(my_acc, indent=2))
        else:
            print(json.dumps(accs, indent=2))
