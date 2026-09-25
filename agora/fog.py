"""
agora/fog.py - fog of war on market data (docs/fleet-market-spec.md section 3).

With fog on, a fleet sees exact prices only at the station where it is
docked. Every other station shows prices LAG rounds old, each jittered by
up to +/-NOISE. The jitter is deterministic per (game seed, viewer, round,
station, good, side), so a fleet re-fetching in the same round sees the
same numbers, and two fleets misread the same station differently.

Identity comes from the per-fleet bearer token (AGORA_TOKEN_<FLEET> or
AGORA_AUTH_TOKENS). The shared combine token and anonymous callers get the
public view: every station lagged and jittered, nothing exact. The admin
token sees everything.

Off unless new_game {"fog": {"lag": 3, "noise": 0.15}} or AGORA_FOG="3,0.15".
"""

import copy
import hashlib
import os
from typing import Any, Dict, List, Optional, Tuple

from agora.spatial import STATIONS

PUBLIC = None  # viewer id for anonymous / combine callers
ADMIN = 'admin'


def env_fog() -> Optional[Tuple[int, float]]:
    raw = os.environ.get("AGORA_FOG", "").strip()
    if not raw:
        return None
    try:
        lag, noise = raw.split(",")
        return parse_fog({"lag": lag, "noise": noise})
    except ValueError:
        return None


def parse_fog(v: Any) -> Optional[Tuple[int, float]]:
    """new_game's `fog` field: false/None -> off; true -> defaults;
    {"lag": n, "noise": x} -> those."""
    if not v:
        return None
    if v is True:
        return (3, 0.15)
    if isinstance(v, dict):
        lag = max(1, min(20, int(v.get("lag", 3))))
        noise = max(0.0, min(0.5, float(v.get("noise", 0.15))))
        return (lag, noise)
    return None


class FogEngine:
    def __init__(self, lag: int, noise: float, seed: int = 0):
        self.lag, self.noise, self.seed = lag, noise, seed
        # round -> {'depots': depot_summary['stations'], 'spots': {st: {comm: px}}}
        self.snapshots: Dict[int, Dict[str, Any]] = {}

    def record(self, ref) -> None:
        """Called at the end of each step_round (and at game start)."""
        self.snapshots[ref.current_round] = {
            'depots': copy.deepcopy(ref.get_depot_summary()['stations']),
            'spots': ref.spatial.get_prices() if ref.spatial else {},
        }
        for r in [r for r in self.snapshots if r < ref.current_round - self.lag]:
            del self.snapshots[r]

    def _old(self, current_round: int) -> Dict[str, Any]:
        want = current_round - self.lag
        rounds = sorted(self.snapshots)
        older = [r for r in rounds if r <= want]
        return self.snapshots[older[-1] if older else rounds[0]]

    def _jitter(self, viewer: Optional[str], rnd: int, st: str, comm: str, side: str, px: Optional[float]):
        if px is None or self.noise <= 0:
            return px
        h = hashlib.sha256(f"{self.seed}|{viewer}|{rnd}|{st}|{comm}|{side}".encode()).digest()
        u = int.from_bytes(h[:8], 'big') / 2 ** 64  # [0, 1)
        out = px * (1 + (2 * u - 1) * self.noise)
        return max(1, int(round(out))) if isinstance(px, int) else round(max(1.0, out), 2)

    @staticmethod
    def docked_at(ref, viewer: Optional[str]) -> Optional[str]:
        """Ship 1's station (the one the old one-ship API reports)."""
        if viewer in (PUBLIC, ADMIN):
            return None
        loc = ref.get_vessel_location(viewer)
        return loc['station_id'] if loc.get('status') == 'docked' else None

    @staticmethod
    def docked_everywhere(ref, viewer: Optional[str]) -> set:
        """Every station where one of the viewer's ships is docked: each
        sees its own station live (#175)."""
        if viewer in (PUBLIC, ADMIN):
            return set()
        here = set(ref.docked_stations(viewer)) if hasattr(ref, 'docked_stations') else set()
        one = FogEngine.docked_at(ref, viewer)
        return here | ({one} if one else set())

    def exact_station(self, ref, viewer: Optional[str], st: str) -> bool:
        if viewer == ADMIN:
            return True
        if viewer and getattr(ref, 'upgrades_enabled', False) and getattr(ref, 'upgrades', None):
            if ref.upgrades.has_telemetry(viewer):
                return True
        return st in self.docked_everywhere(ref, viewer)

    def depot_view(self, ref, viewer: Optional[str]) -> Dict[str, Any]:
        """Same shape as ref.get_depot_summary()."""
        live = ref.get_depot_summary()
        has_telemetry = bool(viewer and getattr(ref, 'upgrades_enabled', False) and
                             getattr(ref, 'upgrades', None) and ref.upgrades.has_telemetry(viewer))
        if viewer == ADMIN or has_telemetry or not self.snapshots:
            return live
        here, rnd = self.docked_at(ref, viewer), ref.current_round
        live_at = self.docked_everywhere(ref, viewer)
        old = self._old(rnd)['depots']
        out = dict(live)
        out['stations'] = {}
        out['fog'] = {'lag': self.lag, 'noise': self.noise, 'exact_station': here}
        for st in STATIONS:
            if st == here or st in live_at:
                out['stations'][st] = live['stations'][st]
                continue
            out['stations'][st] = {}
            for comm, q in old.get(st, {}).items():
                j = dict(q)
                for side in ('best_bid', 'best_ask', 'spot_price'):
                    j[side] = self._jitter(viewer, rnd, st, comm, side, j.get(side))
                if j.get('best_bid') and j.get('best_ask') and j['best_bid'] >= j['best_ask']:
                    j['best_ask'] = j['best_bid'] + 1
                j['bid_depth'] = j['ask_depth'] = None  # depth is not visible from afar
                out['stations'][st][comm] = j
        return out

    def spot_view(self, ref, viewer: Optional[str]) -> Dict[str, Dict[str, float]]:
        live = ref.spatial.get_prices()
        has_telemetry = bool(viewer and getattr(ref, 'upgrades_enabled', False) and
                             getattr(ref, 'upgrades', None) and ref.upgrades.has_telemetry(viewer))
        if viewer == ADMIN or has_telemetry or not self.snapshots:
            return live
        rnd = ref.current_round
        live_at = self.docked_everywhere(ref, viewer)
        old = self._old(rnd)['spots']
        return {st: (live[st] if st in live_at else
                     {c: self._jitter(viewer, rnd, st, c, 'spot_price', p) for c, p in old.get(st, {}).items()})
                for st in STATIONS}

    def leaderboard_view(self, ref, viewer: Optional[str], leaderboard: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """A fog-safe leaderboard view for viewer (#121). Admin sees exact
        marks; non-admin viewers get commodity_marks and mark_price computed
        from their fog view (exact where docked, lagged and jittered elsewhere).
        Rows the viewer cannot see exactly have net_worth recomputed at the
        jittered marks to prevent solving exact remote prices (#261)."""
        has_telemetry = bool(viewer and getattr(ref, 'upgrades_enabled', False) and
                             getattr(ref, 'upgrades', None) and ref.upgrades.has_telemetry(viewer))
        if viewer == ADMIN or has_telemetry or not self.snapshots:
            return leaderboard
        spots = self.spot_view(ref, viewer)
        live_at = self.docked_everywhere(ref, viewer)
        out = []
        for entry in leaderboard:
            e = dict(entry)
            st_id = e.get('station_id')
            if st_id and st_id in spots:
                marks = {
                    comm: int(round(spots[st_id].get(comm, 0)))
                    for comm in ('FRAG', 'FOOD', 'ORE', 'MACHINERY')
                    if comm in spots[st_id]
                }
                e['commodity_marks'] = marks
                if 'FRAG' in marks:
                    e['mark_price'] = marks['FRAG']

            # If the viewer cannot see this row's station live and it's not their own row,
            # recompute net_worth at the jittered marks to prevent solving exact remote prices (#261).
            if viewer != e.get('agent_id') and (st_id is None or st_id not in live_at):
                raw_marks = entry.get('commodity_marks', {})
                new_marks = e.get('commodity_marks', {})
                delta = 0
                qty_map = {
                    'FRAG': entry.get('frags', 0),
                    'FOOD': entry.get('food', 0),
                    'ORE': entry.get('ore', 0),
                    'MACHINERY': entry.get('machinery', 0),
                }
                for comm, qty in qty_map.items():
                    if qty and comm in raw_marks and comm in new_marks:
                        delta += qty * (new_marks[comm] - raw_marks[comm])
                if 'in_transit_cargo' in entry and isinstance(entry['in_transit_cargo'], dict):
                    for comm_raw, qty in entry['in_transit_cargo'].items():
                        comm = 'FRAG' if comm_raw in ('FRAG', 'BANANA') else comm_raw
                        if qty and comm in raw_marks and comm in new_marks:
                            delta += qty * (new_marks[comm] - raw_marks[comm])
                e['net_worth'] = entry.get('net_worth', 0) + delta

            out.append(e)
        out.sort(key=lambda x: x.get('net_worth', 0), reverse=True)
        return out

    def filter_ticks(self, ref, viewer: Optional[str], ticks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Trade prints carry exact prices, so a fleet sees prints only from
        the station it is docked at; the public sees none."""
        has_telemetry = bool(viewer and getattr(ref, 'upgrades_enabled', False) and
                             getattr(ref, 'upgrades', None) and ref.upgrades.has_telemetry(viewer))
        if viewer == ADMIN or has_telemetry:
            return ticks
        live_at = self.docked_everywhere(ref, viewer)
        out = []
        for t in ticks:
            p = t.get('payload')
            st = (p.get('station_id') or p.get('station')) if isinstance(p, dict) else None
            priced = isinstance(p, dict) and any(k in p for k in ('price', 'limit_price', 'fill_price', 'trades'))
            inst = (p.get('instrument') or '') if isinstance(p, dict) else ''
            if str(inst).startswith('EQ_'):
                priced = False  # the stock exchange is public: no fog on stocks
            if priced and (st is None or st not in live_at):
                continue
            out.append(t)
        return out
