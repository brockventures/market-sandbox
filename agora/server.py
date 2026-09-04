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
import os
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict
from agora.referee import AgoraReferee


def get_configured_tokens() -> Dict[str, str]:
    """
    Load agent bearer tokens from environment variables.
    Supports AGORA_AUTH_TOKENS (JSON dict), AGORA_TOKEN_<AGENT>, and AGORA_ADMIN_TOKEN.
    """
    tokens = {}
    if os.environ.get('AGORA_AUTH_TOKENS'):
        try:
            tokens.update(json.loads(os.environ['AGORA_AUTH_TOKENS']))
        except Exception:
            pass
    for agent in ('amos', 'marvin', 'zero'):
        env_val = os.environ.get(f'AGORA_TOKEN_{agent.upper()}')
        if env_val:
            tokens[agent] = env_val
    admin_val = os.environ.get('AGORA_ADMIN_TOKEN')
    if admin_val:
        tokens['admin'] = admin_val
    return tokens


class AgoraHTTPHandler(BaseHTTPRequestHandler):
    referee: Optional[AgoraReferee] = None
    auth_tokens: Optional[Dict[str, str]] = None  # agent_id -> bearer_token

    def _send_json(self, status_code: int, data: dict):
        response_bytes = json.dumps(data, indent=2).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def _authenticate_request(self) -> tuple[Optional[str], Optional[dict]]:
        """
        Validates Authorization: Bearer <token> header against configured auth_tokens.
        Returns (authenticated_agent_id, None) on success.
        Returns (None, error_response_dict) on failure.
        """
        auth_header = self.headers.get('Authorization', '').strip()
        if not auth_header:
            return None, {
                'v': 1, 'kind': 'reject',
                'payload': {
                    'reason': 'unauthorized',
                    'detail': 'Missing Authorization header. Expected Bearer <token>'
                }
            }
        parts = auth_header.split(' ', 1)
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            return None, {
                'v': 1, 'kind': 'reject',
                'payload': {
                    'reason': 'unauthorized',
                    'detail': 'Invalid Authorization header format. Expected Bearer <token>'
                }
            }
        token = parts[1].strip()

        tokens = self.auth_tokens if self.auth_tokens is not None else get_configured_tokens()
        for agent_id, expected_token in tokens.items():
            if expected_token and token == expected_token:
                return agent_id, None

        return None, {
            'v': 1, 'kind': 'reject',
            'payload': {
                'reason': 'unauthorized',
                'detail': 'Invalid or unrecognized bearer token'
            }
        }

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')

        if path != '/referee/orders':
            self._send_json(404, {'error': 'not_found', 'path': self.path})
            return

        # 1. Authenticate caller before inspecting body
        auth_agent, auth_err = self._authenticate_request()
        if auth_err:
            self._send_json(401, auth_err)
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

        # 2. Impersonation guard: payload agent_id must match authenticated agent
        payload = envelope.get('payload', {})
        claimed_agent = payload.get('agent_id')
        if auth_agent != 'admin' and claimed_agent != auth_agent:
            self._send_json(403, {
                'v': 1, 'kind': 'reject',
                'payload': {
                    'reason': 'unauthorized',
                    'detail': f"Authenticated as '{auth_agent}', but payload claims agent_id '{claimed_agent}'"
                }
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
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            agent_id = query_params.get('agent_id', [None])[0]
            if auth_agent != 'admin':
                if agent_id and agent_id != auth_agent:
                    self._send_json(403, {
                        'v': 1, 'kind': 'reject',
                        'payload': {
                            'reason': 'unauthorized',
                            'detail': f"Authenticated as '{auth_agent}', cannot inspect account '{agent_id}'"
                        }
                    })
                    return
                # Default to viewing own account
                agent_id = auth_agent

            self._send_json(200, {
                'status': 'ok',
                'accounts': ref.get_accounts(agent_id=agent_id)
            })
        else:
            self._send_json(404, {'error': 'not_found', 'path': self.path})

    def log_message(self, format, *args):
        # Suppress logging by default to keep test and runtime quiet
        pass


def make_handler(referee: AgoraReferee, auth_tokens: Optional[Dict[str, str]] = None):
    class CustomHandler(AgoraHTTPHandler):
        pass
    CustomHandler.referee = referee
    CustomHandler.auth_tokens = auth_tokens
    return CustomHandler


def run_server(host: str = '0.0.0.0', port: int = 8080, referee: Optional[AgoraReferee] = None, auth_tokens: Optional[Dict[str, str]] = None):
    ref = referee or AgoraReferee()
    tokens = auth_tokens if auth_tokens is not None else get_configured_tokens()
    handler_class = make_handler(ref, auth_tokens=tokens)
    server = HTTPServer((host, port), handler_class)
    print(f"Agora Referee HTTP API listening on {host}:{port}")
    if tokens:
        print(f"Configured auth tokens for agents: {list(tokens.keys())}")
    else:
        print("WARNING: No auth tokens configured. All authenticated endpoints will reject requests.")
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

