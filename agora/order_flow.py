"""
agora/order_flow.py - station order flow: NPC buyers and sellers (#162).

Until this, a fleet that quoted a market (a resting bid below the depot's
ask, a resting ask above its bid) could only be filled by the other three
fleets, and they almost always hit the depot instead. Market making was not
a way to earn a living (#155 baseline, 300 rounds: a joining maker about
-0.7k).

Each round every station's own traders come to its market: consumers who
want to buy, producers who want to sell. They are sized from the station
depot's demand, the same per-round drips the reactive depot uses: a good's
dearest station consumes REACTIVE_MAIN_DRIP a round and every other station
REACTIVE_SIDE_DRIP (agora/referee.py), and production is the mirror image
at the cheapest station. For each station and good, per round:

    buy qty  = FLOW_SCALE x consumption drip x U(FLOW_JITTER)
    sell qty = FLOW_SCALE x production drip  x U(FLOW_JITTER)

On the main side (the dearest station's buyers, the cheapest station's
sellers) the drip is further scaled by FLOW_MAIN_SCALE, which is 0 (#180):
there the depot's own drips already are the station's demand and supply, and
full main-side flow let a hauler farm it (see FLOW_MAIN_SCALE).

NPC buyers will pay up to the depot's own ask (what they would pay the
depot), and NPC sellers take no less than the depot's bid. They fill
against the best resting FLEET orders first, in price-time priority, at the
fleet's limit price: an ask at or below the depot ask, a bid at or above
the depot bid. Whatever no fleet quotes, the depot already serves through
its production and consumption drips, so the rest is left there and the
depot's shelf and hold are not touched. A fleet that quotes inside the
depot's spread (or joins it: a fleet order beats the depot at the same
price) is paid the spread.

Settlement: each fill is one balanced ledger transaction between the fleet
and SYSTEM (the outside world, as for depot production and contract
payments), txn_id `flow-<station>-<good>-r<round>-<n>`. It is capped at what
the fleet still holds (goods for an ask, CR for a bid), so no balance goes
negative. Only fleets docked at the station are filled (moving does not
cancel resting orders, so a fleet could otherwise quote at every station it
has passed). Halted books and fleets that are out of the game are skipped.
Flow runs at the start of step_round, before prices move, against the
orders fleets left resting during the round that is ending and the depot
quotes they saw.

Rolls come from a seeded random.Random, reset by new_game (game seed) and
reset_to_genesis (0). On in the live server (build_referee_from_env,
AGORA_ORDER_FLOW=0 turns it off); AgoraReferee() leaves it off, so unit
tests opt in.
"""

import json
import math
import os
import random
from typing import Any, Dict, List, Optional, Tuple

from agora.spatial import STATIONS, COMMODITIES, BASE_PRICES

# NPC units per side per round, as a multiple of the depot's drip for that
# side (main 100, side 20 units a round). #162 sweep: a maker earns about
# 33k over 300 rounds per 1.0 of scale; 3.0 puts it in the other styles' band.
FLOW_SCALE = 3.0
# Each side's size is scaled by a uniform draw from this range every round.
FLOW_JITTER = (0.5, 1.5)
# Main-side NPC flow (a good's dearest station's buyers, its cheapest
# station's sellers) as a share of the depot's main drip. Off (#180): there
# the depot's own consumption and production drips are the station's demand
# and supply. At 1.0 a hauler resting its cargo at the dear station's ask
# sold up to ~300 units a round at the depot ask (about 11% over the depot
# bid) without adding to the depot's hold, and hauling pulled far ahead of
# every other style (hauler + privateer median about 580k against 290k).
# Side stations, where makers quote, keep FLOW_SCALE x the side drip.
FLOW_MAIN_SCALE = 0.0
FLOW_GOODS = tuple(COMMODITIES)
FLOW_ACCOUNT = 'SYSTEM'


def env_order_flow() -> bool:
    return os.environ.get("AGORA_ORDER_FLOW", "").strip().lower() in ("1", "true", "yes", "on")


def _drips(ref, st: str, comm: str) -> Tuple[int, int]:
    """(production, consumption) per round for this station's depot."""
    rx = getattr(ref, '_reactive', None) or {}
    key = (st, comm)
    if 'prod' in rx and key in rx['prod']:
        return rx['prod'][key], rx['cons'][key]
    from agora import referee as R  # the drip constants live with the depot model
    cheap = min(STATIONS, key=lambda s: BASE_PRICES[s][comm])
    dear = max(STATIONS, key=lambda s: BASE_PRICES[s][comm])
    return (R.REACTIVE_MAIN_DRIP if st == cheap else R.REACTIVE_SIDE_DRIP,
            R.REACTIVE_MAIN_DRIP if st == dear else R.REACTIVE_SIDE_DRIP)


def _flow_drips(ref, st: str, comm: str) -> Tuple[float, float]:
    """(sell, buy) NPC base size a round: the depot's drips, with the main
    side (above the side drip) scaled by FLOW_MAIN_SCALE."""
    from agora import referee as R
    prod, cons = _drips(ref, st, comm)
    return (prod * FLOW_MAIN_SCALE if prod > R.REACTIVE_SIDE_DRIP else prod,
            cons * FLOW_MAIN_SCALE if cons > R.REACTIVE_SIDE_DRIP else cons)


class OrderFlowDesk:
    def __init__(self, ref, enabled: bool = False, seed: int = 0):
        self.ref = ref
        self.enabled = bool(enabled)
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.rng = random.Random(f"order-flow-{seed}")
        self.last: Dict[str, Any] = {}
        self.totals = {'units_bought': 0, 'units_sold': 0, 'cr_paid': 0, 'cr_received': 0, 'fills': 0}
        self.by_fleet: Dict[str, Dict[str, int]] = {}

    # ------------------------------------------------------------ sizing

    def expected(self, st: str, comm: str) -> Dict[str, float]:
        """Mean NPC buy and sell size a round at this station for this good."""
        prod, cons = _flow_drips(self.ref, st, comm)
        mid = (FLOW_JITTER[0] + FLOW_JITTER[1]) / 2
        return {'buy': FLOW_SCALE * cons * mid, 'sell': FLOW_SCALE * prod * mid}

    def _depot_quotes(self, st: str, comm: str) -> Tuple[Optional[int], Optional[int]]:
        """The depot's resting bid and ask; if a side is empty, the unskewed
        quote it would post (spot x 0.97 / x 1.03)."""
        ref = self.ref
        book = ref.books.get(st, {}).get(comm)
        depot = f"depot_{st}"
        bid = next((o.limit_price for o in (book.bids if book else []) if o.agent_id == depot), None)
        ask = next((o.limit_price for o in (book.asks if book else []) if o.agent_id == depot), None)
        spot = ref.spatial.get_station_price(st, comm) if ref.spatial else BASE_PRICES[st][comm]
        if bid is None:
            bid = max(1, int(math.floor(spot * 0.97)))
        if ask is None:
            ask = max(bid + 1, int(math.ceil(spot * 1.03)))
        return bid, ask

    # ------------------------------------------------------------ matching

    def _eligible(self, agent: str, st: str) -> bool:
        """A fleet's order meets the station's traders only while the fleet
        is docked there. Moving does not cancel resting orders, and without
        this a fleet could leave quotes at every station it passed and make
        markets at all four at once."""
        if agent.startswith('depot_') or agent == FLOW_ACCOUNT or self.ref.fleet_out(agent):
            return False
        loc = self.ref.get_vessel_location(agent)
        return loc.get('status') == 'docked' and loc.get('station_id') == st

    def _settle(self, st: str, comm: str, o, qty: int, npc_buys: bool, round_num: int, n: int) -> None:
        """One fill between fleet order o and the station's NPC traders."""
        ref = self.ref
        cost = qty * o.limit_price
        txn = f"flow-{st}-{comm.lower()}-r{round_num}-{n}"
        seq = ref.current_seq + 1  # book_events.seq is unique: one new seq per fill
        sign = -1 if npc_buys else 1  # the fleet's goods delta
        legs = ((o.agent_id, comm, sign * qty), (FLOW_ACCOUNT, comm, -sign * qty),
                (o.agent_id, 'CR', -sign * cost), (FLOW_ACCOUNT, 'CR', sign * cost))
        for acct, inst, d in legs:
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)",
                             (acct, inst))
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                             (d, acct, inst))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                             (txn, seq, acct, inst, d))
        o.filled_qty += qty
        ref.conn.execute("""
            UPDATE orders SET filled_qty = filled_qty + ?,
                status = CASE WHEN filled_qty + ? >= qty THEN 'filled' ELSE status END,
                resolved_seq = CASE WHEN filled_qty + ? >= qty THEN ? ELSE resolved_seq END
            WHERE order_id = ? AND agent_id = ?
        """, (qty, qty, qty, seq, o.order_id, o.agent_id))
        ref.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'trade', ?)", (seq, json.dumps({
            'trade_id': txn, 'station_id': st, 'instrument': comm, 'price': o.limit_price, 'qty': qty, 'cost': cost,
            'buyer_id': 'station_flow' if npc_buys else o.agent_id,
            'seller_id': o.agent_id if npc_buys else 'station_flow', 'order_flow': True})))
        t = self.totals
        t['fills'] += 1
        f = self.by_fleet.setdefault(o.agent_id, {'units': 0, 'cr': 0})
        f['units'] += qty
        f['cr'] += cost
        if npc_buys:
            t['units_bought'] += qty
            t['cr_paid'] += cost
        else:
            t['units_sold'] += qty
            t['cr_received'] += cost

    def _sweep(self, st: str, comm: str, orders: List, want: int, ok, npc_buys: bool,
               round_num: int, n: int) -> Tuple[int, int]:
        """Fill up to `want` units against fleet orders (already in priority
        order) whose price passes ok(price). Returns (units filled, next n)."""
        filled = 0
        ref = self.ref
        for o in list(orders):
            if filled >= want:
                break
            if not ok(o.limit_price):
                break
            if o.remaining_qty <= 0 or not self._eligible(o.agent_id, st):
                continue
            if npc_buys:
                can = ref.get_balance(o.agent_id, comm)
            else:
                can = ref.get_balance(o.agent_id, 'CR') // o.limit_price
            qty = min(want - filled, o.remaining_qty, max(0, can))
            if qty <= 0:
                continue
            n += 1
            self._settle(st, comm, o, qty, npc_buys, round_num, n)
            filled += qty
            if o.is_filled:
                orders.remove(o)
        return filled, n

    def step_locked(self, round_num: int) -> Optional[Dict[str, Any]]:
        """Caller holds ref.lock inside a transaction. One round of NPC flow."""
        ref = self.ref
        if not self.enabled or not ref.depots_enabled:
            return None
        report: Dict[str, Any] = {}
        n = 0
        for st in STATIONS:
            for comm in FLOW_GOODS:
                prod, cons = _flow_drips(ref, st, comm)
                want_buy = int(round(FLOW_SCALE * cons * self.rng.uniform(*FLOW_JITTER)))
                want_sell = int(round(FLOW_SCALE * prod * self.rng.uniform(*FLOW_JITTER)))
                book = ref.books.get(st, {}).get(comm)
                if book is None or ref.circuit_breaker.is_halted(st, comm):
                    continue
                bid, ask = self._depot_quotes(st, comm)
                bought, n = self._sweep(st, comm, book.asks, want_buy, lambda p: p <= ask, True, round_num, n)
                sold, n = self._sweep(st, comm, book.bids, want_sell, lambda p: p >= bid, False, round_num, n)
                if bought or sold:
                    report.setdefault(st, {})[comm] = {'npc_bought': bought, 'npc_sold': sold,
                                                       'depot_bid': bid, 'depot_ask': ask}
        self.last = {'round': round_num, 'fills': report}
        return report

    # ------------------------------------------------------------ reads

    def status(self) -> Dict[str, Any]:
        """GET /referee/order-flow."""
        return {'enabled': self.enabled, 'flow_scale': FLOW_SCALE, 'flow_main_scale': FLOW_MAIN_SCALE, 'jitter': list(FLOW_JITTER),
                'expected': {st: {c: {k: round(v, 1) for k, v in self.expected(st, c).items()} for c in FLOW_GOODS}
                             for st in STATIONS},
                'last': self.last, 'totals': dict(self.totals)}
