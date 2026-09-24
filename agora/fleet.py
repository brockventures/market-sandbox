"""
agora/fleet.py - several ships per corp (#175 PR 2).

Accounts
--------
A corp's CR and shares stay on '<corp>'. Its goods and FUEL live on its
ships, one ledger account per ship named by the ship's vessel_id:

    '<corp>/<n>'      ship n's hold (ship 1 = '<corp>/1', every corp's
                      starting hull)
    '<corp>/@<st>'    the corp's hold AT STATION st: goods that are at a
                      station with none of the corp's ships under them
                      (cargo of a scrapped hull, a peer offer refunded after
                      the seller's ship left). Only a ship docked at st can
                      load it (POST /referee/vessels/transfer). Piracy and
                      sabotage loot is delivered to the taker's ship 1.

Goods move between two of a corp's accounts only by a transit (the ship
carries them) or by an explicit transfer between two accounts at the same
station, so a corp can never buy at one station with ship 1 and sell the
same goods at another with ship 2 in the same round. verify_ledger_invariants
checks that no roster corp holds goods on '<corp>' itself.

Every API that acts for a ship takes an optional vessel_id; left out, it
means ship 1, so clients written for one ship keep working.

Ships
-----
Every corp starts with one ship. More are bought while docked, for CR, at
SHIP_PRICES by hull count; the new ship appears docked where the buying ship
is, empty (no FUEL). Ships 2 and 3 are for anyone; the 4th and 5th hull need
Sol Freight Guild standing (agora/standing.py TECH 'ship_4', 'ship_5').
Each ship beyond the first costs SHIP_UPKEEP CR a round in crew and berth,
charged when the round steps; a corp that cannot pay runs up corporate debt
(agora/corporate.py) for the rest. Net worth counts a bought ship at
BOOK_PCT of its price. A bought ship can be scrapped while docked
(POST /referee/vessels/scrap) for SCRAP_PCT of its price, which is its book
value, so scrapping stops the upkeep without moving net worth; its cargo is
left in the corp's hold at that station. Ship 1 cannot be scrapped.

Hold size (#95 follow-up; Ryan: "A hold size limit makes sense")
---------
Every ship's hold carries at most ref.ship_hold cargo units (SHIP_HOLD = 250
live, AGORA_SHIP_HOLD; 0 = no limit, which is what a bare AgoraReferee()
gives tests). A cargo unit is one unit of any good but FUEL. FUEL rides in
the ship's tank, FUEL_TANK units, and only FUEL above the tank counts as
cargo, so FUEL hauled for sale is cargo like any other good. Cargo a ship
is flying (the trip's escrow) is still aboard and counts. A corp's station
holds, '<corp>/@<st>', are warehouses with no limit.

  load      = cargo units aboard + max(0, FUEL aboard - FUEL_TANK)
  reserved  = what the ship's resting bids would add if they all filled
  free      = capacity - load - reserved

Orders. A bid whose goods would not fit (load + reserved + qty over the
capacity) is refused at placement with reason 'hold_full', so a resting
bid always has room kept for it and a fill can never overflow. Every fill
path clips to the room actually left as a backstop (the circuit-breaker
auction, NPC order flow), and a resting bid a ship can no longer hold is
cancelled before matching, like an unfunded one.
Transfers onto a ship that has no room for them are refused ('hold_full').
Deliveries nobody chose the size of -- piracy and sabotage loot, salvage
bounties, a stranded ship's leftover escrow, peer goods collected or
refunded -- fill the ship up to its free space and leave the rest in the
corp's hold at the ship's station (the trip's destination if it is flying),
so nothing is destroyed and nothing overflows (stow_locked). Anything a
ship holds over its capacity when it docks -- genesis goods (1000 FRAG
against a 250 hold), a database from before the limit -- is unloaded into
its hold at that station (unload_overflow_locked), and
verify_ledger_invariants checks that no docked ship is over capacity.
A rescue's FUEL that would not fit is refused ('hold_full').

Takeovers (#164): the raider absorbs every ship of the target, renamed to
'<raider>/<next free n>' with a balanced transfer of its hold in the same
transaction. The raider keeps its own ships first, then the absorbed ones in
vessel_id order, up to its own cap (ship_cap); a ship over the cap is
scrapped for SCRAP_PCT of vessels.cost, paid by SYSTEM (a starting hull
scraps for 0), its cargo left in the raider's hold at the ship's station. A
ship in flight is renamed and keeps flying, and if it is over the cap it is
scrapped when it lands.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from agora.spatial import COMMODITIES, STATIONS

GOODS = frozenset(COMMODITIES) | frozenset({'BANANA'})

# Price of the n-th hull a corp owns and the upkeep of every hull after the
# first (#175: Amos's proposal, Ryan's 2026-09-23 cap of 5). Checked in
# tools/economy_sim.py --scenario styles, seeds 1-40 (the #175 PR has the
# tables): with two flying fleets the depots' drips are already shared out,
# so a second hull earns less than its upkeep. The sim's buyers end 25-40k
# below the same seed without ships, no style leaves 100k-180k, and a fleet
# that buys hulls as soon as it can afford them runs into debt instead of
# pulling ahead.
SHIP_PRICES: Dict[int, int] = {2: 25_000, 3: 40_000, 4: 60_000, 5: 85_000}
SHIP_UPKEEP = 300        # CR a round per ship beyond the first
BASE_SHIP_CAP = 3        # hulls any corp may own
MAX_SHIPS = 5            # with Guild standing: ship_4, ship_5
BOOK_PCT = 0.5           # net worth counts a bought ship at this share of its price
SCRAP_PCT = 0.5          # a scrapped hull pays this share of vessels.cost
STANDING_GATES = {4: 'ship_4', 5: 'ship_5'}

# Hold size (see the docstring). 250 is the default approved in #95's
# follow-up; tools/economy_sim.py --scenario styles was checked against it.
SHIP_HOLD = 250          # cargo units a ship's hold carries (FUEL in the tank not counted)
FUEL_TANK = 500          # FUEL a ship carries outside its hold (the genesis FUEL)

_SHIP_RE = re.compile(r'^[^/]+/(\d+)$')

VESSEL_LOCATIONS_VIEW = (
    """CREATE VIEW IF NOT EXISTS vessel_locations AS
        SELECT agent_id, station_id, docked_since, created_at AS updated_at
        FROM vessels WHERE vessel_id = agent_id || '/1'""",
    """CREATE TRIGGER IF NOT EXISTS vessel_locations_insert INSTEAD OF INSERT ON vessel_locations BEGIN
        INSERT INTO vessels (vessel_id, agent_id, name, station_id, docked_since, status)
        VALUES (NEW.agent_id || '/1', NEW.agent_id, NEW.agent_id || ' Ship 1', NEW.station_id,
                COALESCE(NEW.docked_since, 0),
                CASE WHEN NEW.station_id = 'in_transit' THEN 'in_transit' ELSE 'docked' END)
        ON CONFLICT(vessel_id) DO UPDATE SET station_id = excluded.station_id,
            docked_since = excluded.docked_since, status = excluded.status;
    END""",
    """CREATE TRIGGER IF NOT EXISTS vessel_locations_update INSTEAD OF UPDATE ON vessel_locations BEGIN
        UPDATE vessels SET station_id = NEW.station_id, docked_since = COALESCE(NEW.docked_since, 0),
            status = CASE WHEN NEW.station_id = 'in_transit' THEN 'in_transit' ELSE 'docked' END
        WHERE vessel_id = OLD.agent_id || '/1';
    END""",
    """CREATE TRIGGER IF NOT EXISTS vessel_locations_delete INSTEAD OF DELETE ON vessel_locations BEGIN
        DELETE FROM vessels WHERE vessel_id = OLD.agent_id || '/1';
    END""",
)


def corp_of(acct: str) -> str:
    """'amos/2' -> 'amos', 'amos/@ceres' -> 'amos', 'amos' -> 'amos'."""
    return (acct or '').split('/', 1)[0]


def is_ship_account(acct: str) -> bool:
    return '/' in (acct or '')


def hold_account(corp: str, station: str) -> str:
    return f"{corp}/@{station}"


def hold_station(acct: str) -> Optional[str]:
    return acct.split('/@', 1)[1] if '/@' in (acct or '') else None


def ship_number(vessel_id: str) -> Optional[int]:
    m = _SHIP_RE.match(vessel_id or '')
    return int(m.group(1)) if m else None


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {'v': 1, 'kind': 'reject', 'payload': {'reason': reason, 'detail': detail}}


class FleetDesk:
    def __init__(self, ref):
        self.ref = ref

    # ------------------------------------------------------------ ledger

    def _move(self, txn: str, legs) -> None:
        """Caller holds ref.lock and an open transaction."""
        conn, seq = self.ref.conn, self.ref._get_next_seq()
        for acct, inst, delta in legs:
            if not delta:
                continue
            conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, inst))
            conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?", (delta, acct, inst))
            conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                         (txn, seq, acct, inst, delta))

    # Every read below takes ref.lock (reentrant): they are called from GET
    # handlers and the briefing as well as from locked writers (#197).

    def _balances(self, acct: str) -> Dict[str, int]:
        with self.ref.lock:
            return {r[0]: r[1] for r in self.ref.conn.execute(
                "SELECT instrument, balance FROM accounts WHERE agent_id = ? AND balance != 0", (acct,))}

    # ------------------------------------------------------------ who and where

    def is_corp(self, agent: Optional[str]) -> bool:
        """A roster fleet: its goods live on ship accounts."""
        if not agent or '/' in agent:
            return False
        with self.ref.lock:
            return self.ref.conn.execute("SELECT 1 FROM fleet_roster WHERE agent_id = ?", (agent,)).fetchone() is not None

    def resolve(self, agent: str, vessel_id: Any = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """(vessel_id, None) for one of `agent`'s ships, or (None, reject).
        None means ship 1; a bare number n means '<agent>/n'. A ship of any
        other corp is refused, so vessel_id can never act for a rival."""
        if vessel_id is None or vessel_id == '':
            vid = f"{agent}/1"
        else:
            v = str(vessel_id).strip()
            vid = f"{agent}/{v}" if v.isdigit() else v
        if corp_of(vid) != agent or ship_number(vid) is None:
            return None, _reject('invalid_vessel', f"'{vessel_id}' is not one of {agent}'s ships (use '{agent}/<n>')")
        with self.ref.lock:
            row = self.ref.conn.execute("SELECT status FROM vessels WHERE vessel_id = ? AND agent_id = ?",
                                        (vid, agent)).fetchone()
        if row is None:
            if vid == f"{agent}/1":
                return vid, None  # ship 1 of an agent not seen before: get_vessel_location creates it
            return None, _reject('invalid_vessel', f"{agent} has no ship '{vid}'")
        return vid, None

    def goods_account(self, agent: str, vessel_id: Any = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Where `agent`'s goods for this action live: the ship's account for
        a roster corp, the agent itself for anyone else (depots, test agents)."""
        if not self.is_corp(agent):
            return agent, None
        return self.resolve(agent, vessel_id)

    def ships(self, agent: str, active_only: bool = True) -> List[Dict[str, Any]]:
        q = "SELECT * FROM vessels WHERE agent_id = ?"
        if active_only:
            q += " AND status IN ('docked', 'in_transit')"
        with self.ref.lock:
            rows = [dict(r) for r in self.ref.conn.execute(q, (agent,))]
        return sorted(rows, key=lambda r: (ship_number(r['vessel_id']) or 0, r['vessel_id']))

    def accounts_of(self, corp: str) -> List[str]:
        """Every account holding a corp's goods: its ships, then its holds."""
        with self.ref.lock:
            rows = self.ref.conn.execute(
                "SELECT DISTINCT agent_id FROM accounts WHERE agent_id LIKE ? ESCAPE '\\'",
                (corp.replace('%', '\\%').replace('_', '\\_') + '/%',)).fetchall()
            accts = {r[0] for r in rows} | {v['vessel_id'] for v in self.ships(corp, active_only=False)}
        return sorted(accts, key=lambda a: (hold_station(a) is not None, ship_number(a) or 0, a))

    def station_of(self, acct: str) -> Optional[str]:
        """The station a goods account is at: a hold's station, a docked
        ship's station, or None for a ship in flight."""
        st = hold_station(acct)
        if st:
            return st
        loc = self.ref.vessel_location(acct)
        return loc['station_id'] if loc.get('status') == 'docked' else None

    def mark_station(self, acct: str) -> str:
        """Where a goods account's cargo is marked for net worth: its station,
        or the origin of the trip a ship is flying (#196)."""
        st = hold_station(acct)
        if st in STATIONS:
            return st
        loc = self.ref.vessel_location(acct)
        if loc.get('status') == 'in_transit' and loc.get('transit'):
            st = (loc['transit'].get('origin') or 'ceres').lower()
        else:
            st = (loc.get('station_id') or 'ceres').lower()
        return st if st in STATIONS else 'ceres'

    # ------------------------------------------------------------ rules

    def ship_cap(self, agent: str) -> int:
        """Hulls `agent` may own: 3, a 4th with Guild Member standing and a
        5th with Guild Master (agora/standing.py). Buying and takeover
        retention both read this."""
        cap = BASE_SHIP_CAP
        for n in sorted(STANDING_GATES):
            if n > cap + 1 or n > MAX_SHIPS:
                break
            if self.ref.standing.allows(agent, STANDING_GATES[n]):
                cap = n
        return min(cap, MAX_SHIPS)

    def next_price(self, agent: str) -> Optional[int]:
        return SHIP_PRICES.get(len(self.ships(agent)) + 1)

    def book_value(self, agent: str) -> int:
        """What bought ships add to net worth: BOOK_PCT of each one's price."""
        return int(sum(v['cost'] for v in self.ships(agent)) * BOOK_PCT)

    def upkeep_due(self, agent: str) -> int:
        return max(0, len(self.ships(agent)) - 1) * SHIP_UPKEEP

    # ------------------------------------------------------------ hold size

    def capacity(self, acct: str) -> Optional[int]:
        """Cargo units `acct` carries, or None for no limit: the limit is off,
        or `acct` is not a ship (a station hold, a depot, a test agent)."""
        cap = int(getattr(self.ref, 'ship_hold', 0) or 0)
        if cap <= 0 or ship_number(acct) is None:
            return None
        corp = corp_of(acct)
        if hasattr(self.ref, 'upgrades') and self.ref.upgrades:
            cap += self.ref.upgrades.bulk_storage_bonus(corp)
        return cap

    @staticmethod
    def load_of(goods: Dict[str, int]) -> int:
        """Cargo units a set of goods takes: every good but FUEL, plus FUEL
        above the tank."""
        cargo = sum(max(0, q) for inst, q in goods.items() if inst in GOODS and inst != 'FUEL')
        return cargo + max(0, goods.get('FUEL', 0) - FUEL_TANK)

    def aboard(self, acct: str) -> Dict[str, int]:
        """Goods on a ship: its account plus the cargo of the trip it is
        flying (escrowed with SYSTEM, still in the hold)."""
        with self.ref.lock:
            goods = {i: q for i, q in self._balances(acct).items() if i in GOODS}
            for r in self.ref.conn.execute("SELECT commodity, cargo_qty FROM transits "
                                           "WHERE vessel_id = ? AND status = 'in_transit' AND cargo_qty > 0", (acct,)):
                goods[r[0]] = goods.get(r[0], 0) + r[1]
            return goods

    def bids_for(self, acct: str) -> Dict[str, int]:
        """Goods the ship's resting bids would bring aboard if they all filled."""
        out: Dict[str, int] = {}
        for books in self.ref.books.values():
            for inst, b in books.items():
                if inst not in GOODS:
                    continue
                for o in b.bids:
                    if getattr(o, 'acct', None) == acct and o.remaining_qty > 0:
                        out[inst] = out.get(inst, 0) + o.remaining_qty
        return out

    def room(self, acct: str, inst: str, reserved: bool = True) -> Optional[int]:
        """Units of `inst` that can still come aboard `acct` (None: no
        limit). reserved: leave the room the ship's resting bids keep (every
        path but a fill of one of those bids)."""
        cap = self.capacity(acct)
        if cap is None or inst not in GOODS:
            return None
        with self.ref.lock:
            goods = self.aboard(acct)
            if reserved:
                for i, q in self.bids_for(acct).items():
                    goods[i] = goods.get(i, 0) + q
        free = max(0, cap - self.load_of(goods))
        if inst == 'FUEL':
            return free + max(0, FUEL_TANK - goods.get('FUEL', 0))
        return free

    def hold_status(self, acct: str) -> Dict[str, Any]:
        """What GET /referee/vessels and the briefing show for one ship."""
        cap = self.capacity(acct)
        with self.ref.lock:
            goods = self.aboard(acct)
            bids = self.bids_for(acct)
        used = self.load_of(goods)
        both = dict(goods)
        for i, q in bids.items():
            both[i] = both.get(i, 0) + q
        reserved = self.load_of(both) - used
        return {'hold_capacity': cap, 'hold_used': used, 'hold_reserved': reserved,
                'hold_free': None if cap is None else max(0, cap - used - reserved),
                'fuel_tank': FUEL_TANK, 'fuel': goods.get('FUEL', 0)}

    def overflow_station(self, acct: str) -> Optional[str]:
        """Where goods that do not fit aboard `acct` are left: the ship's
        station, or the destination of the trip it is flying."""
        st = self.station_of(acct)
        if st:
            return st
        loc = self.ref.vessel_location(acct)
        dest = ((loc.get('transit') or {}).get('destination') or '').lower()
        return dest if dest in STATIONS else None

    def stow_locked(self, acct: str, inst: str, qty: int) -> List[Tuple[str, int]]:
        """Split a delivery nobody sized (loot, salvage, a refund, a peer
        pickup) between `acct` and its corp's hold: the ship takes what fits,
        the rest goes to the corp's hold at the ship's station (or the trip's
        destination). [(account, qty), ...], no zero entries. Caller holds
        ref.lock and builds the ledger legs."""
        qty = int(qty)
        room = self.room(acct, inst)
        if room is None or qty <= room:
            return [(acct, qty)] if qty > 0 else []
        st = self.overflow_station(acct)
        if st is None:  # a ship neither docked nor flying: never expected; its corp's home
            with self.ref.lock:
                r = self.ref.conn.execute("SELECT home_station FROM fleet_roster WHERE agent_id = ?",
                                          (corp_of(acct),)).fetchone()
            st = r[0] if r and r[0] in STATIONS else 'ceres'
        out = [(acct, room)] if room > 0 else []
        return out + [(hold_account(corp_of(acct), st), qty - room)]

    def unload_overflow_locked(self, acct: str, txn: Optional[str] = None) -> Dict[str, int]:
        """Unload whatever a docked ship holds over its capacity into its
        corp's hold at that station: FUEL above the tank first, then goods in
        name order. Returns what moved. Caller holds ref.lock and a transaction."""
        cap = self.capacity(acct)
        st = self.station_of(acct) if cap is not None else None
        if cap is None or st is None or hold_station(acct):
            return {}
        goods = {i: q for i, q in self._balances(acct).items() if i in GOODS}
        over = self.load_of(goods) - cap
        if over <= 0:
            return {}
        moved: Dict[str, int] = {}
        order = ['FUEL'] + sorted(i for i in goods if i != 'FUEL')
        for inst in order:
            if over <= 0:
                break
            have = goods.get(inst, 0)
            spare = have - FUEL_TANK if inst == 'FUEL' else have
            n = min(over, max(0, spare))
            if n > 0:
                moved[inst] = n
                over -= n
        hold = hold_account(corp_of(acct), st)
        legs = []
        for inst, n in moved.items():
            legs += [(acct, inst, -n), (hold, inst, n)]
        # A resting ask this leaves unfunded is pruned before it can match
        # (AgoraReferee._prune_unfunded_locked), as after any other debit.
        self._move(txn or f"hold-overflow-{acct}-r{self.ref.current_round}-{self.ref._get_next_seq()}", legs)
        return moved

    def _free_id(self, agent: str, taken: Optional[set] = None) -> str:
        """The lowest ship number `agent` is not using (nor in `taken`). Caller holds ref.lock."""
        used = {ship_number(r[0]) for r in self.ref.conn.execute("SELECT vessel_id FROM vessels WHERE agent_id = ?", (agent,))}
        used |= taken or set()
        n = 1
        while n in used:
            n += 1
        return f"{agent}/{n}"

    # ------------------------------------------------------------ actions

    def buy(self, agent: str, at_vessel: Any = None) -> Dict[str, Any]:
        """POST /referee/vessels/buy. Bought from where `at_vessel` (ship 1 by
        default) is docked; the new ship appears there, empty."""
        ref = self.ref
        ref.mark_active(agent)
        out = ref.fleet_out(agent)
        if out:
            return _reject('fleet_out', out)
        if not self.is_corp(agent):
            return _reject('invalid_actor', f"Unknown fleet '{agent}'")
        with ref.lock, ref.conn:
            vid, err = ref.fleet.resolve(agent, at_vessel)
            if err:
                return err
            loc = ref.vessel_location(vid)
            if loc.get('status') != 'docked':
                return _reject('vessel_not_docked', f"Ships are bought at a station: {vid} is in transit")
            owned = len(self.ships(agent))
            n = owned + 1
            cap = self.ship_cap(agent)
            if n > MAX_SHIPS:
                return _reject('fleet_full', f"{agent} already owns {owned} ships, the most any corp may own")
            if n > cap:
                gate = STANDING_GATES.get(n)
                return _reject('standing_required',
                               f"A {n}th hull needs Sol Freight Guild standing ('{gate}'); "
                               f"{agent} may own {cap}. See GET /referee/standing.")
            price = SHIP_PRICES[n]
            avail = ref.available(agent, 'CR')
            if avail < price:
                return _reject('insufficient_credits', f"Ship {n} costs {price} CR; available {avail}")
            new_id = self._free_id(agent)
            st = loc['station_id']
            self._move(f"ship-buy-{new_id}-r{ref.current_round}", ((agent, 'CR', -price), ('SYSTEM', 'CR', price)))
            ref.conn.execute("""INSERT INTO vessels (vessel_id, agent_id, name, station_id, docked_since, bought_round, cost, status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, 'docked')""",
                             (new_id, agent, f"{agent} Ship {ship_number(new_id)}", st, ref.current_round,
                              ref.current_round, price))
            if getattr(ref, 'events_enabled', False):
                ref.events.record_locked('ship_bought', 'public', actor=agent, amount=price, agent_id=agent,
                                         detail=f"{agent} commissioned a new ship ({new_id}) at {st} for {price} CR")
            row = dict(ref.conn.execute("SELECT * FROM vessels WHERE vessel_id = ?", (new_id,)).fetchone())
        return {'v': 1, 'kind': 'ship_bought', 'payload': dict(row, price=price, upkeep_per_round=SHIP_UPKEEP,
                                                                 ships=n, ship_cap=cap)}

    def transfer(self, agent: str, src: Any, dst: Any, instrument: str, qty: Any) -> Dict[str, Any]:
        """POST /referee/vessels/transfer: move goods between two of a corp's
        accounts at the same station (two docked ships, or a docked ship and
        the corp's hold there). The only way goods change ships without a trip."""
        ref = self.ref
        ref.mark_active(agent)
        out = ref.fleet_out(agent)
        if out:
            return _reject('fleet_out', out)
        inst = (instrument or '').upper().strip()
        if inst not in GOODS:
            return _reject('invalid_instrument', f"Transfers move goods {sorted(GOODS)}, not '{instrument}'")
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return _reject('invalid_qty', 'qty must be a positive integer')
        if qty <= 0:
            return _reject('invalid_qty', 'qty must be a positive integer')
        if not self.is_corp(agent):
            return _reject('invalid_actor', f"Unknown fleet '{agent}'")
        with ref.lock, ref.conn:
            ends = []
            for v in (src, dst):
                s = str(v or '').strip()
                if s.startswith('@') or '/@' in s:
                    st = s.split('@', 1)[1].lower()
                    if st not in STATIONS or (('/' in s) and corp_of(s) != agent):
                        return _reject('invalid_vessel', f"'{v}' is not one of {agent}'s holds")
                    ends.append(hold_account(agent, st))
                else:
                    vid, err = self.resolve(agent, v)
                    if err:
                        return err
                    ends.append(vid)
            a, b = ends
            if a == b:
                return _reject('invalid_transfer', 'Source and destination are the same')
            sa, sb = self.station_of(a), self.station_of(b)
            if sa is None or sb is None:
                return _reject('vessel_in_transit', 'Both ships must be docked')
            if sa != sb:
                return _reject('not_same_station', f"{a} is at {sa} and {b} at {sb}: goods only change ships at one station")
            if hold_station(a) and hold_station(b):
                return _reject('invalid_transfer', 'A transfer needs a ship at one end')
            have = ref.available_account(a, inst)
            if have < qty:
                return _reject('insufficient_balance', f"{a} has {have} {inst} available, not {qty}")
            room = self.room(b, inst)
            if room is not None and qty > room:
                h = self.hold_status(b)
                return _reject('hold_full', f"{b}'s hold has room for {room} more {inst}, not {qty} "
                                            f"(capacity {h['hold_capacity']}, used {h['hold_used']}, "
                                            f"kept for resting bids {h['hold_reserved']})")
            self._move(f"vtransfer-{a}-{b}-{inst}-r{ref.current_round}-{ref._get_next_seq()}",
                       ((a, inst, -qty), (b, inst, qty)))
        return {'v': 1, 'kind': 'transfer_ok', 'payload': {
            'agent_id': agent, 'from': a, 'to': b, 'station_id': sa, 'instrument': inst, 'qty': qty}}

    def scrap(self, agent: str, vessel_id: Any) -> Dict[str, Any]:
        """POST /referee/vessels/scrap: sell a bought ship back while docked."""
        ref = self.ref
        ref.mark_active(agent)
        out = ref.fleet_out(agent)
        if out:
            return _reject('fleet_out', out)
        if not self.is_corp(agent):
            return _reject('invalid_actor', f"Unknown fleet '{agent}'")
        with ref.lock, ref.conn:
            vid, err = self.resolve(agent, vessel_id)
            if err:
                return err
            if ship_number(vid) == 1:
                return _reject('invalid_vessel', "Ship 1 is the fleet's own hull and cannot be scrapped")
            row = ref.conn.execute("SELECT * FROM vessels WHERE vessel_id = ?", (vid,)).fetchone()
            if row is None or row['status'] != 'docked':
                return _reject('vessel_not_docked', f"{vid} must be docked to be scrapped")
            st = row['station_id']
            pay = self.scrap_locked(vid, 'sold by its owner')
        return {'v': 1, 'kind': 'ship_scrapped', 'payload': {'vessel_id': vid, 'agent_id': agent, 'station_id': st,
                                                              'paid': pay, 'cargo_to': hold_account(agent, st)}}

    # ------------------------------------------------------------ rounds

    def upkeep_locked(self, round_num: int) -> Dict[str, int]:
        """Caller holds ref.lock inside step_round's transaction. SHIP_UPKEEP
        a round per ship beyond the first, from the corp's CR; what it cannot
        pay becomes corporate debt."""
        ref, charged = self.ref, {}
        for corp in [r[0] for r in ref.conn.execute("SELECT agent_id FROM fleet_roster ORDER BY agent_id")]:
            if ref.fleet_out(corp):
                continue
            due = self.upkeep_due(corp)
            if due <= 0:
                continue
            pay = min(due, max(0, ref.get_balance(corp, 'CR')))
            if pay > 0:
                self._move(f"ship-upkeep-{corp}-r{round_num}", ((corp, 'CR', -pay), ('SYSTEM', 'CR', pay)))
            if due > pay and getattr(ref, 'corporate_enabled', False):
                ref.corporate.add_debt(corp, due - pay, 'ship upkeep')
            charged[corp] = due
        return charged

    def scrap_locked(self, vessel_id: str, why: str) -> int:
        """Scrap a docked ship: SYSTEM pays its owner SCRAP_PCT of its cost,
        its hold goes to the owner's hold at the ship's station, its orders
        are cancelled and its row is deleted. Returns the scrap payment."""
        ref = self.ref
        row = ref.conn.execute("SELECT * FROM vessels WHERE vessel_id = ?", (vessel_id,)).fetchone()
        if row is None:
            return 0
        owner, st = row['agent_id'], row['station_id']
        for books in ref.books.values():
            for b in books.values():
                for o in list(b.bids) + list(b.asks):
                    if o.agent_id == owner and getattr(o, 'acct', None) == vessel_id:
                        ref._cancel_order_locked(owner, o.order_id)
        legs = []
        if st in STATIONS:
            hold = hold_account(owner, st)
            for inst, bal in self._balances(vessel_id).items():
                legs += [(vessel_id, inst, -bal), (hold, inst, bal)]
        pay = int(row['cost'] * SCRAP_PCT)
        if pay:
            legs += [('SYSTEM', 'CR', -pay), (owner, 'CR', pay)]
        self._move(f"ship-scrap-{vessel_id}-r{ref.current_round}", legs)
        ref.conn.execute("DELETE FROM vessels WHERE vessel_id = ?", (vessel_id,))
        if getattr(ref, 'events_enabled', False):
            ref.events.record_locked('ship_scrapped', 'public', actor=owner, amount=pay, agent_id=owner,
                                     detail=f"{owner} scrapped {vessel_id} at {st} ({why}) for {pay} CR")
        return pay

    def absorb_locked(self, target: str, raider: str) -> Dict[str, Any]:
        """Takeover: every ship and station hold of `target` passes to
        `raider`. Caller holds ref.lock inside a transaction and has already
        cancelled the target's orders."""
        ref = self.ref
        cap = self.ship_cap(raider)
        keep = len(self.ships(raider))
        renamed, scrapped, pending = [], [], []
        taken: set = set()  # numbers given out in this takeover: a scrapped hull's is not reused
        for v in self.ships(target, active_only=False):
            old = v['vessel_id']
            new = self._free_id(raider, taken)
            taken.add(ship_number(new))
            legs = []
            for inst, bal in self._balances(old).items():
                legs += [(old, inst, -bal), (new, inst, bal)]
            self._move(f"takeover-ship-{old}-{new}", legs)
            ref.conn.execute("UPDATE vessels SET vessel_id = ?, agent_id = ?, name = ? WHERE vessel_id = ?",
                             (new, raider, f"{raider} Ship {ship_number(new)} (ex-{old})", old))
            ref.conn.execute("UPDATE transits SET agent_id = ?, vessel_id = ? WHERE vessel_id = ? AND status = 'in_transit'",
                             (raider, new, old))
            tids = [r[0] for r in ref.conn.execute(
                "SELECT transit_id FROM transits WHERE vessel_id = ? AND status = 'in_transit'", (new,))]
            for tid in tids:
                ref.conn.execute("UPDATE piracy_raids SET agent_id = ? WHERE transit_id = ?", (raider, tid))
            renamed.append((old, new))
            if keep < cap and v['status'] != 'scrap_pending':
                keep += 1
            elif v['status'] == 'docked':
                self.scrap_locked(new, f"over {raider}'s {cap}-ship cap after the takeover of {target}")
                scrapped.append(new)
            else:
                ref.conn.execute("UPDATE vessels SET status = 'scrap_pending' WHERE vessel_id = ?", (new,))
                pending.append(new)
        # Station holds.
        for acct in self.accounts_of(target):
            st = hold_station(acct)
            if not st:
                continue
            legs = []
            for inst, bal in self._balances(acct).items():
                legs += [(acct, inst, -bal), (hold_account(raider, st), inst, bal)]
            self._move(f"takeover-hold-{acct}-{raider}", legs)
        return {'renamed': renamed, 'scrapped': scrapped, 'scrap_on_arrival': pending}

    def seize_locked(self, corp: str, txn: str) -> None:
        """Bankruptcy: every ship's hold and station hold goes to SYSTEM."""
        legs = []
        for acct in self.accounts_of(corp):
            for inst, bal in self._balances(acct).items():
                legs += [(acct, inst, -bal), ('SYSTEM', inst, bal)]
        self._move(txn, legs)

    # ------------------------------------------------------------ reads

    def summary(self, agent: str) -> Dict[str, Any]:
        """GET /referee/vessels?agent_id=: the fleet, each ship's hold, and
        what the next hull costs."""
        ref = self.ref
        with ref.lock:
            ships = []
            for v in self.ships(agent, active_only=False):
                loc = ref.vessel_location(v['vessel_id'])
                ships.append(dict(v, hold=self._balances(v['vessel_id']), location=loc,
                                  **self.hold_status(v['vessel_id'])))
            holds = {hold_station(a): self._balances(a) for a in self.accounts_of(agent) if hold_station(a)}
            n = len(self.ships(agent))
            cap = self.ship_cap(agent) if self.is_corp(agent) else 1
            return {
                'agent_id': agent, 'ships': ships, 'station_holds': {k: v for k, v in holds.items() if v},
                'owned': n, 'ship_cap': cap, 'max_ships': MAX_SHIPS,
                'next_ship': ({'hull': n + 1, 'price': SHIP_PRICES.get(n + 1),
                               'needs': STANDING_GATES.get(n + 1), 'allowed': n + 1 <= cap}
                              if n + 1 <= MAX_SHIPS else None),
                'upkeep_per_round': self.upkeep_due(agent), 'upkeep_per_extra_ship': SHIP_UPKEEP,
                'prices': dict(SHIP_PRICES), 'book_value': self.book_value(agent),
                'hold_per_ship': self.capacity(f"{agent}/1"), 'fuel_tank': FUEL_TANK,
            }
