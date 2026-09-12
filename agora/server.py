"""
agora.server - Standard library HTTP/REST server for AgoraReferee.
Implements Section 3 endpoints of docs/wire-spec.md:
- POST /referee/orders
- GET  /referee/book
- GET  /referee/ticks
- GET  /referee/accounts
- GET  /referee/leaderboard
- GET  /referee/health
- GET  /referee/instructions (alias /referee/rules)
"""

import hmac
import json
import os
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict
from agora.referee import AgoraReferee
from agora.galnet import GalNetEngine
from agora.spatial import STATIONS, COMMODITIES, ROUTES, get_route


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
    for agent in ('amos', 'marvin', 'zero', 'aerial'):
        env_val = os.environ.get(f'AGORA_TOKEN_{agent.upper()}')
        if env_val:
            tokens[agent] = env_val
    admin_val = os.environ.get('AGORA_ADMIN_TOKEN')
    if admin_val:
        tokens['admin'] = admin_val
    return tokens


class AgoraHTTPHandler(BaseHTTPRequestHandler):
    referee: Optional[AgoraReferee] = None
    galnet_engine: Optional[GalNetEngine] = None
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
            if expected_token and hmac.compare_digest(token, expected_token):
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

        if path in ('/referee/floor', '/referee/admin/floor'):
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            if auth_agent != 'admin':
                self._send_json(403, {
                    'v': 1, 'kind': 'reject',
                    'payload': {
                        'reason': 'unauthorized',
                        'detail': f"Only admin token can set floor state (authenticated as '{auth_agent}')"
                    }
                })
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
                payload = json.loads(body.decode('utf-8'))
            except Exception as e:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
                })
                return

            target = payload.get('floor')
            if not target:
                action = str(payload.get('action', '')).lower()
                if action in ('halt', 'pause', 'stop', 'close'):
                    target = 'closed'
                elif action in ('resume', 'open', 'start'):
                    target = 'open'

            if target not in ('open', 'closed'):
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_format', 'detail': "Expected floor='open' or 'closed'"}
                })
                return

            ref = self.referee or AgoraReferee()
            ref.set_floor(target)
            self._send_json(200, {
                'status': 'ok',
                'floor': ref.floor,
                'seq': ref.current_seq
            })
            return

        if path in ('/referee/orders/cancel', '/referee/orders/cancel_all'):
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    envelope = json.loads(body.decode('utf-8')) if body else {}
                    payload = envelope.get('payload', envelope) or {}
                except Exception as e:
                    self._send_json(400, {
                        'v': 1, 'kind': 'reject',
                        'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
                    })
                    return

            # Impersonation guard, same rule as order submission: non-admin callers
            # can only cancel their own orders. Admin may act on behalf of an agent
            # named in the payload; a non-admin's payload agent_id (if present at
            # all) must match who they authenticated as.
            claimed_agent = payload.get('agent_id')
            if auth_agent != 'admin':
                if claimed_agent and claimed_agent != auth_agent:
                    self._send_json(403, {
                        'v': 1, 'kind': 'reject',
                        'payload': {
                            'reason': 'unauthorized',
                            'detail': f"Authenticated as '{auth_agent}', but payload claims agent_id '{claimed_agent}'"
                        }
                    })
                    return
                target_agent = auth_agent
            else:
                target_agent = claimed_agent or auth_agent

            ref = self.referee or AgoraReferee()

            if path == '/referee/orders/cancel_all':
                result = ref.cancel_all(target_agent)
                self._send_json(200, result)
                return

            order_id = payload.get('order_id')
            if not order_id:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_format', 'detail': 'Missing required field: order_id'}
                })
                return

            result = ref.cancel_order(target_agent, order_id)
            if result.get('kind') == 'reject':
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path == '/galnet/step':
            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception:
                    pass
            engine = self.galnet_engine or (self.referee.galnet if self.referee else None)
            if not engine:
                engine = GalNetEngine()
                self.galnet_engine = engine

            round_num = payload.get('round', engine.current_round + 1)
            event = engine.step_round(round_num)
            if event and self.referee:
                self.referee.record_news(event.to_dict())
            self._send_json(200, {
                'status': 'ok',
                'round': round_num,
                'event': event.to_dict() if event else None,
                'active_shocks': engine.get_active_shocks()
            })
            return

        if path == '/galnet/shock':
            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception:
                    pass
            engine = self.galnet_engine or (self.referee.galnet if self.referee else None)
            if not engine:
                engine = GalNetEngine()
                self.galnet_engine = engine

            template_idx = payload.get('template_idx')
            round_num = payload.get('round', engine.current_round)
            event = engine.force_shock(round_num, template_idx=template_idx)
            if self.referee:
                self.referee.record_news(event.to_dict())
            self._send_json(200, {
                'status': 'ok',
                'event': event.to_dict(),
                'active_shocks': engine.get_active_shocks()
            })
            return

        if path == '/stations/transit':
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
                payload = json.loads(body.decode('utf-8'))
            except Exception as e:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
                })
                return

            claimed_agent = payload.get('agent_id')
            if auth_agent != 'admin' and claimed_agent and claimed_agent != auth_agent:
                self._send_json(403, {
                    'v': 1, 'kind': 'reject',
                    'payload': {
                        'reason': 'unauthorized',
                        'detail': f"Authenticated as '{auth_agent}', but payload claims agent_id '{claimed_agent}'"
                    }
                })
                return

            target_agent = auth_agent if auth_agent != 'admin' else (claimed_agent or auth_agent)
            destination = payload.get('destination')
            if not destination:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_format', 'detail': 'Missing required destination parameter'}
                })
                return

            commodity = payload.get('commodity', 'FRAG')
            cargo_qty = payload.get('cargo_qty', 0)

            ref = self.referee or AgoraReferee()
            result = ref.initiate_transit(
                agent_id=target_agent,
                destination=destination,
                commodity=commodity,
                cargo_qty=cargo_qty
            )
            if result.get('kind') == 'reject':
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path == '/stations/step_round':
            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception:
                    pass

            ref = self.referee or AgoraReferee()
            round_num = payload.get('round')
            result = ref.step_round(round_num=round_num)
            self._send_json(200, result)
            return

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
                'floor': ref.floor,
                'invariants_valid': valid,
                'errors': errors
            })
        elif path in ('/referee/floor', '/referee/admin/floor'):
            self._send_json(200, {
                'status': 'ok',
                'floor': ref.floor,
                'seq': ref.current_seq
            })
        elif path == '/referee/book':
            station_id = query_params.get('station_id', [None])[0]
            instrument = query_params.get('instrument', [None])[0]
            self._send_json(200, {
                'status': 'ok',
                'station_id': station_id or 'ceres',
                'instrument': instrument or getattr(ref, 'default_instrument', 'FRAG'),
                'book': ref.get_book_snapshot(station_id=station_id, instrument=instrument)
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
        elif path == '/referee/leaderboard':
            self._send_json(200, {
                'status': 'ok',
                'leaderboard': ref.get_leaderboard()
            })
        elif path in ('/referee/instructions', '/referee/rules'):
            format_param = query_params.get('format', ['json'])[0].lower()
            accept_header = self.headers.get('Accept', '')

            rules_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'docs', 'rules-of-engagement.md')
            raw_markdown = ""
            if os.path.exists(rules_path):
                try:
                    with open(rules_path, 'r', encoding='utf-8') as f:
                        raw_markdown = f.read()
                except Exception:
                    pass

            if format_param in ('raw', 'text', 'markdown') or 'text/markdown' in accept_header or 'text/plain' in accept_header:
                response_bytes = raw_markdown.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/markdown; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)
                return

            self._send_json(200, {
                'status': 'ok',
                'title': 'Station Agora — Syndicate Rules of Engagement & Strategy Window Protocol',
                'version': '1.0',
                'round_interval_sec': 300,
                'round_bell_role': '<@&1543462881624858624>',
                'endpoints': {
                    'book': 'GET /referee/book',
                    'leaderboard': 'GET /referee/leaderboard',
                    'ticks': 'GET /referee/ticks?since_seq=0',
                    'accounts': 'GET /referee/accounts (auth required)',
                    'orders': 'POST /referee/orders (auth required)',
                    'cancel_order': 'POST /referee/orders/cancel (auth required)',
                    'cancel_all': 'POST /referee/orders/cancel_all (auth required)',
                    'health': 'GET /referee/health',
                    'instructions': 'GET /referee/instructions',
                    'galnet_feed': 'GET /galnet/feed?limit=15',
                    'galnet_events': 'GET /galnet/events',
                    'galnet_drift': 'GET /galnet/drift?station_id=ceres&commodity=FUEL',
                    'galnet_step': 'POST /galnet/step',
                    'galnet_shock': 'POST /galnet/shock',
                    'station_prices': 'GET /stations/prices?station_id=mars&commodity=FRAG',
                    'station_routes': 'GET /stations/routes?origin=ceres&destination=mars',
                    'station_locations': 'GET /stations/locations',
                    'station_transit': 'POST /stations/transit (auth required)',
                    'station_step_round': 'POST /stations/step_round'
                },
                'rules': [
                    '1. Round Bell: Every 5 minutes, Agora Trade Terminal pings @robot (<@&1543462881624858624>) in #the-banana-stand.',
                    '2. Strategy Window Deliverable: Each robot ingests market telemetry, writes updated strategy bounds to its local config, and emits a 1-sentence public thesis to #the-banana-stand.',
                    '3. Micro Execution Loop: Decoupled sub-second quoting clients place orders via POST /referee/orders using bearer tokens without LLM inference latency.',
                    '4. Committed Balance: Resting orders commit capital until filled or cancelled. Clients must manage order lifecycle and prune stale orders.',
                    '5. Win Condition: Ranked on Mark-to-Market Net Worth: Liquid Credits + (Commodity Balance * Mark Price). Strict conservation and non-negativity enforced.'
                ],
                'fleet': {
                    'amos': 'First Solvency Combine',
                    'marvin': 'Ballistic Liquidation Co.',
                    'zero': 'Apex Vector Arbitrage',
                    'aerial': 'Zenith Drift Overwatch'
                },
                'markdown': raw_markdown,
                'docs_url': 'https://github.com/brockventures/market-sandbox/blob/main/docs/rules-of-engagement.md'
            })
        elif path == '/galnet/feed':
            engine = self.galnet_engine or (self.referee.galnet if self.referee else None)
            if not engine:
                engine = GalNetEngine()
                self.galnet_engine = engine
            limit_raw = query_params.get('limit', ['15'])[0]
            try:
                limit = int(limit_raw)
            except ValueError:
                limit = 15
            self._send_json(200, {
                'status': 'ok',
                'feed': engine.get_feed(limit=limit)
            })
        elif path == '/galnet/events':
            engine = self.galnet_engine or (self.referee.galnet if self.referee else None)
            if not engine:
                engine = GalNetEngine()
                self.galnet_engine = engine
            self._send_json(200, {
                'status': 'ok',
                'active_shocks': engine.get_active_shocks(),
                'feed': engine.get_feed()
            })
        elif path == '/galnet/drift':
            engine = self.galnet_engine or (self.referee.galnet if self.referee else None)
            if not engine:
                engine = GalNetEngine()
                self.galnet_engine = engine
            station_id = query_params.get('station_id', ['ceres'])[0].lower()
            commodity = query_params.get('commodity', ['FRAG'])[0].upper()
            self._send_json(200, {
                'status': 'ok',
                'station_id': station_id,
                'commodity': commodity,
                'drift_bias': engine.get_active_drift(station_id, commodity)
            })
        elif path == '/stations/prices':
            station_id = query_params.get('station_id', [None])[0]
            commodity = query_params.get('commodity', [None])[0]
            prices_data = ref.get_station_prices(station_id=station_id, commodity=commodity)
            self._send_json(200, {
                'status': 'ok',
                'data': prices_data
            })
        elif path == '/stations/routes':
            origin = query_params.get('origin', [None])[0]
            destination = query_params.get('destination', [None])[0]
            if origin and destination:
                route = get_route(origin, destination)
                self._send_json(200, {
                    'status': 'ok',
                    'origin': origin.lower(),
                    'destination': destination.lower(),
                    'route': route
                })
            else:
                formatted_routes = [
                    {'origin': k[0], 'destination': k[1], 'rounds': v['rounds'], 'fuel': v['fuel']}
                    for k, v in ROUTES.items()
                ]
                self._send_json(200, {
                    'status': 'ok',
                    'stations': STATIONS,
                    'commodities': COMMODITIES,
                    'routes': formatted_routes
                })
        elif path == '/stations/locations':
            agent_id = query_params.get('agent_id', [None])[0]
            if agent_id:
                loc = ref.get_vessel_location(agent_id)
                self._send_json(200, {
                    'status': 'ok',
                    'location': loc
                })
            else:
                locs = ref.get_all_vessel_locations()
                self._send_json(200, {
                    'status': 'ok',
                    'locations': locs
                })
        else:
            self._send_json(404, {'error': 'not_found', 'path': self.path})

    def log_message(self, format, *args):
        # Suppress logging by default to keep test and runtime quiet
        pass


def make_handler(referee: AgoraReferee, auth_tokens: Optional[Dict[str, str]] = None, galnet: Optional[GalNetEngine] = None):
    class CustomHandler(AgoraHTTPHandler):
        pass
    CustomHandler.referee = referee
    CustomHandler.auth_tokens = auth_tokens
    CustomHandler.galnet_engine = galnet or getattr(referee, 'galnet', None) or GalNetEngine()
    return CustomHandler


def run_server(host: Optional[str] = None, port: int = 8080, referee: Optional[AgoraReferee] = None, auth_tokens: Optional[Dict[str, str]] = None):
    bind_host = host or os.environ.get('AGORA_HOST', '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1')
    db_path = os.environ.get('AGORA_DB_PATH', 'agora.db')
    ref = referee or AgoraReferee(db_path=db_path)
    tokens = auth_tokens if auth_tokens is not None else get_configured_tokens()
    handler_class = make_handler(ref, auth_tokens=tokens)
    server = HTTPServer((bind_host, port), handler_class)
    print(f"Agora Referee HTTP API listening on {bind_host}:{port} (db: {db_path})")
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
    raw_port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('PORT', '8080')
    port = int(raw_port)
    host = os.environ.get('AGORA_HOST', '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1')
    run_server(host=host, port=port)

