"""
agora.server - Standard library HTTP/REST server for AgoraReferee.
Implements Section 3 endpoints of docs/wire-spec.md:
- POST /referee/orders
- GET  /referee/book
- GET  /referee/ticks
- GET  /referee/accounts
- GET  /referee/health
"""

import json
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from agora.referee import AgoraReferee


class AgoraHTTPHandler(BaseHTTPRequestHandler):
    referee: Optional[AgoraReferee] = None

    def _send_json(self, status_code: int, data: dict):
        response_bytes = json.dumps(data, indent=2).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')

        if path != '/referee/orders':
            self._send_json(404, {'error': 'not_found', 'path': self.path})
            return

        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self._send_json(400, {
                'v': 1, 'kind': 'reject',
                'payload': {'reason': 'invalid_format', 'detail': 'Empty request body'}
            })
            return

        try:
            body = self.rfile.read(content_length)
            envelope = json.loads(body.decode('utf-8'))
        except Exception as e:
            self._send_json(400, {
                'v': 1, 'kind': 'reject',
                'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
            })
            return

        ref = self.referee or AgoraReferee()
        result = ref.submit_envelope(envelope)
        if result.get('kind') == 'reject':
            self._send_json(400, result)
        else:
            self._send_json(200, result)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')
        query_params = urllib.parse.parse_qs(parsed_url.query)
        ref = self.referee or AgoraReferee()

        if path == '/referee/health':
            valid, errors = ref.verify_ledger_invariants()
            status_code = 200 if valid else 500
            self._send_json(status_code, {
                'status': 'ok' if valid else 'error',
                'seq': ref.current_seq,
                'floor': 'open',
                'invariants_valid': valid,
                'errors': errors
            })
        elif path == '/referee/book':
            self._send_json(200, {
                'status': 'ok',
                'book': ref.get_book_snapshot()
            })
        elif path == '/referee/ticks':
            since_seq_raw = query_params.get('since_seq', ['0'])[0]
            try:
                since_seq = int(since_seq_raw)
            except ValueError:
                since_seq = 0
            self._send_json(200, {
                'status': 'ok',
                'current_seq': ref.current_seq,
                'ticks': ref.get_ticks(since_seq=since_seq)
            })
        elif path == '/referee/accounts':
            agent_id = query_params.get('agent_id', [None])[0]
            self._send_json(200, {
                'status': 'ok',
                'accounts': ref.get_accounts(agent_id=agent_id)
            })
        else:
            self._send_json(404, {'error': 'not_found', 'path': self.path})

    def log_message(self, format, *args):
        # Suppress logging by default to keep test and runtime quiet
        pass


def make_handler(referee: AgoraReferee):
    class CustomHandler(AgoraHTTPHandler):
        pass
    CustomHandler.referee = referee
    return CustomHandler


def run_server(host: str = '0.0.0.0', port: int = 8080, referee: Optional[AgoraReferee] = None):
    ref = referee or AgoraReferee()
    handler_class = make_handler(ref)
    server = HTTPServer((host, port), handler_class)
    print(f"Agora Referee HTTP API listening on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run_server(port=port)
