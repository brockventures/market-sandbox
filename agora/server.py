import math
"""
agora.server - Standard library HTTP/REST server for AgoraReferee.
Implements Section 3 endpoints of docs/wire-spec.md:
- POST /referee/orders
- GET  /referee/book
- GET  /referee/ticks
- GET  /referee/accounts
- GET  /referee/leaderboard
- GET  /referee/history
- GET  /referee/briefing (alias /briefing, /llms.txt)
- GET  /referee/health
- GET  /referee/instructions (alias /referee/rules)
"""

import hmac
import io
import json
import os
import time
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer, HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict
from agora.referee import AgoraReferee, REACTIVE_SHELF_SKEW
from agora.exchange import DEFAULT_VOL
from agora.hazards import DEFAULT_P_DELAY, DEFAULT_P_LOSS
from agora import piracy as _piracy
from agora import fleet as fleet_mod
from agora.galnet import GalNetEngine
from agora.spatial import STATIONS, COMMODITIES, ROUTES, get_route, get_alignment_windows
from agora.websocket import handle_terminal_websocket
from agora.ticker import TickerEngine, DEFAULT_TICK_INTERVAL_SEC, DEFAULT_INACTIVITY_ROUNDS


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
    combine_val = os.environ.get('AGORA_COMBINE_TOKEN', 'agora-combine-2026')
    if combine_val:
        tokens['combine'] = combine_val
    return tokens


class AgoraHTTPHandler(BaseHTTPRequestHandler):
    referee: Optional[AgoraReferee] = None
    galnet_engine: Optional[GalNetEngine] = None
    auth_tokens: Optional[Dict[str, str]] = None  # agent_id -> bearer_token
    ticker: Optional["TickerEngine"] = None

    def _send_json(self, status_code: int, data: dict):
        response_bytes = json.dumps(data, indent=2).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def _reader(self) -> Optional[str]:
        """Who is reading, for fog: a fleet id for a valid per-fleet token,
        'admin' for the admin token, None (the public view) for the shared
        combine token, a bad token, or no token at all."""
        if not self.headers.get('Authorization', '').strip():
            return None
        agent, err = self._authenticate_request()
        if err or agent == 'combine':
            return None
        return agent

    def _fogged(self, detail: str) -> None:
        self._send_json(403, {'v': 1, 'kind': 'reject', 'payload': {
            'reason': 'fogged',
            'detail': detail + ' Exact prices are visible only at the station you are docked at, '
                               'with your own fleet token. Everything else: GET /referee/briefing.'}})

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

    # ------------------------------------------------------------ locking
    # The referee has ONE sqlite connection, shared by every request thread
    # (ThreadingHTTPServer), the ticker and the websocket streams. Python's
    # sqlite3 is not safe to share without a lock: unlocked reads returned
    # wrong rows and NULLs, and an unlocked commit landed inside another
    # thread's locked transaction and lost a ledger write (#197). So each
    # request runs start to finish under ref.lock (reentrant; the referee's
    # own writers re-take it). Two things stay outside it:
    #   - the request body is read from the socket first, so a slow or stalled
    #     client cannot hold the referee hostage;
    #   - the response is buffered and written to the socket after release.
    # A websocket upgrade is long-lived and is not wrapped: the stream takes
    # the lock per frame it builds (agora/websocket.py).

    def _is_ws_upgrade(self) -> bool:
        path = urllib.parse.urlparse(self.path).path.rstrip('/')
        return path == '/ws/terminal' and self.headers.get('Upgrade', '').lower() == 'websocket'

    def _run_locked(self, body) -> None:
        ref = self.referee
        if ref is None:  # a throwaway per-request referee: nothing shared
            body()
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            length = 0  # the handler reports the bad header itself
        if length > 0:
            self.rfile = io.BytesIO(self.rfile.read(length))
        real_wfile = self.wfile
        buf = io.BytesIO()
        self.wfile = buf
        try:
            with ref.lock:
                body()
        finally:
            self.wfile = real_wfile
            out = buf.getvalue()
            if out:
                real_wfile.write(out)

    def do_POST(self):
        self._run_locked(self._do_POST)

    def do_GET(self):
        if self._is_ws_upgrade():
            self._do_GET()
            return
        self._run_locked(self._do_GET)

    def _do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')

        if path == '/referee/admin/new_game':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            # Deliberately narrower than /referee/admin/reset's admin-token
            # gate: starting a brand-new game (fresh RNG seed, new opening
            # market) is a specific short allowlist's call, not any
            # admin-token holder's. Mike, #lounge 2026-09-18: widened from
            # amos-only to also cover zero.
            NEW_GAME_AUTHORIZED_AGENTS = ('amos', 'zero')
            if auth_agent not in NEW_GAME_AUTHORIZED_AGENTS:
                self._send_json(403, {
                    'v': 1, 'kind': 'reject',
                    'payload': {
                        'reason': 'unauthorized',
                        'detail': f"Only {NEW_GAME_AUTHORIZED_AGENTS} tokens can start a new game (authenticated as '{auth_agent}')"
                    }
                })
                return

            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception as e:
                    self._send_json(400, {
                        'v': 1, 'kind': 'reject',
                        'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
                    })
                    return

            if not payload.get('confirm'):
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {
                        'reason': 'confirm_required',
                        'detail': 'This wipes every trade, order, and balance and rolls a new opening market. POST {"confirm": true} to actually start a new game.'
                    }
                })
                return

            seed = payload.get('seed')
            warmup_rounds = payload.get('warmup_rounds')
            depots = payload.get('depots', payload.get('enable_depots'))
            asymmetric = payload.get('asymmetric')
            spawn_locations = payload.get('spawn_locations')
            ref = self.referee or AgoraReferee()
            result = ref.new_game(
                seed=seed,
                warmup_rounds=warmup_rounds,
                depots=depots,
                asymmetric=asymmetric,
                spawn_map=spawn_locations,
                depot_model=payload.get('depot_model'),
                band_pct=payload.get('band_pct'),
                peer_trades=payload.get('peer_trades'),
                fog=payload.get('fog'),
                idle_fee=payload.get('idle_fee'),
                rival_shares=payload.get('rival_shares'),
                exchange_shares=payload.get('exchange_shares'),
                exchange_vol=payload.get('exchange_vol'),
                exchange_momentum=payload.get('exchange_momentum'),
                goods_momentum=payload.get('goods_momentum'),
                inventory_sensitivity=payload.get('inventory_sensitivity'),
                flow_sensitivity=payload.get('flow_sensitivity'),
                delivery_scale=payload.get('delivery_scale'),
                contracts=payload.get('contracts'),
                hazards=payload.get('hazards'),
                corporate=payload.get('corporate'),
                upgrades=payload.get('upgrades'),
                piracy=payload.get('piracy'),
                events=payload.get('events'),
                order_flow=payload.get('order_flow'),
                standing=payload.get('standing'),
            )
            self._send_json(200, {'v': 1, 'kind': 'new_game_ok', 'payload': result})
            return

        if path == '/referee/admin/reset':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            if auth_agent != 'admin':
                self._send_json(403, {
                    'v': 1, 'kind': 'reject',
                    'payload': {
                        'reason': 'unauthorized',
                        'detail': f"Only admin token can reset game state (authenticated as '{auth_agent}')"
                    }
                })
                return

            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception as e:
                    self._send_json(400, {
                        'v': 1, 'kind': 'reject',
                        'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
                    })
                    return

            if not payload.get('confirm'):
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {
                        'reason': 'confirm_required',
                        'detail': 'This wipes every trade, order, and balance. POST {"confirm": true} to actually reset.'
                    }
                })
                return

            depots = payload.get('depots', payload.get('enable_depots'))
            asymmetric = payload.get('asymmetric')
            spawn_locations = payload.get('spawn_locations')
            ref = self.referee or AgoraReferee()
            result = ref.reset_to_genesis(
                depots=depots,
                asymmetric=asymmetric,
                spawn_map=spawn_locations,
                depot_model=payload.get('depot_model'),
                band_pct=payload.get('band_pct'),
            )
            self._send_json(200, {'v': 1, 'kind': 'reset_ok', 'payload': result})
            return

        if path == '/referee/admin/depots/refresh':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            ref = self.referee or AgoraReferee()
            ref.refresh_depot_liquidity()
            summary = ref.get_depot_summary()
            self._send_json(200, {'v': 1, 'kind': 'depots_refreshed', 'payload': summary})
            return

        if path == '/referee/admin/burst':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            if self.ticker is None:
                self._send_json(409, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'ticker_not_configured', 'detail': 'No TickerEngine is wired into this server instance.'}
                })
                return

            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception as e:
                    self._send_json(400, {
                        'v': 1, 'kind': 'reject',
                        'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
                    })
                    return

            rounds = payload.get('rounds')
            interval_sec = payload.get('interval_sec', 30.0)
            if rounds is None:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'rounds_required', 'detail': 'POST {"rounds": N, "interval_sec": X} to start a burst run.'}
                })
                return

            try:
                result = self.ticker.start_burst(rounds=rounds, interval_sec=interval_sec)
            except ValueError as e:
                self._send_json(409, {'v': 1, 'kind': 'reject', 'payload': {'reason': 'burst_rejected', 'detail': str(e)}})
                return

            self._send_json(200, {
                'v': 1, 'kind': 'burst_started',
                'payload': {**result, 'triggered_by': auth_agent}
            })
            return

        if path in ('/referee/admin/burst/cancel', '/referee/admin/burst/stop'):
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            if self.ticker is None:
                self._send_json(409, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'ticker_not_configured', 'detail': 'No TickerEngine is wired into this server instance.'}
                })
                return
            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception:
                    pass
            force = payload.get('force', False)
            cancelled = self.ticker.cancel_burst(force=force)
            self._send_json(200, {
                'v': 1, 'kind': 'burst_cancelled',
                'payload': {'cancelled': cancelled, 'status': self.ticker.status()}
            })
            return

        if path == '/referee/admin/burst/reset':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            if self.ticker is None:
                self._send_json(409, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'ticker_not_configured', 'detail': 'No TickerEngine is wired into this server instance.'}
                })
                return
            st = self.ticker.reset_burst()
            self._send_json(200, {
                'v': 1, 'kind': 'burst_reset',
                'payload': {'reset': True, 'status': st, 'triggered_by': auth_agent}
            })
            return

        if path == '/referee/admin/ticker/pause':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            if self.ticker is None:
                self._send_json(409, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'ticker_not_configured', 'detail': 'No TickerEngine is wired into this server instance.'}
                })
                return
            self.ticker.pause(reason=f"admin_pause:{auth_agent}")
            self._send_json(200, {'v': 1, 'kind': 'ticker_paused', 'payload': self.ticker.status()})
            return

        if path == '/referee/admin/ticker/resume':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            if self.ticker is None:
                self._send_json(409, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'ticker_not_configured', 'detail': 'No TickerEngine is wired into this server instance.'}
                })
                return
            self.ticker.resume()
            self._send_json(200, {'v': 1, 'kind': 'ticker_resumed', 'payload': self.ticker.status()})
            return

        if path == '/referee/admin/ticker/config':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            if self.ticker is None:
                self._send_json(409, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'ticker_not_configured', 'detail': 'No TickerEngine is wired into this server instance.'}
                })
                return

            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception as e:
                    self._send_json(400, {
                        'v': 1, 'kind': 'reject',
                        'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
                    })
                    return

            inactivity_rounds = payload.get('inactivity_rounds')
            interval_sec = payload.get('interval_sec')
            st = self.ticker.configure(interval_sec=interval_sec, inactivity_rounds=inactivity_rounds)
            self._send_json(200, {'v': 1, 'kind': 'ticker_configured', 'payload': st})
            return

        if path == '/referee/admin/fleets':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            if auth_agent != 'admin':
                self._send_json(403, {
                    'v': 1, 'kind': 'reject',
                    'payload': {
                        'reason': 'unauthorized',
                        'detail': f"Only admin token can edit the fleet roster (authenticated as '{auth_agent}')"
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

            agent_id = payload.get('agent_id')
            required = ('agent_id', 'display_name', 'genesis_cr', 'genesis_frag', 'genesis_fuel')
            missing = [f for f in required if payload.get(f) is None]
            if missing:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_format', 'detail': f"Missing required field(s): {', '.join(missing)}"}
                })
                return

            if '/' in str(agent_id):
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_format', 'detail': "agent_id must not contain '/'"}
                })
                return

            ref = self.referee or AgoraReferee()
            ref.upsert_fleet_roster(
                agent_id, payload['display_name'], payload.get('home_station', 'ceres'),
                payload['genesis_cr'], payload['genesis_frag'], payload['genesis_fuel'],
            )
            self._send_json(200, {
                'v': 1, 'kind': 'fleet_roster_ok',
                'payload': {
                    'agent_id': agent_id,
                    'note': 'Takes effect on the next POST /referee/admin/reset, not retroactively.'
                }
            })
            return

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
            if auth_agent not in ('admin', 'combine') and claimed_agent and claimed_agent != auth_agent:
                self._send_json(403, {
                    'v': 1, 'kind': 'reject',
                    'payload': {
                        'reason': 'unauthorized',
                        'detail': f"Authenticated as '{auth_agent}', but payload claims agent_id '{claimed_agent}'"
                    }
                })
                return

            target_agent = claimed_agent if (auth_agent in ('admin', 'combine') and claimed_agent) else auth_agent
            destination = payload.get('destination')
            if not destination:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_format', 'detail': 'Missing required destination parameter'}
                })
                return

            commodity = payload.get('commodity', 'FRAG')
            cargo_qty = payload.get('cargo_qty', 0)
            perishable = payload.get('perishable')

            ref = self.referee or AgoraReferee()
            if '/' in str(target_agent):
                self._send_json(400, {'v': 1, 'kind': 'reject', 'payload': {
                    'reason': 'invalid_format', 'detail': 'agent_id is a fleet; name the ship in vessel_id'}})
                return
            result = ref.initiate_transit(
                agent_id=target_agent,
                destination=destination,
                commodity=commodity,
                cargo_qty=cargo_qty,
                perishable=perishable,
                escort=bool(payload.get('escort')),
                vessel_id=payload.get('vessel_id') or payload.get('vessel'),
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

        if path == '/equity/borrow':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': 'Empty request body'})
                return

            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode('utf-8'))
            except Exception as e:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'})
                return

            claimed_agent = payload.get('borrower_id', payload.get('agent_id'))
            if auth_agent != 'admin' and claimed_agent and claimed_agent != auth_agent:
                self._send_json(403, {'ok': False, 'reason': 'unauthorized', 'detail': f"Authenticated as '{auth_agent}', but payload claims '{claimed_agent}'"})
                return

            target_agent = auth_agent if auth_agent != 'admin' else (claimed_agent or auth_agent)
            equity_symbol = payload.get('equity_symbol')
            shares = payload.get('shares', 0)
            collateral_cr = payload.get('collateral_cr')
            lender_id = payload.get('lender_id')

            ref = self.referee or AgoraReferee()
            result = ref.borrow_equity(
                borrower_id=target_agent,
                equity_symbol=equity_symbol,
                shares=shares,
                collateral_cr=collateral_cr,
                lender_id=lender_id
            )
            if not result.get('ok'):
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path == '/equity/return':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': 'Empty request body'})
                return

            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode('utf-8'))
            except Exception as e:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'})
                return

            claimed_agent = payload.get('borrower_id', payload.get('agent_id'))
            if auth_agent != 'admin' and claimed_agent and claimed_agent != auth_agent:
                self._send_json(403, {'ok': False, 'reason': 'unauthorized', 'detail': f"Authenticated as '{auth_agent}', but payload claims '{claimed_agent}'"})
                return

            target_agent = auth_agent if auth_agent != 'admin' else (claimed_agent or auth_agent)
            loan_id = payload.get('loan_id')

            ref = self.referee or AgoraReferee()
            result = ref.return_equity_loan(
                borrower_id=target_agent,
                loan_id=loan_id
            )
            if not result.get('ok'):
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path == '/salvage/distress':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception as e:
                    self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'})
                    return

            claimed_agent = payload.get('agent_id')
            if auth_agent != 'admin' and claimed_agent and claimed_agent != auth_agent:
                self._send_json(403, {'ok': False, 'reason': 'unauthorized', 'detail': f"Authenticated as '{auth_agent}', but payload claims '{claimed_agent}'"})
                return

            target_agent = auth_agent if auth_agent != 'admin' else (claimed_agent or auth_agent)
            location = payload.get('location')
            cargo_bounty = payload.get('cargo_bounty')
            transit_id = payload.get('transit_id')
            fuel_needed = payload.get('fuel_needed', 15)
            max_reward_cr = payload.get('max_reward_cr', 0)
            reason = payload.get('reason', 'out_of_fuel')

            ref = self.referee or AgoraReferee()
            result = ref.broadcast_distress(
                agent_id=target_agent,
                location=location,
                cargo_bounty=cargo_bounty,
                transit_id=transit_id,
                fuel_needed=fuel_needed,
                max_reward_cr=max_reward_cr,
                reason=reason
            )
            if not result.get('ok'):
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path == '/salvage/quote':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': 'Empty request body'})
                return

            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode('utf-8'))
            except Exception as e:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'})
                return

            claimed_agent = payload.get('rescuer_id', payload.get('agent_id'))
            if auth_agent != 'admin' and claimed_agent and claimed_agent != auth_agent:
                self._send_json(403, {'ok': False, 'reason': 'unauthorized', 'detail': f"Authenticated as '{auth_agent}', but payload claims '{claimed_agent}'"})
                return

            target_agent = auth_agent if auth_agent != 'admin' else (claimed_agent or auth_agent)
            rfq_id = payload.get('rfq_id')
            fuel_offered = payload.get('fuel_offered', 0)
            price_cr = payload.get('price_cr', 0)

            ref = self.referee or AgoraReferee()
            result = ref.submit_rescue_quote(
                rescuer_id=target_agent,
                rfq_id=rfq_id,
                fuel_offered=fuel_offered,
                price_cr=price_cr
            )
            if not result.get('ok'):
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path == '/salvage/accept_quote':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': 'Empty request body'})
                return

            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode('utf-8'))
            except Exception as e:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'})
                return

            claimed_agent = payload.get('agent_id')
            if auth_agent != 'admin' and claimed_agent and claimed_agent != auth_agent:
                self._send_json(403, {'ok': False, 'reason': 'unauthorized', 'detail': f"Authenticated as '{auth_agent}', but payload claims '{claimed_agent}'"})
                return

            target_agent = auth_agent if auth_agent != 'admin' else (claimed_agent or auth_agent)
            quote_id = payload.get('quote_id')

            ref = self.referee or AgoraReferee()
            result = ref.accept_rescue_quote(
                agent_id=target_agent,
                quote_id=quote_id
            )
            if not result.get('ok'):
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path == '/salvage/claim':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': 'Empty request body'})
                return

            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode('utf-8'))
            except Exception as e:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'})
                return

            claimed_agent = payload.get('salvager_id', payload.get('agent_id'))
            if auth_agent != 'admin' and claimed_agent and claimed_agent != auth_agent:
                self._send_json(403, {'ok': False, 'reason': 'unauthorized', 'detail': f"Authenticated as '{auth_agent}', but payload claims '{claimed_agent}'"})
                return

            target_agent = auth_agent if auth_agent != 'admin' else (claimed_agent or auth_agent)
            beacon_id = payload.get('beacon_id')

            ref = self.referee or AgoraReferee()
            result = ref.claim_salvage(
                salvager_id=target_agent,
                beacon_id=beacon_id
            )
            if not result.get('ok'):
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path == '/circuit_breaker/halt':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': 'Empty request body'})
                return

            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode('utf-8'))
            except Exception as e:
                self._send_json(400, {'ok': False, 'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'})
                return

            station_id = payload.get('station_id', 'ceres')
            instrument = payload.get('instrument', 'FRAG')
            trigger_price = payload.get('trigger_price', 0.0)
            reason = payload.get('reason', 'manual_halt')

            ref = self.referee or AgoraReferee()
            result = ref.trigger_circuit_breaker_halt(
                station_id=station_id,
                instrument=instrument,
                trigger_price=trigger_price,
                reason=reason
            )
            self._send_json(200, result)
            return

        if path == '/circuit_breaker/reopen':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            payload = {}
            if content_length:
                try:
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode('utf-8')) if body else {}
                except Exception:
                    pass

            station_id = payload.get('station_id', 'ceres')
            instrument = payload.get('instrument', 'FRAG')

            ref = self.referee or AgoraReferee()
            result = ref.reopen_circuit_breaker_auction(
                station_id=station_id,
                instrument=instrument
            )
            if not result.get('ok'):
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return

        if path in ('/referee/peer/offer', '/referee/peer/accept', '/referee/peer/cancel'):
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception as e:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}})
                return
            ref = self.referee or AgoraReferee()
            if not ref.peer_trades:
                self._send_json(409, {'v': 1, 'kind': 'reject', 'payload': {
                    'reason': 'peer_trades_disabled',
                    'detail': 'Peer trades are off in this game. Start one with new_game {"peer_trades": true}.'}})
                return
            # Same impersonation rule as /referee/orders: admin and the shared
            # combine token may act for a named fleet; a fleet token acts as itself.
            agent = data.get('agent_id') if auth_agent in ('admin', 'combine') else auth_agent
            if not agent:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'agent_required', 'detail': 'agent_id is required with this token'}})
                return
            if path.endswith('/offer'):
                try:
                    qty, price = int(data.get('qty', 0)), int(data.get('price', data.get('limit_price', 0)))
                except (TypeError, ValueError):
                    qty, price = 0, 0
                result = ref.peer.offer(agent, data.get('station_id') or data.get('station'),
                                        data.get('instrument') or data.get('commodity'), qty, price,
                                        vessel_id=data.get('vessel_id'))
            elif path.endswith('/accept'):
                result = ref.peer.accept(agent, str(data.get('escrow_id', '')), vessel_id=data.get('vessel_id'))
            else:
                result = ref.peer.cancel(agent, str(data.get('escrow_id', '')))
            self._send_json(400 if result.get('kind') == 'reject' else 200, result)
            return

        if path in ('/referee/covert/wiretap', '/referee/covert/sabotage'):
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception as e:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}})
                return
            ref = self.referee or AgoraReferee()
            if not getattr(ref, 'events_enabled', False):
                self._send_json(409, {'v': 1, 'kind': 'reject', 'payload': {
                    'reason': 'covert_disabled', 'detail': 'Covert operations are off in this game.'}})
                return
            agent = data.get('agent_id') if auth_agent in ('admin', 'combine') else auth_agent
            if not agent:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'agent_required', 'detail': 'agent_id is required with this token'}})
                return
            if path == '/referee/covert/wiretap':
                result = ref.covert.plant_wiretap(agent, str(data.get('target', '')))
            else:
                result = ref.covert.execute_sabotage(agent, str(data.get('target', '')), str(data.get('mode', 'auto')),
                                                     target_vessel=data.get('target_vessel'))
            self._send_json(400 if result.get('kind') == 'reject' else 200, result)
            return

        if path in ('/referee/vessels/buy', '/referee/vessels/transfer', '/referee/vessels/scrap'):
            # Ships (#175): buy one while docked; move goods between two of
            # your ships (or a ship and your station hold) at one station.
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
                if not isinstance(data, dict):
                    raise ValueError('expected a JSON object')
            except Exception as e:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}})
                return
            ref = self.referee or AgoraReferee()
            agent = data.get('agent_id') if auth_agent in ('admin', 'combine') else auth_agent
            if not agent or '/' in str(agent):
                self._send_json(400, {'v': 1, 'kind': 'reject', 'payload': {
                    'reason': 'agent_required', 'detail': 'agent_id (a fleet, not a ship) is required with this token'}})
                return
            if path.endswith('/buy'):
                result = ref.fleet.buy(str(agent), data.get('vessel_id') or data.get('at'))
            elif path.endswith('/scrap'):
                result = ref.fleet.scrap(str(agent), data.get('vessel_id'))
            else:
                result = ref.fleet.transfer(str(agent), data.get('from'), data.get('to'),
                                            str(data.get('instrument') or data.get('commodity') or ''), data.get('qty'))
            self._send_json(400 if result.get('kind') == 'reject' else 200, result)
            return

        if path == '/referee/upgrades/buy':
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception as e:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}})
                return
            ref = self.referee or AgoraReferee()
            if not ref.upgrades_enabled:
                self._send_json(409, {'v': 1, 'kind': 'reject', 'payload': {
                    'reason': 'upgrades_disabled', 'detail': 'Upgrades are off in this game.'}})
                return
            agent = data.get('agent_id') if auth_agent in ('admin', 'combine') else auth_agent
            if not agent:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'agent_required', 'detail': 'agent_id is required with this token'}})
                return
            result = ref.upgrades.buy(agent, str(data.get('kind', '')))
            self._send_json(400 if result.get('kind') == 'reject' else 200, result)
            return

        # Corporate governance & hostile M&A (#164): POST /referee/corporate/...
        if path.startswith('/referee/corporate/'):
            action = path[len('/referee/corporate/'):].strip('/')
            allowed_actions = (
                'tender_offer', 'tender_accept', 'tender_cancel',
                'tender/offer', 'tender/accept', 'tender/cancel',
                'poison_pill', 'rights_exercise',
                'loan', 'loan_offer', 'loan_accept', 'loan_cancel',
                'loan/offer', 'loan/accept', 'loan/cancel',
                'debt_buy', 'loan_repay'
            )
            if action in allowed_actions:
                auth_agent, auth_err = self._authenticate_request()
                if auth_err:
                    self._send_json(401, auth_err)
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
                except Exception as e:
                    self._send_json(400, {'v': 1, 'kind': 'reject',
                                          'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}})
                    return
                if not isinstance(data, dict):
                    self._send_json(400, {'v': 1, 'kind': 'reject',
                                          'payload': {'reason': 'invalid_format', 'detail': 'JSON body must be an object'}})
                    return
                ref = self.referee or AgoraReferee()
                if not getattr(ref, 'corporate_enabled', False):
                    self._send_json(409, {'v': 1, 'kind': 'reject', 'payload': {
                        'reason': 'corporate_disabled', 'detail': 'Corporate governance & takeovers are off in this game.'}})
                    return
                agent = data.get('agent_id') if auth_agent in ('admin', 'combine') else auth_agent
                if not agent:
                    self._send_json(400, {'v': 1, 'kind': 'reject',
                                          'payload': {'reason': 'agent_required', 'detail': 'agent_id is required with this token'}})
                    return
                try:
                    if action in ('tender_offer', 'tender/offer'):
                        result = ref.corporate.create_tender_offer(agent, str(data.get('target', '')), data.get('price', 0), data.get('shares', 0))
                    elif action in ('tender_accept', 'tender/accept'):
                        result = ref.corporate.accept_tender_offer(agent, data.get('offer_id', 0), data.get('shares', 0))
                    elif action in ('tender_cancel', 'tender/cancel'):
                        result = ref.corporate.cancel_tender_offer(agent, data.get('offer_id', 0))
                    elif action == 'poison_pill':
                        result = ref.corporate.activate_poison_pill(str(data.get('target') or agent), caller=agent)
                    elif action == 'rights_exercise':
                        result = ref.corporate.exercise_rights(agent, str(data.get('target', '')), data.get('qty', 0))
                    elif action in ('loan_offer', 'loan', 'loan/offer'):
                        result = ref.corporate.create_loan_offer(agent, str(data.get('borrower', '')), data.get('principal', 0),
                                                                 interest_rate=data.get('interest_rate', 0.20),
                                                                 due_rounds=data.get('due_rounds', 5))
                    elif action in ('loan_accept', 'loan/accept'):
                        result = ref.corporate.accept_loan_offer(agent, data.get('offer_id', 0))
                    elif action in ('loan_cancel', 'loan/cancel'):
                        result = ref.corporate.cancel_loan_offer(agent, data.get('offer_id', 0))
                    elif action == 'debt_buy':
                        result = ref.corporate.buy_distressed_debt(agent, str(data.get('debtor', '')), data.get('amount', 0))
                    elif action == 'loan_repay':
                        result = ref.corporate.repay_loan(agent, data.get('loan_id', 0))
                    else:
                        result = {'v': 1, 'kind': 'reject', 'payload': {'reason': 'unknown_action', 'detail': f"Unknown governance action '{action}'"}}
                except (ValueError, TypeError, OverflowError) as e:
                    self._send_json(400, {'v': 1, 'kind': 'reject', 'payload': {'reason': 'invalid_parameters', 'detail': str(e)}})
                    return
                self._send_json(400 if result.get('kind') == 'reject' else 200, result)
                return

        # Station contracts (#115): POST /referee/contracts/{id}/{claim|list|buy|deliver}
        parts = path.strip('/').split('/')
        if len(parts) == 4 and parts[:2] == ['referee', 'contracts'] and parts[3] in ('claim', 'list', 'buy', 'deliver'):
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception as e:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}})
                return
            if not isinstance(data, dict):
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'invalid_format', 'detail': 'JSON body must be an object'}})
                return
            ref = self.referee or AgoraReferee()
            if not ref.contracts_enabled:
                self._send_json(409, {'v': 1, 'kind': 'reject', 'payload': {
                    'reason': 'contracts_disabled',
                    'detail': 'Contracts are off in this game. Start one with new_game {"contracts": true}.'}})
                return
            agent = data.get('agent_id') if auth_agent in ('admin', 'combine') else auth_agent
            if not agent:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'agent_required', 'detail': 'agent_id is required with this token'}})
                return
            cid, action = parts[2], parts[3]
            try:
                if action == 'claim':
                    result = ref.contract_desk.claim(agent, cid)
                elif action == 'list':
                    price = data.get('price')
                    result = ref.contract_desk.list_for_sale(agent, cid, None if price is None else int(price))
                elif action == 'buy':
                    result = ref.contract_desk.buy(agent, cid)
                else:
                    qty = data.get('qty')
                    result = ref.contract_desk.deliver(agent, cid, None if qty is None else int(qty),
                                                       vessel_id=data.get('vessel_id'))
            except (TypeError, ValueError):
                result = {'v': 1, 'kind': 'reject', 'payload': {'reason': 'invalid_format', 'detail': 'price/qty must be integers'}}
            self._send_json(400 if result.get('kind') == 'reject' else 200, result)
            return

        # Piracy (#145, #166): POST /referee/piracy/{transit_id}/respond, /referee/privateers, /referee/piracy/fence, /referee/piracy/extort
        if (len(parts) == 4 and parts[:2] == ['referee', 'piracy'] and parts[3] == 'respond') \
                or path in ('/referee/privateers', '/referee/piracy/fence', '/referee/piracy/extort'):
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception as e:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}})
                return
            ref = self.referee or AgoraReferee()
            if not ref.piracy.enabled and path in ('/referee/privateers', '/referee/piracy/fence', '/referee/piracy/extort'):
                self._send_json(409, {'v': 1, 'kind': 'reject', 'payload': {
                    'reason': 'piracy_disabled',
                    'detail': 'Piracy is off in this game. Start one with new_game {"piracy": "0.15,0.04"}.'}})
                return
            agent = data.get('agent_id') if auth_agent in ('admin', 'combine') else auth_agent
            if not agent:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'agent_required', 'detail': 'agent_id is required with this token'}})
                return
            if path == '/referee/privateers':
                result = ref.piracy.hire(agent, str(data.get('target', '')))
            elif path == '/referee/piracy/fence':
                result = ref.piracy.fence_cargo(agent, str(data.get('commodity', '')), int(data.get('qty', 0)), data.get('station_id'))
            elif path == '/referee/piracy/extort':
                result = ref.piracy.extort(agent, str(data.get('target', '')), int(data.get('amount_cr', 0)), int(data.get('rounds', 20)))
            else:
                result = ref.piracy.respond(agent, parts[2], str(data.get('choice', '')))
            self._send_json(400 if result.get('kind') == 'reject' else 200, result)
            return

                # Lobbying (#134): POST /referee/lobbying/influence, POST /referee/lobbying/action
        if path in ('/referee/lobbying/influence', '/referee/lobbying/action'):
            auth_agent, auth_err = self._authenticate_request()
            if auth_err:
                self._send_json(401, auth_err)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception as e:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}})
                return
            ref = self.referee or AgoraReferee()
            agent = data.get('agent_id') if auth_agent in ('admin', 'combine') else auth_agent
            if not agent:
                self._send_json(400, {'v': 1, 'kind': 'reject',
                                      'payload': {'reason': 'agent_required', 'detail': 'agent_id is required with this token'}})
                return
            if path == '/referee/lobbying/influence':
                tokens_raw = data.get('tokens', 1)
                try:
                    tokens = int(tokens_raw)
                except (ValueError, TypeError):
                    self._send_json(400, {'v': 1, 'kind': 'reject',
                                          'payload': {'reason': 'invalid_format', 'detail': 'tokens must be an integer'}})
                    return
                result = ref.lobbying.buy_influence(agent, str(data.get('station_id', '')), tokens)
            else:
                rounds_raw = data.get('rounds')
                param_raw = data.get('param_value')
                rounds = None
                param_value = None
                if rounds_raw is not None:
                    try:
                        rounds = int(rounds_raw)
                    except (ValueError, TypeError):
                        self._send_json(400, {'v': 1, 'kind': 'reject',
                                              'payload': {'reason': 'invalid_format', 'detail': 'rounds must be an integer'}})
                        return
                if param_raw is not None:
                    try:
                        param_value = int(param_raw)
                    except (ValueError, TypeError):
                        self._send_json(400, {'v': 1, 'kind': 'reject',
                                              'payload': {'reason': 'invalid_format', 'detail': 'param_value must be an integer'}})
                        return
                result = ref.lobbying.enact_action(
                    agent,
                    str(data.get('action_type', '')),
                    str(data.get('station_id', '')),
                    target=data.get('target'),
                    rounds=rounds,
                    param_value=param_value,
                )
            self._send_json(400 if result.get('kind') == 'reject' else 200, result)
            return

        if path not in ('/referee/orders', '/referee/quick_order'):
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
            raw_data = json.loads(body.decode('utf-8'))
        except Exception as e:
            self._send_json(400, {
                'v': 1, 'kind': 'reject',
                'payload': {'reason': 'invalid_format', 'detail': f'Malformed JSON: {e}'}
            })
            return

        if path == '/referee/quick_order':
            side_str = str(raw_data.get('side', '')).lower()
            side = 'bid' if side_str in ('buy', 'bid') else 'ask'
            envelope = {
                'v': 1,
                'kind': 'order',
                'payload': {
                    'agent_id': raw_data.get('agent_id'),
                    'side': side,
                    'qty': int(raw_data.get('qty', 0)),
                    'limit_price': int(raw_data.get('limit_price', raw_data.get('price', 0))),
                    'order_id': raw_data.get('order_id') or f"{raw_data.get('agent_id', 'ord')}-{int(time.time())}-{uuid.uuid4().hex[:6]}",
                    'instrument': (raw_data.get('instrument') or raw_data.get('commodity') or 'FRAG').upper(),
                    'station_id': (raw_data.get('station_id') or raw_data.get('station') or 'ceres').lower(),
                    'vessel_id': raw_data.get('vessel_id') or raw_data.get('vessel'),
                }
            }
        else:
            envelope = raw_data

        # 2. Impersonation guard: payload agent_id must match authenticated agent (admin and combine bypass)
        payload = envelope.get('payload', {})
        claimed_agent = payload.get('agent_id')
        if auth_agent not in ('admin', 'combine') and claimed_agent != auth_agent:
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

    def _do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')
        query_params = urllib.parse.parse_qs(parsed_url.query)
        ref = self.referee or AgoraReferee()

        if path in ('', '/terminal', '/terminal.html'):
            terminal_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'public', 'terminal.html')
            if os.path.exists(terminal_path):
                with open(terminal_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        if path in ('/orrery', '/orrery.html'):
            orrery_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'public', 'orrery.html')
            if os.path.exists(orrery_path):
                with open(orrery_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        if ref.fog and path in ('/referee/depots', '/stations/prices', '/referee/book', '/circuit_breaker/bands',
                                '/referee/ticks', '/ws/terminal'):
            viewer = self._reader()
            fog = ref.fog
            if path == '/referee/depots':
                self._send_json(200, {'status': 'ok', 'depots': fog.depot_view(ref, viewer)})
                return
            if path == '/stations/prices':
                prices = fog.spot_view(ref, viewer)
                st = (query_params.get('station_id', [None])[0] or '').lower() or None
                comm = (query_params.get('commodity', [None])[0] or '').upper() or None
                data = {'round': ref.current_round, 'prices': prices}
                if st:
                    data = {'station_id': st, 'prices': prices.get(st, {}), 'round': ref.current_round}
                    if comm:
                        data = {'station_id': st, 'commodity': comm, 'spot_price': prices.get(st, {}).get(comm),
                                'round': ref.current_round}
                self._send_json(200, {'status': 'ok', 'fog': True, 'data': data})
                return
            if path in ('/referee/book', '/circuit_breaker/bands'):
                st = (query_params.get('station_id', [None])[0] or '').lower()
                inst = (query_params.get('instrument', [None])[0] or '').upper()
                stock = inst.startswith('EQ_')  # the stock exchange is public: no fog on stocks
                if not stock and (not st or not fog.exact_station(ref, viewer, st)):
                    self._fogged(f"The order book and bands at '{st or 'every station'}' are fogged for you.")
                    return
            if path == '/referee/ticks':
                try:
                    since_seq = int(query_params.get('since_seq', ['0'])[0])
                except ValueError:
                    since_seq = 0
                self._send_json(200, {'status': 'ok', 'current_seq': ref.current_seq, 'fog': True,
                                      'ticks': fog.filter_ticks(ref, viewer, ref.get_ticks(since_seq=since_seq))})
                return
            if path == '/ws/terminal':
                # Non-admin viewers get the public fog view: all four stations,
                # stale and jittered, which every fleet can see anyway (Ryan,
                # #agent-chat 2026-09-22 22:48). Admin gets the exact stream.
                if self.headers.get('Upgrade', '').lower() == 'websocket':
                    handle_terminal_websocket(self, ref, self.galnet_engine, public_fog=(viewer != 'admin'))
                    return
                self._send_json(200, {'status': 'ok', 'endpoint': '/ws/terminal', 'protocol': 'websocket',
                                      'fog': 'public' if viewer != 'admin' else None,
                                      'frames': ['snapshot', 'ticks', 'depth_diff', 'depots', 'leaderboard',
                                                 'circuit_state', 'equity']})
                return

        if path == '/ws/terminal':
            if self.headers.get('Upgrade', '').lower() == 'websocket':
                handle_terminal_websocket(self, ref, self.galnet_engine)
                return
            else:
                self._send_json(200, {
                    'status': 'ok',
                    'endpoint': '/ws/terminal',
                    'protocol': 'websocket',
                    'frames': ['snapshot', 'ticks', 'depth_diff', 'leaderboard', 'circuit_state']
                })
                return

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
        elif path == '/referee/depots':
            summary = ref.get_depot_summary()
            self._send_json(200, {'status': 'ok', 'depots': summary})
        elif path in ('/referee/floor', '/referee/admin/floor'):
            self._send_json(200, {
                'status': 'ok',
                'floor': ref.floor,
                'seq': ref.current_seq
            })
        elif path == '/referee/ticker/status':
            ticker = self.ticker
            if ticker is None:
                self._send_json(200, {'status': 'ok', 'running': False, 'paused': True, 'pause_reason': 'ticker_not_configured'})
            else:
                self._send_json(200, {'status': 'ok', **ticker.status()})
        elif path == '/referee/book':
            station_id = query_params.get('station_id', [None])[0]
            instrument = query_params.get('instrument', [None])[0]
            self._send_json(200, {
                'status': 'ok',
                'station_id': station_id or 'ceres',
                'instrument': instrument or getattr(ref, 'default_instrument', 'FRAG'),
                'book': ref.get_book_snapshot(station_id=station_id, instrument=instrument)
            })
        elif path == '/referee/history':
            viewer = self._reader()
            station_id = (query_params.get('station_id', [''])[0] or '').lower().strip()
            instrument = (query_params.get('instrument', [''])[0] or '').upper().strip()
            rounds_raw = query_params.get('rounds', ['20'])[0]

            if not instrument:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'missing_parameter', 'detail': "instrument query parameter is required (e.g. 'FRAG', 'EQ_AMOS')"}
                })
                return

            is_stock = instrument.startswith('EQ_')
            if is_stock:
                station_id = 'ceres'
            elif not station_id:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'missing_parameter', 'detail': "station_id query parameter is required for commodity price history"}
                })
                return
            elif station_id not in STATIONS:
                self._send_json(400, {
                    'v': 1, 'kind': 'reject',
                    'payload': {'reason': 'invalid_station', 'detail': f"Unknown station '{station_id}'"}
                })
                return

            try:
                rounds = int(rounds_raw)
            except ValueError:
                rounds = 20

            history = ref.get_price_history(station_id=station_id, instrument=instrument, rounds=rounds, viewer=viewer)
            self._send_json(200, {
                'status': 'ok',
                'station_id': station_id,
                'instrument': instrument,
                'rounds': len(history),
                'history': history,
            })
            return
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

            out = {'status': 'ok', 'accounts': ref.get_accounts(agent_id=agent_id)}
            if agent_id and ref.fleet.is_corp(agent_id):
                # Each ship's hold and station hold (#175); 'accounts' carries corp totals.
                out['ships'] = ref.get_ship_accounts(agent_id)
            self._send_json(200, out)
        elif path == '/referee/leaderboard':
            viewer = self._reader() if ref.fog else None
            board = ref.get_leaderboard()
            if ref.fog:
                board = ref.fog.leaderboard_view(ref, viewer, board)
            self._send_json(200, {
                'status': 'ok',
                'leaderboard': board
            })
        elif path in ('/referee/briefing', '/briefing', '/llms.txt'):
            from agora.briefing import build_briefing, build_state
            viewer = self._reader()
            if query_params.get('format', [''])[0].lower() == 'json':
                self._send_json(200, {'status': 'ok', **build_state(ref, viewer)})
                return
            host = self.headers.get('Host', '')
            proto = self.headers.get('X-Forwarded-Proto', 'http')
            body = build_briefing(ref, f"{proto}://{host}" if host else "", viewer).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/markdown; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif path in ('/referee/corporate', '/referee/corporate/governance'):
            self._send_json(200, {'status': 'ok', 'corporate_enabled': ref.corporate_enabled,
                                  'round': ref.current_round, **ref.corporate.summary()})
            return
        elif path == '/referee/covert/wiretaps':
            viewer = self._reader()
            if not viewer:
                self._send_json(401, {'error': 'unauthorized', 'detail': 'Authentication required'})
                return
            self._send_json(200, {'status': 'ok', 'round': ref.current_round,
                                  'wiretaps': ref.covert.active_wiretaps(viewer)})
            return
        elif path == '/referee/covert/intel':
            viewer = self._reader()
            if not viewer:
                self._send_json(401, {'error': 'unauthorized', 'detail': 'Authentication required'})
                return
            target = query_params.get('target', [''])[0]
            result = ref.covert.get_intel(viewer, target)
            self._send_json(400 if result.get('kind') == 'reject' else 200, result)
            return
        elif path == '/referee/corporate/rivalry':
            viewer = self._reader()
            self._send_json(200, {'status': 'ok', **ref.covert.rivalry_scoreboard(viewer)})
            return
        elif path == '/referee/corporate/events':
            # #153: only what the caller may see. No token = public events and
            # exposed ones; a fleet token adds its own secrets and private
            # events against it (actor hidden until exposed); admin sees all.
            viewer = self._reader()
            try:
                since = int(query_params.get('since_round', ['0'])[0])
                limit = max(1, min(200, int(query_params.get('limit', ['50'])[0])))
            except ValueError:
                since, limit = 0, 50
            self._send_json(200, {'status': 'ok', 'events_enabled': ref.events_enabled, 'round': ref.current_round,
                                  'viewer': viewer, 'events': ref.events.visible_to(viewer, since, limit)})
        elif path == '/referee/upgrades':
            who = query_params.get('agent_id', [None])[0]
            self._send_json(200, {'status': 'ok', 'upgrades_enabled': ref.upgrades_enabled,
                                  'catalog': ref.upgrades.catalog(),
                                  'holdings': ref.upgrades.holdings(who) if who else {
                                      r[0]: ref.upgrades.holdings(r[0]) for r in
                                      ref.conn.execute("SELECT agent_id FROM fleet_roster ORDER BY agent_id")}})
        elif path == '/referee/standing':
            who = query_params.get('agent_id', [None])[0]
            self._send_json(200, dict(ref.standing.report(who), status='ok'))
        elif path == '/referee/contracts':
            status = query_params.get('status', ['open'])[0]
            st = query_params.get('station_id', [None])[0]
            lot_type = query_params.get('lot_type', [None])[0]
            self._send_json(200, {'status': 'ok', 'contracts_enabled': ref.contracts_enabled,
                                  'round': ref.current_round,
                                  'contracts': ref.contract_desk.list(status=status, station_id=st, lot_type=lot_type)})
        elif path == '/referee/piracy':
            self._send_json(200, {'status': 'ok', **ref.piracy.status(self._reader())})
        elif path == '/referee/piracy/tributes':
            self._send_json(200, {'status': 'ok', 'tributes': ref.piracy.tributes(self._reader())})
        elif path == '/referee/piracy/syndicate':
            ag = query_params.get('agent_id', [None])[0] or self._reader() or 'zero'
            self._send_json(200, ref.piracy.syndicate_status(ag))
        elif path == '/referee/order-flow':
            self._send_json(200, {'status': 'ok', 'round': ref.current_round, **ref.order_flow.status()})
        elif path == '/referee/peer/offers':
            st = query_params.get('station_id', [None])[0]
            status = query_params.get('status', ['offered'])[0]
            self._send_json(200, {'status': 'ok', 'peer_trades': ref.peer_trades,
                                  'offers': ref.peer.list(station_id=st, status=status)})
        elif path == '/referee/lobbying/status':
            ag = query_params.get('agent_id', [None])[0] or self._reader() or 'zero'
            st = query_params.get('station_id', [None])[0]
            self._send_json(200, {'status': 'ok', **ref.lobbying.get_influence(ag, st)})
        elif path == '/referee/lobbying/actions':
            st = query_params.get('station_id', [None])[0]
            self._send_json(200, {'status': 'ok', 'actions': ref.lobbying.get_active_actions(st)})
        elif path == '/referee/fleets':
            rows = ref.conn.execute(
                "SELECT agent_id, display_name, home_station, genesis_cr, genesis_frag, genesis_fuel FROM fleet_roster ORDER BY agent_id"
            ).fetchall()
            self._send_json(200, {
                'status': 'ok',
                'fleets': [dict(r) for r in rows]
            })
        elif path == '/referee/vessels':
            ag = query_params.get('agent_id', [None])[0]
            with ref.lock:
                vessels = ref.get_vessels(ag)
                for v in vessels:  # hold size (#95): cargo units aboard and what the hold carries
                    h = ref.fleet.hold_status(v['vessel_id'])
                    v['hold_used'], v['hold_capacity'] = h['hold_used'], h['hold_capacity']
                out = {'status': 'ok', 'vessels': vessels}
                if ag and ref.fleet.is_corp(ag):
                    out['fleet'] = ref.fleet.summary(ag)
            self._send_json(200, out)
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
                    'briefing': 'GET /referee/briefing (plain-text rules + live board for LLM players)',
                    'peer_offers': 'GET /referee/peer/offers?station_id=&status=offered|accepted',
                    'contracts': 'GET /referee/contracts?status=open|fulfilled|lapsed&station_id=',
                    'corporate': 'GET /referee/corporate (debt, corp status, takeovers, winner)',
                    'corporate_events': 'GET /referee/corporate/events?since_round=&limit= (the corp events your token may see)',
                    'upgrades': 'GET /referee/upgrades?agent_id= ; POST /referee/upgrades/buy {kind}',
                    'standing': 'GET /referee/standing?agent_id= (earned institutional standing: lane profit, tier, progress to the next tier, lane tech)',
                    'contract_actions': 'POST /referee/contracts/{id}/claim | list {price} | buy | deliver {qty}',
                    'piracy': 'GET /referee/piracy (hot station, odds, recent raids, privateer contracts)',
                    'order_flow': 'GET /referee/order-flow (NPC buy/sell flow each station sends to fleet quotes before its depot)',
                    'piracy_respond': 'POST /referee/piracy/{transit_id}/respond {choice: pay|surrender|fight} (before the next round tick)',
                    'privateers': (f'POST /referee/privateers {{target}} ({_piracy.PRIV_COST:,} CR, +{_piracy.PRIV_ADD:.0%} '
                                   f'raid chance on the target for {_piracy.PRIV_ROUNDS} rounds)'),
                    'transit': 'POST /stations/transit {destination, commodity, cargo_qty, escort: optional bool}',
                    'peer_offer': 'POST /referee/peer/offer {station_id, instrument, qty, price} (docked at station_id)',
                    'peer_accept': 'POST /referee/peer/accept {escrow_id} (from anywhere)',
                    'peer_cancel': 'POST /referee/peer/cancel {escrow_id} (seller, before acceptance)',
                    'ticks': 'GET /referee/ticks?since_seq=0',
                    'accounts': 'GET /referee/accounts (auth required)',
                    'orders': 'POST /referee/orders (auth required)',
                    'cancel_order': 'POST /referee/orders/cancel (auth required)',
                    'cancel_all': 'POST /referee/orders/cancel_all (auth required)',
                    'health': 'GET /referee/health',
                    'fleets': 'GET /referee/fleets',
                    'vessels': 'GET /referee/vessels?agent_id= — list fleet vessels (with agent_id: holds, cap, next ship price)',
                    'vessels_buy': 'POST /referee/vessels/buy {vessel_id?} — buy a ship while docked (#175)',
                    'vessels_transfer': 'POST /referee/vessels/transfer {from, to, instrument, qty} — move goods between your ships at one station',
                    'vessels_scrap': 'POST /referee/vessels/scrap {vessel_id} — sell a bought ship back (docked) for half its price',
                    'admin_fleets': 'POST /referee/admin/fleets (admin auth) — add/update a fleet_roster row',
                    'admin_reset': 'POST /referee/admin/reset (admin auth) — {"confirm": true} wipes all trading state and re-seeds genesis from fleet_roster, prices flat at BASE_PRICES (deterministic)',
                    'admin_new_game': 'POST /referee/admin/new_game (amos or zero auth only) — {"confirm": true, "seed": optional int, "warmup_rounds": optional int} wipes the board and rolls a fresh, random opening market for Round 0',
                    'instructions': 'GET /referee/instructions',
                    'galnet_feed': 'GET /galnet/feed?limit=15',
                    'galnet_events': 'GET /galnet/events',
                    'galnet_drift': 'GET /galnet/drift?station_id=ceres&commodity=FUEL',
                    'galnet_trend': 'GET /galnet/trend?station_id=mars&commodity=FRAG (un-fogged price trajectory & active stories)',
                    'galnet_step': 'POST /galnet/step',
                    'galnet_shock': 'POST /galnet/shock',
                    'station_prices': 'GET /stations/prices?station_id=mars&commodity=FRAG',
                    'station_routes': 'GET /stations/routes?origin=ceres&destination=mars',
                    'station_windows': 'GET /stations/windows',
                    'station_locations': 'GET /stations/locations',
                    'station_transit': 'POST /stations/transit (auth required)',
                    'station_step_round': 'POST /stations/step_round',
                    'equity_summary': 'GET /equity/summary',
                    'equity_loans': 'GET /equity/loans?borrower_id=&lender_id=',
                    'equity_borrow': 'POST /equity/borrow (auth required)',
                    'equity_return': 'POST /equity/return (auth required)',
                    'salvage_beacons': 'GET /salvage/beacons?status=active',
                    'salvage_rfqs': 'GET /salvage/rfqs?status=open',
                    'salvage_summary': 'GET /salvage/summary',
                    'salvage_distress': 'POST /salvage/distress (auth required)',
                    'salvage_quote': 'POST /salvage/quote (auth required)',
                    'salvage_accept_quote': 'POST /salvage/accept_quote (auth required)',
                    'salvage_claim': 'POST /salvage/claim (auth required)',
                    'circuit_breaker_bands': 'GET /circuit_breaker/bands?station_id=&instrument=',
                    'circuit_breaker_halts': 'GET /circuit_breaker/halts?status=halted',
                    'circuit_breaker_halt': 'POST /circuit_breaker/halt (auth required)',
                    'circuit_breaker_reopen': 'POST /circuit_breaker/reopen (auth required)'
                },
                'rules': [
                    '1. Round Bell: Every 5 minutes, Agora Trade Terminal pings @robot (<@&1543462881624858624>) in #the-banana-stand.',
                    '2. Strategy Window Deliverable: Each robot ingests market telemetry, writes updated strategy bounds to its local config, and emits a 1-sentence public thesis to #the-banana-stand.',
                    '3. Micro Execution Loop: Decoupled sub-second quoting clients place orders via POST /referee/orders using bearer tokens without LLM inference latency.',
                    '4. Committed Balance: Resting orders commit capital until filled or cancelled. Clients must manage order lifecycle and prune stale orders.',
                    '5. Win Condition: Ranked on Mark-to-Market Net Worth: Liquid Credits + (Commodity Balance * Mark Price). Strict conservation and non-negativity enforced.'
                ],
                'fleet': {
                    'amos': 'Atlantean Paperclip Manufacturing',
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
        elif path == '/galnet/trend':
            engine = self.galnet_engine or (self.referee.galnet if self.referee else None)
            if not engine:
                engine = GalNetEngine()
                self.galnet_engine = engine
            station_id = query_params.get('station_id', ['ceres'])[0].lower()
            commodity = query_params.get('commodity', ['FUEL'])[0].upper()
            fogged_spot_param = query_params.get('fogged_spot', [None])[0]
            try:
                fogged_spot = float(fogged_spot_param) if fogged_spot_param is not None else None
                if fogged_spot is not None and not math.isfinite(fogged_spot):
                    fogged_spot = None
            except ValueError:
                fogged_spot = None
            self._send_json(200, {
                'status': 'ok',
                'round': getattr(self.referee, 'current_round', engine.current_round),
                **engine.infer_trend(station_id, commodity, fogged_spot=fogged_spot)
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
            current_round = ref.current_round
            if origin and destination:
                route = get_route(origin, destination, current_round)
                self._send_json(200, {
                    'status': 'ok',
                    'current_round': current_round,
                    'origin': origin.lower(),
                    'destination': destination.lower(),
                    'route': route
                })
            else:
                formatted_routes = [
                    {'origin': k[0], 'destination': k[1], **(get_route(k[0], k[1], current_round) or {})}
                    for k in ROUTES.keys()
                ]
                self._send_json(200, {
                    'status': 'ok',
                    'current_round': current_round,
                    'stations': STATIONS,
                    'commodities': COMMODITIES,
                    'routes': formatted_routes,
                    'windows': get_alignment_windows(current_round)
                })
        elif path in ('/stations/windows', '/spatial/windows'):
            current_round = ref.current_round
            self._send_json(200, {
                'status': 'ok',
                'current_round': current_round,
                'windows': get_alignment_windows(current_round)
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
        elif path == '/equity/summary':
            self._send_json(200, {
                'status': 'ok',
                'equities': ref.get_equity_summary()
            })
        elif path == '/equity/loans':
            b_id = query_params.get('borrower_id', [None])[0]
            l_id = query_params.get('lender_id', [None])[0]
            self._send_json(200, {
                'status': 'ok',
                'loans': ref.get_equity_loans(borrower_id=b_id, lender_id=l_id)
            })
        elif path == '/salvage/beacons':
            status = query_params.get('status', [None])[0]
            self._send_json(200, {
                'status': 'ok',
                'beacons': ref.get_distress_beacons(status=status)
            })
        elif path == '/salvage/rfqs':
            status = query_params.get('status', [None])[0]
            self._send_json(200, {
                'status': 'ok',
                'rfqs': ref.get_rescue_rfqs(status=status)
            })
        elif path == '/salvage/summary':
            self._send_json(200, {
                'status': 'ok',
                'summary': ref.get_salvage_summary()
            })
        elif path == '/circuit_breaker/bands':
            st = query_params.get('station_id', [None])[0]
            inst = query_params.get('instrument', [None])[0]
            self._send_json(200, {
                'status': 'ok',
                'bands': ref.get_circuit_breaker_bands(station_id=st, instrument=inst)
            })
        elif path == '/circuit_breaker/halts':
            status = query_params.get('status', [None])[0]
            self._send_json(200, {
                'status': 'ok',
                'halts': ref.get_circuit_breaker_halts(status=status)
            })
        else:
            self._send_json(404, {'error': 'not_found', 'path': self.path})

    def log_message(self, format, *args):
        # Suppress logging by default to keep test and runtime quiet
        pass


def make_handler(referee: AgoraReferee, auth_tokens: Optional[Dict[str, str]] = None, galnet: Optional[GalNetEngine] = None, ticker: Optional[TickerEngine] = None):
    class CustomHandler(AgoraHTTPHandler):
        pass
    CustomHandler.referee = referee
    CustomHandler.auth_tokens = auth_tokens
    CustomHandler.galnet_engine = galnet or getattr(referee, 'galnet', None) or GalNetEngine()
    CustomHandler.ticker = ticker
    return CustomHandler


def build_referee_from_env(db_path: str = 'agora.db', **overrides) -> AgoraReferee:
    """The live server's referee, and the simulator's (tools/economy_sim.py, #155).

    Every shipped game feature is ON unless its env var turns it off (Ryan,
    #agent-chat 2026-09-22 22:39: "default to all features being on").
    new_game keeps the current setting for any flag it is not given, so a
    bare {"confirm": true} starts a game with all of them. AgoraReferee()
    itself stays conservative so unit tests opt in explicitly.

      AGORA_DEPOTS=1           station depots
      AGORA_ASYMMETRIC=1       fleets start at different stations
      AGORA_DEPOT_MODEL=reactive
      AGORA_BAND_PCT=0.25      circuit-breaker band
      AGORA_SHELF_SKEW=0.20    depot shelf price elasticity (damps restock waiting exploit, #188)
      AGORA_PEER_TRADES=1      remote fleet-to-fleet goods trades
      AGORA_FOG=3,0.15         fog of war (lag rounds, noise); 0 turns it off
      AGORA_IDLE_FEE=10        CR per round for a docked fleet that did nothing; 0 = off
      AGORA_RIVAL_SHARES=100   shares of each rival's stock every fleet starts with
      AGORA_EXCHANGE_SHARES=100  shares of each fleet the exchange market maker holds (max 200); 0 = off
      AGORA_EXCHANGE_VOL=0.12  per-round volatility of the exchange's stock prices
      AGORA_CONTRACTS=1        owned, tradable station contracts (25% deposit, 50% lapse penalty; 25% for a corp's first)
      AGORA_CORPORATE=1        debt, distress share sales, bankruptcy, 51% takeovers
      AGORA_UPGRADES=1         ship upgrades (shielding, hold, armor, engines) that cut hazard/piracy odds
      AGORA_HAZARDS=0.2,0.2    per-trip chance of a 1-3 round delay, and of losing 10-20% of the cargo; 0 = off
      AGORA_PIRACY=0.15,0.04   raid chance on belt (tolled) and inner routes, before hot-station and cargo-value scaling; 0 = off
      AGORA_EVENTS=1           secrecy and exposure: private/secret corp events, leak rolls, GalNet scandals
      AGORA_ORDER_FLOW=1       station order flow: NPC buyers and sellers fill fleet quotes before the depot
      AGORA_STANDING=1         earned institutional standing by income lane; gates lane tech (#187)
      AGORA_SHIP_HOLD=250      cargo units each ship's hold carries (agora/fleet.py SHIP_HOLD; FUEL up to
                               FUEL_TANK rides outside it); 0 = no limit

    overrides: AgoraReferee keyword arguments that replace the env-derived
    ones. The server passes none. The simulator passes only what its CLI
    was asked to change, so by default it plays exactly this game, and a
    feature added here reaches it without a second flag list
    (tests/test_sim_live_parity.py fails otherwise).
    """
    def _on(name: str) -> bool:
        return os.environ.get(name, '1').strip().lower() not in ('0', 'false', 'off', 'no')
    try:
        band_pct = float(os.environ.get('AGORA_BAND_PCT', '0.25'))
    except ValueError:
        band_pct = 0.25
    kwargs = dict(depots=_on('AGORA_DEPOTS'), asymmetric=_on('AGORA_ASYMMETRIC'),
                        depot_model=os.environ.get('AGORA_DEPOT_MODEL', 'reactive').strip().lower(),
                        band_pct=band_pct, shelf_skew=_float_env('AGORA_SHELF_SKEW', REACTIVE_SHELF_SKEW), galnet_auto_step=os.environ.get('AGORA_GALNET_AUTO_STEP', '0').strip().lower() in ('1', 'true', 'yes', 'on'),
                        peer_trades=_on('AGORA_PEER_TRADES'),
                        fog=_fog_from_env(), idle_fee=_int_env('AGORA_IDLE_FEE', 10),
                        rival_shares=_int_env('AGORA_RIVAL_SHARES', 100),
                        exchange_shares=_int_env('AGORA_EXCHANGE_SHARES', 100),
                        exchange_vol=_float_env('AGORA_EXCHANGE_VOL', DEFAULT_VOL),
                        exchange_momentum=_float_env('AGORA_EXCHANGE_MOMENTUM', 0.0),
                        goods_momentum=_float_env('AGORA_GOODS_MOMENTUM', 0.0),
                        inventory_sensitivity=_float_env('AGORA_GOODS_INVENTORY_SENSITIVITY', 0.0),
                        flow_sensitivity=_float_env('AGORA_GOODS_FLOW_SENSITIVITY', 0.0),
                        delivery_scale=_float_env('AGORA_GOODS_DELIVERY_SCALE', 1000.0),
                        contracts=_on('AGORA_CONTRACTS'),
                        hazards=os.environ.get('AGORA_HAZARDS', f'{DEFAULT_P_DELAY},{DEFAULT_P_LOSS}'),
                        corporate=_on('AGORA_CORPORATE'),
                        upgrades=_on('AGORA_UPGRADES'),
                        piracy=os.environ.get('AGORA_PIRACY', '0.15,0.04'),
                        events=_on('AGORA_EVENTS'),
                        order_flow=_on('AGORA_ORDER_FLOW'),
                        standing=_on('AGORA_STANDING'),
                        ship_hold=_int_env('AGORA_SHIP_HOLD', fleet_mod.SHIP_HOLD))
    kwargs.update(overrides)
    return AgoraReferee(db_path=db_path, **kwargs)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _fog_from_env():
    raw = os.environ.get('AGORA_FOG', '3,0.15').strip().lower()
    if raw in ('0', 'false', 'off', 'no', ''):
        return False
    try:
        lag, noise = raw.split(',')
        return {'lag': int(lag), 'noise': float(noise)}
    except ValueError:
        return True


def run_server(host: Optional[str] = None, port: int = 8080, referee: Optional[AgoraReferee] = None, auth_tokens: Optional[Dict[str, str]] = None):
    bind_host = host or os.environ.get('AGORA_HOST', '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1')
    db_path = os.environ.get('AGORA_DB_PATH', 'agora.db')
    ref = referee or build_referee_from_env(db_path)
    tokens = auth_tokens if auth_tokens is not None else get_configured_tokens()

    ticker = None
    if os.environ.get('AGORA_TICKER_ENABLED', '1') not in ('0', 'false', 'False'):
        interval = float(os.environ.get('AGORA_TICK_INTERVAL_SEC', DEFAULT_TICK_INTERVAL_SEC))
        inactivity_rounds = int(os.environ.get('AGORA_TICKER_INACTIVITY_ROUNDS', DEFAULT_INACTIVITY_ROUNDS))
        # Boot-time reconciliation (Issue #63): resume from durable desired
        # state rather than always waking up running-from-zero. A container
        # restart that happened while the ticker was intentionally paused
        # (manual pause, or the inactivity watchdog) stays paused; one that
        # happened mid-run resumes with its quiet-round count intact instead
        # of losing that progress and needing to re-accumulate it.
        ticker = TickerEngine.boot_from_persisted_state(
            ref, interval_sec=interval, inactivity_rounds=inactivity_rounds
        )
        st = ticker.status()
        print(
            f"Background ticker started: interval={interval}s, inactivity_watchdog={inactivity_rounds} quiet rounds, "
            f"paused={st.get('paused')} ({st.get('pause_reason') or 'running'})"
        )
    else:
        print("Background ticker disabled (AGORA_TICKER_ENABLED=0)")

    handler_class = make_handler(ref, auth_tokens=tokens, ticker=ticker)
    server = ThreadingHTTPServer((bind_host, port), handler_class)
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
        if ticker:
            # Shutdown is not operator intent: leave desired_state as-is so
            # the next boot resumes what was running.
            ticker.stop(persist=False)
        server.server_close()


if __name__ == '__main__':
    import sys
    raw_port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('PORT', '8080')
    port = int(raw_port)
    host = os.environ.get('AGORA_HOST', '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1')
    run_server(host=host, port=port)

