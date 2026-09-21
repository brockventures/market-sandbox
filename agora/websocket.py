"""
agora.websocket - Pure standard library RFC 6455 WebSocket server and live diff engine.

Implements /ws/terminal differential streaming frames for Terminal Web HUD:
- snapshot: complete state on initial connect or resubscribe
- ticks: incremental trade executions and book event ticks
- depth_diff: order book bids/asks depth and spread deltas
- leaderboard: mark-to-market valuations and net worth ranking updates
- circuit_state: dynamic LULD volatility bands and station halt alerts
"""

import base64
import hashlib
import json
import select
import struct
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple

from agora.galnet import GalNetEngine
from agora.referee import AgoraReferee
from agora.spatial import get_alignment_windows


WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_accept_token(key: str) -> str:
    """Compute RFC 6455 Sec-WebSocket-Accept token from client Sec-WebSocket-Key."""
    concat = key.strip() + WS_MAGIC_GUID
    sha = hashlib.sha1(concat.encode("utf-8")).digest()
    return base64.b64encode(sha).decode("utf-8")


def encode_ws_frame(payload: bytes | str, opcode: int = 0x1) -> bytes:
    """
    Encode an unmasked server-to-client WebSocket frame (RFC 6455 section 5.1).
    Opcode 0x1 = text, 0x2 = binary, 0x8 = close, 0x9 = ping, 0xA = pong.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    length = len(payload)
    header = bytearray()
    # FIN = 1, RSV = 0, opcode
    header.append(0x80 | (opcode & 0x0F))
    # Unmasked (mask bit = 0)
    if length <= 125:
        header.append(length)
    elif length <= 65535:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def decode_ws_frame(sock) -> Tuple[Optional[int], Optional[bytes]]:
    """
    Read and decode a single masked client-to-server WebSocket frame from socket.
    Returns (opcode, payload_bytes) or (None, None) on socket close/EOF.
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


class TerminalDiffEngine:
    """
    In-memory state and diff engine for the Agora Terminal Web HUD.
    Calculates differential payload frames (depth_diff, ticks, leaderboard, circuit_state)
    against cached sequence numbers and entity state hashes.
    """

    def __init__(self, referee: AgoraReferee, galnet: Optional[GalNetEngine] = None):
        self.referee = referee
        self.galnet = galnet
        self.last_seq: int = 0
        self.last_leaderboard_hash: str = ""
        self.last_book_hash: Dict[Tuple[str, str], str] = {}
        self.last_circuit_hash: Dict[Tuple[str, str], str] = {}
        self.last_equity_hash: str = ""

    def get_snapshot(self, station_id: str = "ceres", instrument: str = "FRAG") -> Dict[str, Any]:
        """Generate a full initial snapshot frame covering all terminal panels."""
        st = station_id.lower()
        inst = instrument.upper()
        book_snap = self.referee.get_book_snapshot(st, inst)
        leaderboard = self.referee.get_leaderboard()
        bands = self.referee.get_circuit_breaker_bands(st, inst)
        halts = self.referee.get_circuit_breaker_halts()
        ticks = self.referee.get_ticks()[-25:]
        locations = self.referee.get_all_vessel_locations()
        windows = self.referee.get_orbital_windows()
        equity = self.referee.get_equity_summary() if hasattr(self.referee, "get_equity_summary") else {}
        loans = self.referee.get_equity_loans() if hasattr(self.referee, "get_equity_loans") else []

        self.last_seq = self.referee.current_seq
        self.last_leaderboard_hash = hashlib.md5(json.dumps(leaderboard, sort_keys=True).encode("utf-8")).hexdigest()
        self.last_book_hash[(st, inst)] = hashlib.md5(json.dumps(book_snap, sort_keys=True).encode("utf-8")).hexdigest()
        self.last_circuit_hash[(st, inst)] = hashlib.md5(
            json.dumps({"bands": bands, "halts": halts}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.last_equity_hash = hashlib.md5(json.dumps(equity, sort_keys=True).encode("utf-8")).hexdigest()

        return {
            "type": "snapshot",
            "seq": self.last_seq,
            "round": self.referee.current_round,
            "floor": self.referee.floor,
            "station_id": st,
            "instrument": inst,
            "leaderboard": leaderboard,
            "book": book_snap,
            "last_price": self.referee.get_last_price(st, inst),
            "last_qty": self.referee.get_last_qty(st, inst),
            "circuit": {
                "bands": bands,
                "halts": halts
            },
            "ticks": ticks,
            "locations": locations,
            "windows": windows,
            "equity": equity,
            "loans": loans
        }

    def get_diffs(self, station_id: str = "ceres", instrument: str = "FRAG") -> List[Dict[str, Any]]:
        """Calculate differential streaming frames since last invocation."""
        diffs = []
        st = station_id.lower()
        inst = instrument.upper()

        curr_seq = self.referee.current_seq

        # 1. Incremental trade ticks / book events
        if curr_seq > self.last_seq:
            new_ticks = self.referee.get_ticks(since_seq=self.last_seq)
            if new_ticks:
                diffs.append({
                    "type": "ticks",
                    "seq": curr_seq,
                    "ticks": new_ticks
                })
            self.last_seq = curr_seq

        # 2. Order book depth diff
        book_snap = self.referee.get_book_snapshot(st, inst)
        book_hash = hashlib.md5(json.dumps(book_snap, sort_keys=True).encode("utf-8")).hexdigest()
        if book_hash != self.last_book_hash.get((st, inst)):
            self.last_book_hash[(st, inst)] = book_hash
            diffs.append({
                "type": "depth_diff",
                "seq": curr_seq,
                "station_id": st,
                "instrument": inst,
                "book": book_snap,
                "last_price": self.referee.get_last_price(st, inst),
                "last_qty": self.referee.get_last_qty(st, inst)
            })

        # 3. Mark-to-market leaderboard diff
        leaderboard = self.referee.get_leaderboard()
        lb_hash = hashlib.md5(json.dumps(leaderboard, sort_keys=True).encode("utf-8")).hexdigest()
        if lb_hash != self.last_leaderboard_hash:
            self.last_leaderboard_hash = lb_hash
            diffs.append({
                "type": "leaderboard",
                "seq": curr_seq,
                "leaderboard": leaderboard
            })

        # 4. Circuit breaker state diff
        bands = self.referee.get_circuit_breaker_bands(st, inst)
        halts = self.referee.get_circuit_breaker_halts()
        circ_hash = hashlib.md5(
            json.dumps({"bands": bands, "halts": halts}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if circ_hash != self.last_circuit_hash.get((st, inst)):
            self.last_circuit_hash[(st, inst)] = circ_hash
            diffs.append({
                "type": "circuit_state",
                "seq": curr_seq,
                "station_id": st,
                "instrument": inst,
                "bands": bands,
                "halts": halts
            })

        # 5. Syndicate equity & bilateral loans diff
        if hasattr(self.referee, "get_equity_summary"):
            equity = self.referee.get_equity_summary()
            eq_hash = hashlib.md5(json.dumps(equity, sort_keys=True).encode("utf-8")).hexdigest()
            if eq_hash != self.last_equity_hash:
                self.last_equity_hash = eq_hash
                loans = self.referee.get_equity_loans() if hasattr(self.referee, "get_equity_loans") else []
                diffs.append({
                    "type": "equity",
                    "seq": curr_seq,
                    "equity": equity,
                    "loans": loans
                })

        return diffs


def handle_terminal_websocket(
    handler: BaseHTTPRequestHandler,
    referee: AgoraReferee,
    galnet: Optional[GalNetEngine] = None
):
    """
    Perform RFC 6455 HTTP 101 Switching Protocols handshake on /ws/terminal,
    dispatch initial full snapshot, and run differential stream loop.
    """
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        handler.send_error(400, "Missing Sec-WebSocket-Key header")
        return

    accept_token = compute_accept_token(key)
    response_headers = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_token}\r\n"
        "\r\n"
    )
    handler.wfile.write(response_headers.encode("utf-8"))
    handler.wfile.flush()

    sock = handler.connection
    sock.setblocking(False)

    diff_engine = TerminalDiffEngine(referee, galnet)
    station_id = "ceres"
    instrument = "FRAG"

    # Send initial full snapshot frame
    snapshot = diff_engine.get_snapshot(station_id, instrument)
    try:
        sock.sendall(encode_ws_frame(json.dumps(snapshot), opcode=0x1))
    except Exception:
        return

    last_ping = time.time()
    try:
        while True:
            # Check for incoming client messages (subscribe, ping, close) with 0.25s timeout
            rlist, _, _ = select.select([sock], [], [], 0.25)
            if rlist:
                opcode, payload = decode_ws_frame(sock)
                if opcode is None or opcode == 0x8:
                    # Connection closed or close frame
                    try:
                        sock.sendall(encode_ws_frame(b"", opcode=0x8))
                    except Exception:
                        pass
                    break
                elif opcode == 0x9:
                    # Ping -> respond with pong
                    sock.sendall(encode_ws_frame(payload or b"", opcode=0xA))
                elif opcode == 0x1 and payload:
                    # Client control frame (subscription/filter change)
                    try:
                        cmd = json.loads(payload.decode("utf-8"))
                        if "station_id" in cmd:
                            station_id = cmd["station_id"].lower()
                        if "instrument" in cmd:
                            instrument = cmd["instrument"].upper()
                        if cmd.get("action") == "snapshot":
                            snap = diff_engine.get_snapshot(station_id, instrument)
                            sock.sendall(encode_ws_frame(json.dumps(snap), opcode=0x1))
                    except Exception:
                        pass

            # Stream deltas
            diffs = diff_engine.get_diffs(station_id, instrument)
            for diff in diffs:
                sock.sendall(encode_ws_frame(json.dumps(diff), opcode=0x1))

            # Keep-alive heartbeat ping every 25s
            now = time.time()
            if now - last_ping >= 25.0:
                sock.sendall(encode_ws_frame(b"ping", opcode=0x9))
                last_ping = now

    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
