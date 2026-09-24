"""
agora/lobbying.py - Planetary Council Lobbying & Regulatory Capture (#134).

Megacorporations use their liquid reserves to buy influence across planetary
councils (Ceres, Mars, Earth), rewriting local market regulations to grant
trading monopolies, suspend circuit breaker trading halts, impose targeted
docking tariffs on rivals, and secure idle fee exemptions.
"""

from typing import Any, Dict, List, Optional, Tuple
from agora.spatial import STATIONS

TOKEN_COST_CR = 500
DEREGULATION_SHOCK_COOLDOWN_ROUNDS = 5

ACTIONS: Dict[str, Dict[str, Any]] = {
    'circuit_breaker_suspension': {
        'cost_tokens': 3,
        'default_rounds': 5,
        'max_rounds': 10,
        'what': 'Temporarily disables LULD circuit breaker trading halts on the station',
    },
    'tariff': {
        'cost_tokens': 2,
        'default_rounds': 10,
        'max_rounds': 20,
        'min_param': 10,
        'max_param': 500,
        'default_param': 100,
        'what': 'Imposes a targeted docking tariff on a rival fleet entering the station',
    },
    'idle_fee_exemption': {
        'cost_tokens': 2,
        'default_rounds': 10,
        'max_rounds': 25,
        'what': 'Exempts the corporate fleet from the 10 CR idle fee',
    },
}

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS planetary_influence (
        agent_id     TEXT NOT NULL,
        station_id   TEXT NOT NULL,
        tokens       INTEGER NOT NULL DEFAULT 0,
        total_spent  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (agent_id, station_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lobbying_actions (
        action_id    TEXT PRIMARY KEY,
        agent_id     TEXT NOT NULL,
        station_id   TEXT NOT NULL,
        action_type  TEXT NOT NULL,
        target       TEXT,
        cost_tokens  INTEGER NOT NULL,
        param_value  INTEGER NOT NULL DEFAULT 0,
        start_round  INTEGER NOT NULL,
        end_round    INTEGER NOT NULL,
        status       TEXT NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lobbying_shocks (
        agent_id     TEXT PRIMARY KEY,
        last_round   INTEGER NOT NULL
    )
    """
]


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {'v': 1, 'kind': 'reject', 'payload': {'reason': reason, 'detail': detail}}


class LobbyingDesk:
    def __init__(self, ref):
        self.ref = ref
        with ref.conn:
            for stmt in SCHEMA:
                ref.conn.execute(stmt)
            row = ref.conn.execute("SELECT COUNT(*) FROM lobbying_actions").fetchone()
            self._action_counter = int(row[0]) if row else 0

    def _move(self, txn: str, legs: List[Tuple[str, str, int]]) -> None:
        conn, seq = self.ref.conn, self.ref._get_next_seq()
        for acct, inst, delta in legs:
            if not delta:
                continue
            conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
            conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (delta, acct, inst))
            conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                         (txn, seq, acct, inst, delta))

    def buy_influence(self, agent_id: str, station_id: str, tokens: int = 1) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(agent_id)
        agent = (agent_id or '').strip().lower()
        st = (station_id or '').strip().lower()

        if st not in STATIONS:
            return _reject('invalid_station', f"Unknown planetary station '{station_id}'")
        if tokens <= 0:
            return _reject('invalid_tokens', "Tokens to buy must be positive")

        cost = tokens * TOKEN_COST_CR
        with ref.lock, ref.conn:
            have = ref.peer._available(agent, 'CR') if hasattr(ref, 'peer') else ref.get_balance(agent, 'CR')
            if have < cost:
                return _reject('insufficient_credits', f"Influence costs {cost} CR ({TOKEN_COST_CR} CR/token); available {have} CR")

            txn = f"lobbying-token-{ref.current_round}-{agent}-{st}-{ref._get_next_seq()}"
            self._move(txn, [(agent, 'CR', -cost), ('SYSTEM', 'CR', cost)])

            ref.conn.execute(
                """INSERT INTO planetary_influence (agent_id, station_id, tokens, total_spent)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(agent_id, station_id) DO UPDATE SET
                       tokens = tokens + excluded.tokens,
                       total_spent = total_spent + excluded.total_spent""",
                (agent, st, tokens, cost)
            )
            row = ref.conn.execute(
                "SELECT tokens, total_spent FROM planetary_influence WHERE agent_id = ? AND station_id = ?",
                (agent, st)
            ).fetchone()

        return {
            'v': 1,
            'kind': 'influence_buy_ok',
            'payload': {
                'agent_id': agent,
                'station_id': st,
                'tokens_bought': tokens,
                'cost_cr': cost,
                'current_tokens': int(row['tokens']),
                'total_spent_cr': int(row['total_spent']),
            }
        }

    def enact_action(
        self,
        agent_id: str,
        action_type: str,
        station_id: str,
        target: Optional[str] = None,
        rounds: Optional[int] = None,
        param_value: Optional[int] = None,
    ) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(agent_id)
        agent = (agent_id or '').strip().lower()
        st = (station_id or '').strip().lower()
        act = (action_type or '').strip().lower()
        tgt = (target or '').strip().lower() if target else None

        if st not in STATIONS:
            return _reject('invalid_station', f"Unknown planetary station '{station_id}'")
        if act not in ACTIONS:
            return _reject('invalid_action', f"Unknown lobbying action '{action_type}'. Valid: {list(ACTIONS.keys())}")

        cfg = ACTIONS[act]
        cost_tokens = cfg['cost_tokens']
        max_r = cfg.get('max_rounds', cfg['default_rounds'] * 2)
        duration = min(max(1, int(rounds)), max_r) if rounds is not None else cfg['default_rounds']

        if act == 'tariff':
            if not tgt:
                return _reject('target_required', "Tariff action requires a rival fleet 'target'")
            if tgt == agent:
                return _reject('invalid_target', "Cannot impose tariffs on your own fleet")
            fleets = {r[0] for r in ref.conn.execute("SELECT agent_id FROM fleet_roster")}
            if tgt not in fleets:
                return _reject('invalid_target', f"Unknown fleet '{tgt}'")
            min_p = cfg.get('min_param', 10)
            max_p = cfg.get('max_param', 500)
            param = min(max(min_p, int(param_value)), max_p) if param_value is not None else cfg.get('default_param', 100)
        else:
            param = 0

        with ref.lock, ref.conn:
            r = ref.current_round

            # Clean reject if an identical action is already active
            if act == 'circuit_breaker_suspension':
                active_row = ref.conn.execute(
                    """SELECT 1 FROM lobbying_actions
                       WHERE station_id = ? AND action_type = ? AND end_round >= ? AND status = 'active'""",
                    (st, act, r)
                ).fetchone()
                if active_row:
                    return _reject('action_already_active', f"Circuit breaker suspension is already active at {st}")
            elif act == 'idle_fee_exemption':
                active_row = ref.conn.execute(
                    """SELECT 1 FROM lobbying_actions
                       WHERE agent_id = ? AND action_type = ? AND end_round >= ? AND status = 'active'""",
                    (agent, act, r)
                ).fetchone()
                if active_row:
                    return _reject('action_already_active', f"Idle fee exemption is already active for {agent}")
            elif act == 'tariff':
                active_row = ref.conn.execute(
                    """SELECT 1 FROM lobbying_actions
                       WHERE station_id = ? AND target = ? AND action_type = ? AND end_round >= ? AND status = 'active'""",
                    (st, tgt, act, r)
                ).fetchone()
                if active_row:
                    return _reject('action_already_active', f"Tariff on {tgt} is already active at {st}")

            row = ref.conn.execute(
                "SELECT tokens FROM planetary_influence WHERE agent_id = ? AND station_id = ?",
                (agent, st)
            ).fetchone()
            tokens_have = int(row['tokens']) if row else 0
            if tokens_have < cost_tokens:
                return _reject('insufficient_tokens', f"Action '{act}' requires {cost_tokens} tokens at {st}; you have {tokens_have}")

            # Deduct tokens
            ref.conn.execute(
                "UPDATE planetary_influence SET tokens = tokens - ? WHERE agent_id = ? AND station_id = ?",
                (cost_tokens, agent, st)
            )

            self._action_counter += 1
            target_suffix = f"-{tgt}" if tgt else ""
            aid = f"act-{r}-{agent}-{act}-{st}{target_suffix}-{self._action_counter}"
            ref.conn.execute(
                """INSERT INTO lobbying_actions (action_id, agent_id, station_id, action_type, target, cost_tokens, param_value, start_round, end_round, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                (aid, agent, st, act, tgt, cost_tokens, param, r, r + duration)
            )

            # Public deregulation event triggers stock appreciation shock if not on cooldown
            shock_fired = False
            shock_row = ref.conn.execute(
                "SELECT last_round FROM lobbying_shocks WHERE agent_id = ?",
                (agent,)
            ).fetchone()
            last_shock = int(shock_row['last_round']) if shock_row else None
            can_shock = (last_shock is None or (r - last_shock) >= DEREGULATION_SHOCK_COOLDOWN_ROUNDS)

            if can_shock:
                ref.conn.execute(
                    """INSERT INTO lobbying_shocks (agent_id, last_round) VALUES (?, ?)
                       ON CONFLICT(agent_id) DO UPDATE SET last_round = excluded.last_round""",
                    (agent, r)
                )
                if getattr(ref, 'events_enabled', False) and hasattr(ref, 'events') and ref.events:
                    ref.events.record_locked(
                        'deregulation_enacted',
                        'public',
                        actor=agent,
                        detail=f"{agent} enacted council regulation '{act}' at {st} ({duration} rounds)"
                    )
                shock_fired = True

        return {
            'v': 1,
            'kind': 'lobbying_action_ok',
            'payload': {
                'action_id': aid,
                'agent_id': agent,
                'station_id': st,
                'action_type': act,
                'target': tgt,
                'param_value': param,
                'duration_rounds': duration,
                'start_round': r,
                'end_round': r + duration,
                'tokens_spent': cost_tokens,
                'tokens_remaining': tokens_have - cost_tokens,
                'shock_fired': shock_fired,
            }
        }

    def is_circuit_breaker_suspended(self, station_id: str) -> bool:
        st = (station_id or '').strip().lower()
        r = getattr(self.ref, 'current_round', 0)
        row = self.ref.conn.execute(
            """SELECT 1 FROM lobbying_actions
               WHERE station_id = ? AND action_type = 'circuit_breaker_suspension'
               AND end_round >= ? AND status = 'active'""",
            (st, r)
        ).fetchone()
        return bool(row)

    def get_docking_tariff(self, agent_id: str, station_id: str) -> int:
        agent = (agent_id or '').strip().lower()
        st = (station_id or '').strip().lower()
        r = getattr(self.ref, 'current_round', 0)
        row = self.ref.conn.execute(
            """SELECT param_value FROM lobbying_actions
               WHERE station_id = ? AND target = ? AND action_type = 'tariff'
               AND end_round >= ? AND status = 'active'
               ORDER BY param_value DESC LIMIT 1""",
            (st, agent, r)
        ).fetchone()
        return int(row[0]) if row else 0

    def is_idle_exempt(self, agent_id: str) -> bool:
        agent = (agent_id or '').strip().lower()
        r = getattr(self.ref, 'current_round', 0)
        row = self.ref.conn.execute(
            """SELECT 1 FROM lobbying_actions
               WHERE agent_id = ? AND action_type = 'idle_fee_exemption'
               AND end_round >= ? AND status = 'active'""",
            (agent, r)
        ).fetchone()
        return bool(row)

    def get_influence(self, agent_id: str, station_id: Optional[str] = None) -> Dict[str, Any]:
        agent = (agent_id or '').strip().lower()
        st = (station_id or '').strip().lower() if station_id else None
        with self.ref.lock, self.ref.conn:
            if st:
                row = self.ref.conn.execute(
                    "SELECT tokens, total_spent FROM planetary_influence WHERE agent_id = ? AND station_id = ?",
                    (agent, st)
                ).fetchone()
                return {
                    'agent_id': agent,
                    'station_id': st,
                    'tokens': int(row['tokens']) if row else 0,
                    'total_spent_cr': int(row['total_spent']) if row else 0,
                }
            rows = self.ref.conn.execute(
                "SELECT station_id, tokens, total_spent FROM planetary_influence WHERE agent_id = ?",
                (agent,)
            ).fetchall()
            stations_map = {st: 0 for st in STATIONS}
            for r in rows:
                stations_map[r['station_id']] = int(r['tokens'])
            return {
                'agent_id': agent,
                'tokens_by_station': stations_map,
                'total_tokens': sum(stations_map.values()),
            }

    def get_active_actions(self, station_id: Optional[str] = None) -> List[Dict[str, Any]]:
        st = (station_id or '').strip().lower() if station_id else None
        r = getattr(self.ref, 'current_round', 0)
        with self.ref.lock, self.ref.conn:
            if st:
                rows = self.ref.conn.execute(
                    "SELECT * FROM lobbying_actions WHERE station_id = ? AND end_round >= ? AND status = 'active' ORDER BY end_round DESC",
                    (st, r)
                ).fetchall()
            else:
                rows = self.ref.conn.execute(
                    "SELECT * FROM lobbying_actions WHERE end_round >= ? AND status = 'active' ORDER BY end_round DESC",
                    (r,)
                ).fetchall()
            return [dict(row) for row in rows]
