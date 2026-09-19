"""
tests/test_websocket.py - Integration and unit tests for Agora Terminal WebSocket streaming and live diff engine.
"""

import base64
import json
import os
import socket
import struct
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from agora.referee import AgoraReferee
from agora.server import make_handler
from agora.websocket import (
    WS_MAGIC_GUID,
    TerminalDiffEngine,
    compute_accept_token,
    decode_ws_frame,
    encode_ws_frame,
)


class TestAgoraWebSocket(unittest.TestCase):

    def test_rfc6455_accept_token(self):
        # RFC 6455 section 1.3 test vector
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        expected = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        self.assertEqual(compute_accept_token(key), expected)

    def test_frame_encoding_and_decoding(self):
        # Short frame (< 126)
        msg = "hello agora websocket"
        encoded = encode_ws_frame(msg, opcode=0x1)
        self.assertEqual(encoded[0], 0x81)  # FIN + text
        self.assertEqual(encoded[1], len(msg))
        self.assertEqual(encoded[2:].decode("utf-8"), msg)

        # Extended 16-bit frame (e.g. 500 bytes)
        big_msg = "X" * 500
        big_encoded = encode_ws_frame(big_msg, opcode=0x1)
        self.assertEqual(big_encoded[0], 0x81)
        self.assertEqual(big_encoded[1], 126)
        ext_len = struct.unpack("!H", big_encoded[2:4])[0]
        self.assertEqual(ext_len, 500)
        self.assertEqual(big_encoded[4:].decode("utf-8"), big_msg)

    def test_terminal_diff_engine_snapshot(self):
        referee = AgoraReferee()
        engine = TerminalDiffEngine(referee)

        snapshot = engine.get_snapshot("ceres", "FRAG")
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["station_id"], "ceres")
        self.assertEqual(snapshot["instrument"], "FRAG")
        self.assertIn("leaderboard", snapshot)
        self.assertIn("book", snapshot)
        self.assertIn("circuit", snapshot)
        self.assertIn("ticks", snapshot)
        self.assertIn("locations", snapshot)
        self.assertIn("windows", snapshot)
        self.assertEqual(len(snapshot["leaderboard"]), 4)

        # Diffs immediately after snapshot should be empty
        diffs = engine.get_diffs("ceres", "FRAG")
        self.assertEqual(len(diffs), 0)

    def test_terminal_diff_engine_diffs_on_order(self):
        referee = AgoraReferee()
        engine = TerminalDiffEngine(referee)
        _ = engine.get_snapshot("ceres", "FRAG")

        # Submit an order to generate tick and book diff
        order_env = {
            "v": 1,
            "kind": "order",
            "payload": {
                "order_id": "ws-ord-1",
                "agent_id": "zero",
                "instrument": "FRAG",
                "side": "bid",
                "qty": 20,
                "limit_price": 21,
                "station_id": "ceres",
                "seq_seen": 0,
            },
        }
        res = referee.submit_envelope(order_env)
        self.assertEqual(res["kind"], "market_tick")

        diffs = engine.get_diffs("ceres", "FRAG")
        self.assertGreaterEqual(len(diffs), 1)
        diff_types = [d["type"] for d in diffs]
        self.assertIn("ticks", diff_types)
        self.assertIn("depth_diff", diff_types)

        # Verify tick details
        tick_frame = [d for d in diffs if d["type"] == "ticks"][0]
        self.assertEqual(len(tick_frame["ticks"]), 1)
        self.assertEqual(tick_frame["ticks"][0]["payload"]["order_id"], "ws-ord-1")

        # Subsequent check without new events should yield 0 diffs
        diffs2 = engine.get_diffs("ceres", "FRAG")
        self.assertEqual(len(diffs2), 0)

    def test_websocket_live_server_stream(self):
        referee = AgoraReferee()
        handler_class = make_handler(referee)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        port = server.server_address[1]

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        time.sleep(0.1)

        try:
            # 1. Test regular GET request to /ws/terminal (returns 200 metadata)
            sock_http = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock_http.connect(("127.0.0.1", port))
            sock_http.sendall(b"GET /ws/terminal HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            http_resp = b""
            while True:
                data = sock_http.recv(4096)
                if not data:
                    break
                http_resp += data
            resp_str = http_resp.decode("utf-8")
            self.assertIn("200 OK", resp_str)
            self.assertIn("websocket", resp_str)
            sock_http.close()

            # 2. Test live RFC 6455 WebSocket connection
            sock_ws = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock_ws.connect(("127.0.0.1", port))

            client_key = "dGhlIHNhbXBsZSBub25jZQ=="
            handshake = (
                "GET /ws/terminal HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {client_key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock_ws.sendall(handshake.encode("utf-8"))

            # Read handshake response
            resp_header = b""
            while b"\r\n\r\n" not in resp_header:
                chunk = sock_ws.recv(1024)
                if not chunk:
                    break
                resp_header += chunk

            self.assertIn(b"HTTP/1.1 101 Switching Protocols", resp_header)
            self.assertIn(b"Upgrade: websocket", resp_header)
            expected_accept = compute_accept_token(client_key)
            self.assertIn(f"Sec-WebSocket-Accept: {expected_accept}".encode("utf-8"), resp_header)

            # 3. Read initial snapshot frame
            opcode, payload = decode_ws_frame(sock_ws)
            self.assertEqual(opcode, 0x1)  # text frame
            snapshot_data = json.loads(payload.decode("utf-8"))
            self.assertEqual(snapshot_data["type"], "snapshot")
            self.assertEqual(snapshot_data["station_id"], "ceres")

            # 4. Trigger an order to verify live streaming of delta frames
            referee.submit_envelope({
                "v": 1,
                "kind": "order",
                "payload": {
                    "order_id": "ws-live-order-1",
                    "agent_id": "zero",
                    "instrument": "FRAG",
                    "side": "bid",
                    "qty": 15,
                    "limit_price": 21,
                    "station_id": "ceres",
                    "seq_seen": 0,
                },
            })

            # Read diff frames streamed over socket
            # Socket will receive ticks and/or depth_diff
            sock_ws.settimeout(2.0)
            received_frames = []
            start_t = time.time()
            while time.time() - start_t < 1.5:
                try:
                    op, pl = decode_ws_frame(sock_ws)
                    if op == 0x1 and pl:
                        received_frames.append(json.loads(pl.decode("utf-8")))
                except socket.timeout:
                    break

            self.assertGreaterEqual(len(received_frames), 1)
            frame_types = [f["type"] for f in received_frames]
            self.assertTrue("ticks" in frame_types or "depth_diff" in frame_types)

            # 5. Send masked client close frame (RFC 6455 requires client frames to be masked)
            # 0x88 (FIN + close opcode), 0x80 (masked + 0 len) + 4-byte mask
            close_frame = bytes([0x88, 0x80, 0x12, 0x34, 0x56, 0x78])
            sock_ws.sendall(close_frame)
            sock_ws.close()

        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
