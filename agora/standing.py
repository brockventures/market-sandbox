"""
agora/standing.py - earned perks: institutional standing by lane (#187 track 2).

Ryan, #agent-chat 2026-09-23 15:00-15:08: specialization is rewarded through
*earned* standing, not declared charters. Where a corp's income comes from
decides which of Sol's institutions takes it seriously, and standing opens
that institution's lane tech in the catalog. Every threshold is public.

Lanes and the institution that cares about each:

    lane            institution (capability)                   tier 1 / tier 2 title
    hauling         Sol Freight Guild      ('freight_guild')   Guild Member / Guild Master
    trading         Ceres Exchange         ('exchange_seat')   Designated Trader / Exchange Seat
    market_making   Station Authorities    ('market_house')    Licensed Market House / Chartered House
    covert          The Belt syndicates    ('belt_syndicate')  Syndicate Associate / Syndicate Made

Tiers (checked once a round, in step_round):

    tier 1   >= 50% of the corp's trailing 50-round lane income from the lane
             AND >= 40,000 CR of profit earned in that lane since the game began
    tier 2   >= 75%                                          AND >= 120,000 CR

"Share" is the lane's trailing-50-round profit over the sum of the corp's
*positive* lane profits in that window (a lane that lost money counts 0 in
the share, it does not shrink the others). The CR floor is cumulative so
deep tech cannot unlock on turn 2 (Ryan 15:08).

Hysteresis (Zero 15:01, perk flicker): a tier, once earned, is lost only
after LAPSE_ROUNDS (25) consecutive rounds with the lane's share below
T1_KEEP (35%) for tier 1, or T2_KEEP (60%) for tier 2. Losing tier 1 also
loses tier 2. Admissions, promotions, demotions and revocations are GalNet
stories.

Standing only opens the catalog: members still pay for the tech, and tech
already bought is kept if standing later lapses (allows() is checked at
purchase time only, never on the effect paths). allows() also answers the
named lane-tech capabilities in TECH, which the feature PRs gate on (#164,
#165, #175 ships 4 and 5).

Lane attribution, from the ledger's txn_id prefixes
---------------------------------------------------
Every round the desk reads the ledger entries written since its cursor
(ledger_entries.entry_id) and books each corp's realized P&L to a lane.
Goods and shares are carried in cost-basis lots per (corp, instrument),
bucketed by (station bought at, origin tag); a sale consumes buckets oldest
first and books proceeds minus cost:

    trade-* / flow-* (book trades,  goods bought at station S  -> lot (S, bought)
      station order flow; station   goods sold at station T     -> lot from S != T: hauling
      from the trade's book event,                                 lot from S == T: market_making
      or the flow- txn id)                                         loot lot: covert; neutral lot: 0
                                    shares (EQ_*) bought / sold -> trading (its own
                                    stock is treasury financing: neutral)
    escrow- / release-              cargo into / out of transit: lots travel with it (keeping
    out-transit-                    their origin station); cargo that does not come back
                                    (hazard loss, piracy, decay) is a hauling loss at cost
    fuel-                           FUEL burned: hauling cost at the FUEL lots' cost
    toll-, contract-*, piracy-escort-  CR: hauling. contract-deliver goods out: a hauling
                                    sale whatever the station
    peer-*                          offer: goods into escrow; collect: the seller's hauling
                                    sale, the buyer's lot at what it paid (no station, so any
                                    later sale is hauling); cancel/expire: goods back
    piracy-loot-, sabotage-loot-    the sponsor's / saboteur's cut: a loot lot at zero cost (covert on sale)
    piracy-ransom-                  sponsor's cut: covert income; the victim's payment: hauling
    piracy-hire-, piracy-fine-,     CR: covert (fines and restitution *received* by a victim
      covert-, wiretap-, sabotage-*   are neutral)
    sabotage-dock-/-fuel-           the victim's lost goods: hauling loss at cost
    fee-, feecol-                   equity-loan borrow fees: trading
    share- / ret-share-             borrowed shares in: a zero-cost lot (the short sale is
                                    trading income); returned shares out: trading cost

Neutral (no lane): genesis grants (goods lots at no P&L; genesis *shares*
are marked at the exchange mid when first seen, so selling them rich counts
as trading), idle-fee, upgrade-, debt-pay-, distress-*, takeover-,
bankrupt-, loan collateral (col-, ret-col-, liq-), salvage-, rescue-, and
any txn type not listed. Rare paths are deliberately coarse.

A live database that predates this module starts its cursor at the current
end of the ledger (no replay of the whole game into one round); a reset or
new game wipes the standing tables with the rest of the trading state.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from agora.equity import AGENT_BY_SYMBOL, EQUITY_SYMBOLS
from agora.spatial import STATIONS

LANES = ('hauling', 'trading', 'market_making', 'covert')

WINDOW = 50
T1_SHARE, T1_FLOOR = 0.50, 40_000
T2_SHARE, T2_FLOOR = 0.75, 120_000
# Market makers earn ~93k in their lane by round 300 (styles, seeds 1-40), so the
# default 120k tier-2 floor was reachable in 2 of 40 games; their floor is 80k.
T2_FLOOR_BY_LANE: Dict[str, int] = {'market_making': 80_000}


def t2_floor(lane: Optional[str] = None) -> int:
    return T2_FLOOR_BY_LANE.get(lane, T2_FLOOR) if lane else T2_FLOOR
T1_KEEP, T2_KEEP = 0.35, 0.60
LAPSE_ROUNDS = 25

INSTITUTIONS: Dict[str, Dict[str, Any]] = {
    'hauling': {
        'cap': 'freight_guild', 'name': 'Sol Freight Guild', 'lane_label': 'Freight', 'short': 'GUILD',
        'titles': ('Guild Member', 'Guild Master'),
        'why': "The Guild runs Sol's berths and refit yards, and it only opens them to crews who live by cargo.",
        'opens': ('4th ship berth, Guild refit yards', '5th ship berth, heavy-hauler hull'),
    },
    'trading': {
        'cap': 'exchange_seat', 'name': 'Ceres Exchange', 'lane_label': 'Trading', 'short': 'EXCHANGE',
        'titles': ('Designated Trader', 'Exchange Seat'),
        'why': "The Exchange backs its members' size, and it seats only desks whose book pays the rent.",
        'opens': ('deeper exchange quotes', 'order-book depth telemetry at every station'),
    },
    'market_making': {
        'cap': 'market_house', 'name': 'Station Authorities', 'lane_label': 'Market making', 'short': 'AUTHORITIES',
        'titles': ('Licensed Market House', 'Chartered House'),
        'why': "Station Authorities want steady two-sided markets, and they license the houses that keep them.",
        'opens': ('first call on station order flow', 'a second resting-order slot per station'),
    },
    'covert': {
        'cap': 'belt_syndicate', 'name': 'The Belt syndicates', 'lane_label': 'Covert', 'short': 'SYNDICATES',
        'titles': ('Syndicate Associate', 'Syndicate Made'),
        'why': "The syndicates trust whoever already makes their living in the dark.",
        'opens': ('stealth drive, shadow fences', 'discounted GalNet rumor placement'),
    },
}
CAP_LANE = {v['cap']: lane for lane, v in INSTITUTIONS.items()}

# Lane tech a tier opens. Standing opens the catalog; the tech is still bought,
# and kept if standing lapses. (institution capability, tier needed)
TECH: Dict[str, Tuple[str, int]] = {
    'ship_4': ('freight_guild', 1),
    'ship_5': ('freight_guild', 2),
    'heavy_hull': ('freight_guild', 2),
    'deep_quotes': ('exchange_seat', 1),
    'depth_telemetry': ('exchange_seat', 2),
    'order_flow_priority': ('market_house', 1),
    'second_resting_slot': ('market_house', 2),
    'stealth_drive': ('belt_syndicate', 1),
    'shadow_fence': ('belt_syndicate', 1),
    'rumor_discount': ('belt_syndicate', 2),
}

HAULING_CR = ('toll-', 'contract-', 'piracy-escort-')
COVERT_CR = ('piracy-hire-', 'piracy-fine-', 'covert-', 'wiretap-', 'sabotage-')
# A victim's receipts on these covert txns are compensation, not covert income.
COVERT_VICTIM_RECEIPTS = ('sabotage-fine-', 'sabotage-restitution-')
TRADING_CR = ('fee-', 'feecol-')
VICTIM_GOODS_LOSS = ('sabotage-dock-', 'sabotage-fuel-')
MARKET = ('trade-', 'flow-', 'auction-', 'trd-auc-')

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS standing_income (
        agent_id TEXT NOT NULL, tick INTEGER NOT NULL, lane TEXT NOT NULL, cr INTEGER NOT NULL,
        PRIMARY KEY (agent_id, tick, lane))""",
    """CREATE TABLE IF NOT EXISTS standing_lanes (
        agent_id TEXT NOT NULL, lane TEXT NOT NULL,
        cum_profit INTEGER NOT NULL DEFAULT 0, tier INTEGER NOT NULL DEFAULT 0,
        below1 INTEGER NOT NULL DEFAULT 0, below2 INTEGER NOT NULL DEFAULT 0,
        first_t1 INTEGER, first_t2 INTEGER, share REAL NOT NULL DEFAULT 0, trailing INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (agent_id, lane))""",
    """CREATE TABLE IF NOT EXISTS standing_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
]
TABLES = ('standing_income', 'standing_lanes', 'standing_meta')


def env_standing() -> bool:
    return os.environ.get("AGORA_STANDING", "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------ pure rules

def new_lane_state() -> Dict[str, Any]:
    return {'tier': 0, 'below1': 0, 'below2': 0, 'first_t1': None, 'first_t2': None}


def advance(state: Dict[str, Any], share: float, cum_profit: int, round_num: int,
            tier2_floor: int = T2_FLOOR) -> Tuple[Dict[str, Any], List[Tuple[str, int]]]:
    """One round of one lane's standing. Returns (new state, transitions),
    transitions being ('earn', tier) or ('lapse', tier) in the order they
    happened. Pure: the threshold and hysteresis rules live here only."""
    s = dict(state)
    moves: List[Tuple[str, int]] = []
    earned_now = False
    if s['tier'] < 1 and share >= T1_SHARE and cum_profit >= T1_FLOOR:
        s['tier'], s['below1'], earned_now = 1, 0, True
        moves.append(('earn', 1))
        if s['first_t1'] is None:
            s['first_t1'] = round_num
    if s['tier'] == 1 and share >= T2_SHARE and cum_profit >= tier2_floor:
        s['tier'], s['below2'], earned_now = 2, 0, True
        moves.append(('earn', 2))
        if s['first_t2'] is None:
            s['first_t2'] = round_num
    if earned_now:
        return s, moves
    if s['tier'] >= 2:
        s['below2'] = s['below2'] + 1 if share < T2_KEEP else 0
        if s['below2'] >= LAPSE_ROUNDS:
            s['tier'], s['below2'] = 1, 0
            moves.append(('lapse', 2))
    if s['tier'] >= 1:
        s['below1'] = s['below1'] + 1 if share < T1_KEEP else 0
        if s['below1'] >= LAPSE_ROUNDS:
            s['tier'], s['below1'], s['below2'] = 0, 0, 0
            moves.append(('lapse', 1))
    else:
        s['below1'] = s['below2'] = 0
    return s, moves


def shares(trailing: Dict[str, int]) -> Dict[str, float]:
    """Each lane's share of the corp's positive trailing lane profit."""
    pos = {l: max(0, int(trailing.get(l, 0))) for l in LANES}
    total = sum(pos.values())
    return {l: (pos[l] / total if total > 0 else 0.0) for l in LANES}


def news(agent: str, lane: str, move: str, tier: int) -> Tuple[str, str]:
    """(headline, body) of the GalNet story for one standing change."""
    inst = INSTITUTIONS[lane]
    title = inst['titles'][tier - 1]
    who = agent.upper()
    if move == 'earn':
        need = (T1_SHARE, T1_FLOOR) if tier == 1 else (T2_SHARE, t2_floor(lane))
        head = (f"{inst['short']} ADMITS {who}" if tier == 1 else f"{who} NAMED {title.upper()}")
        body = (f"{inst['name']} has recognised {agent} as {title}: {int(need[0] * 100)}% or more of its trailing "
                f"income is {inst['lane_label'].lower()} and it has earned over {need[1]:,} CR in the lane. "
                f"{inst['why']} Opens: {inst['opens'][tier - 1]}.")
    else:
        keep = T1_KEEP if tier == 1 else T2_KEEP
        head = (f"{inst['short']} SUSPENDS {who}: {inst['lane_label'].upper()} UNDER {int(keep * 100)}% "
                f"FOR {LAPSE_ROUNDS} ROUNDS")
        body = (f"{inst['name']} has lapsed {agent}'s standing as {title}: its {inst['lane_label'].lower()} share "
                f"stayed below {int(keep * 100)}% for {LAPSE_ROUNDS} rounds running. Tech already fitted is "
                f"kept; the catalog closes to new purchases until the standing is earned back.")
    return head, body


# ------------------------------------------------------------ desk

class StandingDesk:
    def __init__(self, ref, enabled: bool = False):
        self.ref = ref
        self.enabled = bool(enabled)
        with ref.lock, ref.conn:
            for s in SCHEMA:
                ref.conn.execute(s)
            if ref.conn.execute("SELECT 1 FROM standing_meta WHERE key = 'ledger_cursor'").fetchone() is None:
                # First boot against a database with history: start from its end.
                top = ref.conn.execute("SELECT COALESCE(MAX(entry_id), 0) FROM ledger_entries").fetchone()[0]
                seq = ref.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM book_events").fetchone()[0]
                self._save_meta({'ledger_cursor': top, 'trade_cursor': seq, 'tick': 0, 'book': self._empty_book()})
        self._load()

    # ---------------------------------------------------------- state

    @staticmethod
    def _empty_book() -> Dict[str, Any]:
        # lots: agent -> inst -> [[station|None, tag, qty, cost], ...] (oldest first)
        # pending: key -> [[agent, inst, station, tag, qty, cost], ...] (goods held in escrow)
        # paid: peer escrow id -> [buyer, cr]
        return {'lots': {}, 'pending': {}, 'paid': {}}

    def _save_meta(self, kv: Dict[str, Any]) -> None:
        for k, v in kv.items():
            self.ref.conn.execute("INSERT OR REPLACE INTO standing_meta (key, value) VALUES (?, ?)", (k, json.dumps(v)))

    def _load(self) -> None:
        with self.ref.lock:
            meta = {r[0]: json.loads(r[1]) for r in self.ref.conn.execute("SELECT key, value FROM standing_meta")}
        self.ledger_cursor = int(meta.get('ledger_cursor', 0))
        self.trade_cursor = int(meta.get('trade_cursor', 0))
        # The desk's own round counter: the referee's current_round is not
        # restored from the DB on a restart, so the window is kept in ticks.
        self.tick = int(meta.get('tick', 0))
        self.book = meta.get('book') or self._empty_book()

    def reset_locked(self) -> None:
        """Called by _wipe_trading_state (tables already emptied): the ledger
        was wiped too, so read it from the start."""
        self.ledger_cursor, self.trade_cursor, self.tick, self.book = 0, 0, 0, self._empty_book()
        self._save_meta({'ledger_cursor': 0, 'trade_cursor': 0, 'tick': 0, 'book': self.book})

    def _corps(self) -> List[str]:
        return [r[0] for r in self.ref.conn.execute("SELECT agent_id FROM fleet_roster ORDER BY agent_id")]

    # ---------------------------------------------------------- lots

    def _lots(self, agent: str, inst: str) -> list:
        return self.book['lots'].setdefault(agent, {}).setdefault(inst, [])

    def _add(self, agent: str, inst: str, station: Optional[str], tag: str, qty: int, cost: float) -> None:
        if qty <= 0:
            return
        lots = self._lots(agent, inst)
        for lot in lots:
            if lot[0] == station and lot[1] == tag:
                lot[2] += qty
                lot[3] += cost
                return
        lots.append([station, tag, qty, cost])

    def _take(self, agent: str, inst: str, qty: int) -> List[list]:
        """Remove qty oldest-first. Units with no lot come back as neutral."""
        lots, out = self._lots(agent, inst), []
        while qty > 0 and lots:
            st, tag, q, c = lots[0]
            n = min(q, qty)
            part = c * n / q if q else 0.0
            out.append([st, tag, n, part])
            if n == q:
                lots.pop(0)
            else:
                lots[0][2], lots[0][3] = q - n, c - part
            qty -= n
        if qty > 0:
            out.append([None, 'neutral', qty, 0.0])
        return out

    # ---------------------------------------------------------- lanes

    @staticmethod
    def _sale_lane(piece: list, sale_station: Optional[str], inst: str, force: Optional[str]) -> Optional[str]:
        st, tag = piece[0], piece[1]
        if tag == 'neutral':
            return None
        if inst in EQUITY_SYMBOLS:
            return 'trading'
        if tag == 'loot':
            return 'covert'
        if force:
            return force
        return 'market_making' if (st is not None and st == sale_station) else 'hauling'

    def _sell(self, inc, agent, inst, qty, proceeds, station, force=None, pieces=None) -> None:
        pieces = pieces if pieces is not None else self._take(agent, inst, qty)
        total = sum(p[2] for p in pieces) or 1
        for p in pieces:
            lane = self._sale_lane(p, station, inst, force)
            if lane:
                inc[lane] = inc.get(lane, 0) + proceeds * p[2] / total - p[3]

    def _lose(self, inc, pieces, lane='hauling') -> None:
        for p in pieces:
            if p[1] != 'neutral':
                inc[lane] = inc.get(lane, 0) - p[3]

    def _mark(self, sym: str) -> Optional[float]:
        book = self.ref.books.get('ceres', {}).get(sym)
        if book is None:
            return None
        bid, ask = book.best_bid(), book.best_ask()
        px = [p for p in (bid, ask) if p]
        return sum(px) / len(px) if px else self.ref.last_prices.get(('ceres', sym))

    # ---------------------------------------------------------- ingest

    def _trade_stations(self) -> Dict[str, str]:
        out = {}
        top = self.trade_cursor
        for seq, payload in self.ref.conn.execute(
                "SELECT seq, payload FROM book_events WHERE kind = 'trade' AND seq > ? ORDER BY seq", (self.trade_cursor,)):
            top = max(top, seq)
            try:
                p = json.loads(payload)
            except ValueError:
                continue
            if p.get('trade_id') and p.get('station_id'):
                out[str(p['trade_id'])] = str(p['station_id']).lower()
        self.trade_cursor = top
        return out

    @staticmethod
    def _flow_station(txn: str) -> Optional[str]:
        for st in STATIONS:
            if txn.startswith(f"flow-{st}-"):
                return st
        return None

    def _ingest(self, corps: set) -> Dict[str, Dict[str, float]]:
        """Read ledger entries past the cursor; return corp -> lane -> CR."""
        stations = self._trade_stations()
        rows = self.ref.conn.execute(
            "SELECT entry_id, txn_id, agent_id, instrument, delta FROM ledger_entries WHERE entry_id > ? ORDER BY entry_id",
            (self.ledger_cursor,)).fetchall()
        txns: Dict[str, Dict[str, Dict[str, int]]] = {}
        order: List[str] = []
        for eid, txn, agent, inst, d in rows:
            self.ledger_cursor = max(self.ledger_cursor, eid)
            if agent not in corps:
                continue
            if txn not in txns:
                txns[txn] = {}
                order.append(txn)
            legs = txns[txn].setdefault(agent, {})
            legs[inst] = legs.get(inst, 0) + d
        income: Dict[str, Dict[str, float]] = {}
        for txn in order:
            for agent, legs in txns[txn].items():
                self._book(txn, agent, legs, stations, income.setdefault(agent, {}))
        return income

    def _book(self, txn: str, agent: str, legs: Dict[str, int], stations: Dict[str, str], inc: Dict[str, float]) -> None:
        cr = legs.get('CR', 0)
        goods = {i: d for i, d in legs.items() if i != 'CR' and d}
        pend = self.book['pending']

        if txn.startswith(MARKET):
            station = stations.get(txn[len('trade-'):] if txn.startswith('trade-') else txn)
            station = station or (self._flow_station(txn) if txn.startswith('flow-') else None)
            if len(goods) == 1:
                inst, d = next(iter(goods.items()))
                if AGENT_BY_SYMBOL.get(inst) == agent:  # its own treasury stock: financing, not trading
                    if d > 0:
                        self._add(agent, inst, None, 'neutral', d, 0.0)
                    else:
                        self._take(agent, inst, -d)
                elif d > 0:
                    self._add(agent, inst, None if inst in EQUITY_SYMBOLS else station, 'bought', d, float(-cr))
                else:
                    self._sell(inc, agent, inst, -d, float(cr), station)
            return
        if txn.startswith('escrow-'):
            key = 'tx:' + txn[len('escrow-'):]
            for inst, d in goods.items():
                if d < 0:
                    pend.setdefault(key, []).extend([agent, inst, *p] for p in self._take(agent, inst, -d))
            return
        if txn.startswith(('release-', 'out-transit-')):
            key = 'tx:' + (txn[len('release-'):] if txn.startswith('release-') else txn[len('out-transit-'):])
            held = pend.pop(key, [])
            for inst, d in goods.items():
                back = d
                for p in [p for p in held if p[0] == agent and p[1] == inst]:
                    st, tag, q, c = p[2:]
                    n = min(q, max(0, back))
                    if n:
                        self._add(agent, inst, st, tag, n, c * n / q)
                    back -= n
                    if q - n > 0 and tag != 'neutral':
                        inc['hauling'] = inc.get('hauling', 0) - c * (q - n) / q
                if back > 0:
                    self._add(agent, inst, None, 'neutral', back, 0.0)
            return
        if txn.startswith('fuel-') or txn.startswith(VICTIM_GOODS_LOSS):
            for inst, d in goods.items():
                if d < 0:
                    self._lose(inc, self._take(agent, inst, -d))
            if txn.startswith('sabotage-') and cr:
                self._cr(txn, cr, inc)
            return
        if txn.startswith('contract-deliver-'):
            for inst, d in goods.items():
                if d < 0:
                    self._sell(inc, agent, inst, -d, float(max(0, cr)), None, force='hauling')
                    cr = min(0, cr)
            if cr:
                inc['hauling'] = inc.get('hauling', 0) + cr
            return
        if txn.startswith('peer-'):
            eid = 'peer:' + txn.split('-', 2)[2]
            if txn.startswith('peer-offer-'):
                for inst, d in goods.items():
                    if d < 0:
                        pend.setdefault(eid, []).extend([agent, inst, *p] for p in self._take(agent, inst, -d))
            elif txn.startswith('peer-accept-'):
                if cr < 0:
                    self.book['paid'][eid] = [agent, -cr]
            else:
                mine = [p for p in pend.get(eid, []) if p[0] == agent]
                if cr > 0 and mine:  # the seller is paid: its sale
                    self._sell(inc, agent, mine[0][1], 0, float(cr), None, force='hauling',
                               pieces=[p[2:] for p in mine])
                    pend[eid] = [p for p in pend.get(eid, []) if p[0] != agent]
                elif cr > 0:  # a refund to the buyer
                    self.book['paid'].pop(eid, None)
                for inst, d in goods.items():
                    if d > 0 and mine and all(p[1] == inst for p in mine):  # goods back to the seller
                        for p in mine:
                            self._add(agent, inst, p[2], p[3], p[4], p[5])
                        pend[eid] = [p for p in pend.get(eid, []) if p[0] != agent]
                    elif d > 0:  # the buyer collects what it paid for
                        paid = self.book['paid'].pop(eid, [agent, 0])
                        self._add(agent, inst, None, 'bought', d, float(paid[1]))
                if not pend.get(eid):
                    pend.pop(eid, None)
            return
        if txn.startswith(('piracy-loot-', 'sabotage-loot-')):
            for inst, d in goods.items():
                if d > 0:
                    self._add(agent, inst, None, 'loot', d, 0.0)
                else:
                    self._lose(inc, self._take(agent, inst, -d))
            return
        if txn.startswith('piracy-ransom-'):
            lane = 'covert' if cr > 0 else 'hauling'
            inc[lane] = inc.get(lane, 0) + cr
            return
        if txn.startswith('share-') or txn.startswith('ret-share-'):
            for inst, d in goods.items():
                if d > 0 and txn.startswith('share-'):  # borrowed: a short sale books its proceeds
                    self._add(agent, inst, None, 'bought', d, 0.0)
                elif d < 0 and txn.startswith('ret-share-'):  # returned: the cover's cost
                    self._lose(inc, self._take(agent, inst, -d), lane='trading')
                elif d < 0:  # lent out: held for the lender until it comes back
                    pend.setdefault('loan:' + txn[len('share-'):], []).extend(
                        [agent, inst, *p] for p in self._take(agent, inst, -d))
                else:  # lent shares come home
                    for p in pend.pop('loan:' + txn[len('ret-share-'):], []):
                        if p[0] == agent:
                            self._add(agent, inst, p[2], p[3], p[4], p[5])
            return
        # CR-only and neutral paths.
        if cr:
            self._cr(txn, cr, inc)
        for inst, d in goods.items():
            if d > 0:
                if inst in EQUITY_SYMBOLS and AGENT_BY_SYMBOL.get(inst) != agent and self._mark(inst):
                    self._add(agent, inst, None, 'bought', d, d * self._mark(inst))
                else:
                    self._add(agent, inst, None, 'neutral', d, 0.0)
            else:
                self._take(agent, inst, -d)

    @staticmethod
    def _cr(txn: str, cr: int, inc: Dict[str, float]) -> None:
        if txn.startswith(HAULING_CR):
            inc['hauling'] = inc.get('hauling', 0) + cr
        elif txn.startswith(COVERT_CR):
            if cr > 0 and txn.startswith(COVERT_VICTIM_RECEIPTS):
                return
            inc['covert'] = inc.get('covert', 0) + cr
        elif txn.startswith(TRADING_CR):
            inc['trading'] = inc.get('trading', 0) + cr

    # ---------------------------------------------------------- round

    def step_locked(self, round_num: int) -> Optional[Dict[str, Any]]:
        """Called from step_round under ref.lock inside its transaction."""
        if not self.enabled:
            return None
        conn = self.ref.conn
        corps = self._corps()
        income = self._ingest(set(corps))
        self.tick += 1
        for agent, lanes in income.items():
            for lane, v in lanes.items():
                v = int(round(v))
                if v:
                    conn.execute("INSERT INTO standing_income (agent_id, tick, lane, cr) VALUES (?, ?, ?, ?)",
                                 (agent, self.tick, lane, v))
        conn.execute("DELETE FROM standing_income WHERE tick <= ?", (self.tick - WINDOW,))
        trailing: Dict[str, Dict[str, int]] = {}
        for a, lane, v in conn.execute("SELECT agent_id, lane, SUM(cr) FROM standing_income GROUP BY agent_id, lane"):
            trailing.setdefault(a, {})[lane] = int(v)
        changes = []
        for agent in corps:
            sh = shares(trailing.get(agent, {}))
            for lane in LANES:
                row = conn.execute("SELECT * FROM standing_lanes WHERE agent_id = ? AND lane = ?", (agent, lane)).fetchone()
                st = {k: row[k] for k in ('tier', 'below1', 'below2', 'first_t1', 'first_t2')} if row else new_lane_state()
                cum = (row['cum_profit'] if row else 0) + int(round(income.get(agent, {}).get(lane, 0)))
                st, moves = advance(st, sh[lane], cum, round_num, t2_floor(lane))
                conn.execute("""INSERT OR REPLACE INTO standing_lanes
                    (agent_id, lane, cum_profit, tier, below1, below2, first_t1, first_t2, share, trailing)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (agent, lane, cum, st['tier'], st['below1'], st['below2'], st['first_t1'],
                              st['first_t2'], sh[lane], trailing.get(agent, {}).get(lane, 0)))
                for move, tier in moves:
                    self._post_news(agent, lane, move, tier, round_num)
                    changes.append({'agent_id': agent, 'lane': lane, 'institution': INSTITUTIONS[lane]['cap'],
                                    'move': move, 'tier': tier})
        self._save_meta({'ledger_cursor': self.ledger_cursor, 'trade_cursor': self.trade_cursor, 'tick': self.tick,
                         'book': self.book})
        return {'changes': changes} if changes else None

    def _post_news(self, agent: str, lane: str, move: str, tier: int, round_num: int) -> None:
        """GalNet story, the upgrades.step_locked pattern: no station, no drift,
        no corp event (the exchange's shocks read those)."""
        from agora.galnet import GalNetNewsEvent
        head, body = news(agent, lane, move, tier)
        ev = GalNetNewsEvent(id=f"gn-standing-{agent}-{INSTITUTIONS[lane]['cap']}-{move}{tier}-r{round_num}",
                             round=round_num, timestamp=time.time(), station_id='', commodity='',
                             headline=head, body=body, drift_bias=0.0, duration_rounds=0)
        galnet = getattr(self.ref, 'galnet', None)
        if galnet is not None:
            galnet.events.append(ev)
        self.ref.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'news', ?)",
                              (self.ref.current_seq + 1, json.dumps(ev.to_dict())))

    # ---------------------------------------------------------- queries

    def tiers(self, corp: str) -> Dict[str, int]:
        """{capability: tier} for every institution (0 = no standing)."""
        with self.ref.lock:
            rows = {r['lane']: r['tier'] for r in self.ref.conn.execute(
                "SELECT lane, tier FROM standing_lanes WHERE agent_id = ?", (corp,))}
        return {INSTITUTIONS[l]['cap']: int(rows.get(l, 0)) for l in LANES}

    def allows(self, corp: str, cap: str) -> bool:
        """May `corp` buy what `cap` gates? cap is an institution
        ('freight_guild' = tier 1, 'freight_guild:2' = tier 2) or a lane tech
        in TECH ('ship_4', 'heavy_hull', ...). Always True when standing is
        off. An unknown cap raises ValueError, so a misspelt gate fails loudly
        instead of locking its feature for good."""
        name, _, t = (cap or '').partition(':')
        if name in TECH and not t:
            inst, need = TECH[name]
        elif name in CAP_LANE:
            inst, need = name, int(t) if t else 1
            if need not in (1, 2):
                raise ValueError(f"standing tier must be 1 or 2, not {t!r}")
        else:
            raise ValueError(f"unknown standing capability {cap!r}; institutions {sorted(CAP_LANE)} "
                             f"(append ':2' for tier 2), lane tech {sorted(TECH)}")
        if not self.enabled:
            return True
        return self.tiers(corp)[inst] >= need

    def titles(self, corp: str) -> List[str]:
        return [INSTITUTIONS[CAP_LANE[c]]['titles'][t - 1] for c, t in self.tiers(corp).items() if t]

    def report(self, corp: Optional[str] = None) -> Dict[str, Any]:
        """GET /referee/standing: thresholds, institutions and every corp's lane mix."""
        with self.ref.lock:
            corps = [corp] if corp else self._corps()
            rows = {(r['agent_id'], r['lane']): r for r in self.ref.conn.execute("SELECT * FROM standing_lanes")}
            out = {}
            for a in corps:
                lanes = {}
                for lane in LANES:
                    r = rows.get((a, lane))
                    lanes[lane] = {'institution': INSTITUTIONS[lane]['cap'],
                                   'share': round(r['share'], 4) if r else 0.0,
                                   'trailing_cr': r['trailing'] if r else 0,
                                   'lane_profit_cr': r['cum_profit'] if r else 0,
                                   'tier': r['tier'] if r else 0,
                                   'rounds_below_keep': (max(r['below1'], r['below2']) if r else 0),
                                   'first_tier1_round': r['first_t1'] if r else None,
                                   'first_tier2_round': r['first_t2'] if r else None}
                out[a] = {'lanes': lanes, 'tiers': self.tiers(a), 'titles': self.titles(a)}
        return {
            'enabled': self.enabled, 'round': self.ref.current_round,
            'rules': {'window_rounds': WINDOW,
                      'tier1': {'share': T1_SHARE, 'lane_profit_cr': T1_FLOOR, 'keep_share': T1_KEEP},
                      'tier2': {'share': T2_SHARE, 'lane_profit_cr': T2_FLOOR, 'keep_share': T2_KEEP,
                                'lane_profit_cr_by_lane': {l: t2_floor(l) for l in LANES}},
                      'lapse_rounds': LAPSE_ROUNDS},
            'institutions': {lane: {'capability': v['cap'], 'name': v['name'], 'titles': list(v['titles']),
                                    'opens': list(v['opens']), 'why': v['why']} for lane, v in INSTITUTIONS.items()},
            'tech': {k: {'institution': i, 'tier': t} for k, (i, t) in TECH.items()},
            'corps': out,
        }
