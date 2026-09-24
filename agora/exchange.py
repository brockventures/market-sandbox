"""
agora.exchange - the stock exchange's own market maker.

Stations quote goods; until this, nothing quoted fleet stocks, so a stock
order filled only if another fleet happened to rest the other side (found
2026-09-23 building the #119 stock-trader sim; Ryan approved an exchange
quote the same morning: "this is also how you will control the volatility
and fluctuation in price of the corps").

The exchange is an account, `depot_exchange`. The `depot_` prefix keeps it
off the leaderboard and out of fleet stock holdings, like station depots.
At genesis it takes SHARES of each fleet's stock from that fleet's treasury
(nothing is minted, the 1,000 total holds) and SEED_CR credits from SYSTEM.

Each round it quotes every stock two-sided around a reference price:

    anchor = mean of the fleet's NAV over the last ANCHOR_ROUNDS rounds,
             carried forward along that window's trend (a plain mean lags)
    ref   += REVERSION * (anchor - ref) + VOL * ref * N(0, 1)
           + IMPACT * ref * (shares the exchange sold - bought last round) / DEPTH
    bid    = ref * (1 - SPREAD), ask = ref * (1 + SPREAD), DEPTH shares a side

Depth follows the stock's traded volume (#187 track 1). The stock trader
lane is meant to scale with capital, and a flat 20 shares a side capped
what capital could deploy. Each stock's depth is

    DEPTH = clamp(MIN_DEPTH + VOLUME_K * trailing volume a round, MIN_DEPTH, MAX_DEPTH)

where trailing volume is every share of that stock traded on the exchange
book (the exchange's own fills and fleet-to-fleet trades alike) over the
last VOLUME_ROUNDS rounds, averaged over the whole window. A quiet stock
quotes MIN_DEPTH (the old flat 20); a busy one up to MAX_DEPTH. At
VOLUME_K = 1 the exchange quotes its base plus what the stock actually
trades a round, so a trader who takes a full side every round deepens it
round over round until MAX_DEPTH holds it. Swept on the `styles` scenario
(seeds 1-40): VOLUME_K 0.5 / 1 / 2 lifted the stock trader's median about
+6k / +10k / +23k over flat depth; at 1, no other style moved more than 2k.
Price impact is measured against the depth that was quoted when the
shares traded, so a full side still moves the price IMPACT.

Hard cap on holdings: the exchange never bids for more shares of a stock
than would take it to MAX_SHARES. Deeper quotes must not turn it into a
warehouse a raider can buy a takeover stake out of (takeover is 51%; see
the takeover math below).

The anchor is smoothed because raw NAV drops whenever a hauler's cargo is
in flight (escrowed, marked at zero) and recovers when it docks; quoting
raw NAV would pay anyone who times other fleets' trips. VOL is the dial for
how wild stock prices are. The noise is drawn from a generator seeded by
the game seed, so a seeded game is reproducible.

Event shocks (#151, Ryan 2026-09-23 10:28): news about a corp moves its
stock. When an event becomes known (agora/events.py: recorded public, or a
private/secret one exposed) the reference price of the stock in SHOCKS
jumps once, multiplicatively, and then fades through REVERSION like any
other move. NAV is not touched: a cargo loss already lowers NAV, so the
shock is the market's reaction on top, not a second cut. Private and
secret events move nothing until exposed. The jump lands on `ref` at
once and shows in the quotes at the next round's refresh. No randomness:
a seeded game shocks the same way every time.

Takeover math: rivals start with 300 shares of a corp (3 x 100). A raider
can reach its own 100 + 200 bought from rivals + whatever the exchange
holds, so SHARES is capped at MAX_SHARES = 200 to keep 51% (510) out of
reach without distress sales.
"""

import math
import os
import random
from typing import Any, Dict, List, Optional

from agora.order_book import Order

EXCHANGE_ID = 'depot_exchange'
EXCHANGE_STATION = 'ceres'
MAX_SHARES = 200
SEED_CR = 100_000
ANCHOR_ROUNDS = 20
REVERSION = 0.1
# 0.03 until #162: the sweep found a stock trader needs 0.12 to earn what the
# other play styles do over 300 rounds (0.06: ~+60k, 0.09: ~+85k, 0.12: ~+110k).
DEFAULT_VOL = 0.12
DEFAULT_SPREAD = 0.03
DEFAULT_DEPTH = 20
# Volume-scaled depth (#187 track 1): see the module docstring.
MIN_DEPTH = DEFAULT_DEPTH
MAX_DEPTH = 80
VOLUME_ROUNDS = 20
VOLUME_K = 1.0
# Periodic liquidity replenishment from SYSTEM to prevent cash depletion (#179)
REPLENISH_ROUNDS = 100
REPLENISH_FLOOR = 25_000
# Price impact (Ryan, #agent-chat 2026-09-23 08:26: "buying the stock should
# make the price go up"): each share the exchange sold since last round
# lifts its price by IMPACT / DEPTH, each share it bought lowers it. A full
# side of DEPTH shares moves the price IMPACT (5%). The move persists and
# decays only through reversion toward NAV.
IMPACT = 0.05

def _env_exchange_momentum() -> float:
    try:
        v = os.environ.get("AGORA_EXCHANGE_MOMENTUM", "")
        return float(v) if v else 0.0
    except ValueError:
        return 0.0

DEFAULT_MOMENTUM = 0.0

# Event kind -> (whose stock moves: 'actor' or 'victim', fractional jump).
# Starting values from #151 (Amos's comment, settled with Zero 10:33) and #124.
SHOCKS: Dict[str, tuple] = {
    'upgrade':              ('actor', 0.02),
    'escort':               ('actor', 0.01),
    'raid_repelled':        ('victim', 0.01),
    'contract_lapse':       ('actor', -0.03),
    'privateer_contract':   ('actor', -0.08),   # fires only on exposure
    'sabotage':             ('actor', -0.08),   # fires only on exposure
    'stake_20':             ('victim', 0.03),   # takeover premium
    'deregulation_enacted': ('actor', 0.03), # planetary council lobbying shock (#134)
}

EXTENDED_SHOCKS: Dict[str, tuple] = {
    'contract_win':         ('actor', 0.03),
    'contract_fulfillment': ('actor', 0.03),
    'contract_failure':     ('actor', -0.03),
    'distress_beacon':      ('actor', -0.05),
    'distress':             ('actor', -0.05),
    'loan_default':         ('actor', -0.06),
    'debt_default':         ('actor', -0.06),
}
# Cargo lost to a hazard or pirates: LOSS_PER of the price per LOSS_UNIT CR
# lost (valued at agora.piracy.REF_PRICE), capped at LOSS_CAP.
LOSS_KINDS = ('hazard_loss', 'pirate_loss')
LOSS_PER, LOSS_UNIT, LOSS_CAP = -0.01, 10_000, -0.05


def shock_for(ev: Dict[str, Any], include_extended: bool = False) -> Optional[tuple]:
    """(agent whose stock moves, fractional jump) for a known event, or None."""
    kind = ev.get('kind')
    if kind in LOSS_KINDS:
        lost = max(0, int(ev.get('amount') or 0))
        pct = max(LOSS_CAP, LOSS_PER * lost / LOSS_UNIT)
        return (ev.get('victim'), pct) if pct else None
    table = dict(SHOCKS)
    if include_extended:
        table.update(EXTENDED_SHOCKS)
    if kind not in table:
        return None
    who, pct = table[kind]
    agent = ev.get(who)
    return (agent, pct) if agent else None


def clamp_shares(n: Any) -> int:
    try:
        return max(0, min(MAX_SHARES, int(n)))
    except (TypeError, ValueError):
        return 0


class EquityExchange:
    def __init__(self, ref, vol: float = DEFAULT_VOL, spread: float = DEFAULT_SPREAD,
                 depth: int = DEFAULT_DEPTH, seed: int = 0, momentum: float = DEFAULT_MOMENTUM, extended_shocks: bool = False):
        self.ref = ref
        self.vol = max(0.0, float(vol))
        self.spread = min(0.5, max(0.005, float(spread)))
        self.depth = max(1, int(depth))
        self.momentum = max(0.0, min(float(momentum), 0.5))
        self.extended_shocks = bool(extended_shocks)
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.rng = random.Random(f"exchange-{seed}")
        self.navs: Dict[str, List[float]] = {}
        self.price: Dict[str, float] = {}
        self.prev_price: Dict[str, float] = {}
        self.held: Dict[str, int] = {}
        # Shares of each stock traded on the exchange book: this round so
        # far, and the last VOLUME_ROUNDS closed rounds (#187).
        self.volume_now: Dict[str, int] = {}
        self.volumes: Dict[str, List[int]] = {}
        # Depth quoted at the last refresh, per stock.
        self.depths: Dict[str, int] = {}
        self.shocks: List[Dict[str, Any]] = []

    # ------------------------------------------------------------ genesis

    @staticmethod
    def genesis_cr_legs() -> List[tuple]:
        return [(EXCHANGE_ID, 'CR', SEED_CR), ('SYSTEM', 'CR', -SEED_CR)]

    # ------------------------------------------------------------ volume

    def note_trade(self, station_id: str, instrument: str, qty: int) -> None:
        """Every executed trade reports here (CircuitBreakerEngine.record_trade,
        the one call both continuous matching and auction uncrosses make).
        Only fleet stocks on the exchange book count."""
        if str(station_id).lower() != EXCHANGE_STATION or not str(instrument).upper().startswith('EQ_'):
            return
        sym = str(instrument).upper()
        self.volume_now[sym] = self.volume_now.get(sym, 0) + max(0, int(qty))

    def depth_for(self, sym: str) -> int:
        """Shares a side to quote for sym from its trailing traded volume."""
        hist = self.volumes.get(sym, [])
        avg = sum(hist) / VOLUME_ROUNDS
        lo = max(1, min(self.depth, MAX_DEPTH))
        return max(lo, min(MAX_DEPTH, int(round(lo + VOLUME_K * avg))))

    # ------------------------------------------------------------ quoting

    def _clear_locked(self, sym: str) -> None:
        book = self.ref.books[EXCHANGE_STATION][sym]
        book.bids = [o for o in book.bids if o.agent_id != EXCHANGE_ID]
        book.asks = [o for o in book.asks if o.agent_id != EXCHANGE_ID]
        self.ref.conn.execute("DELETE FROM orders WHERE agent_id = ? AND instrument = ? AND status = 'open'",
                              (EXCHANGE_ID, sym))

    def _replenish_locked(self, round_num: int) -> int:
        """Replenish exchange cash from SYSTEM if below REPLENISH_FLOOR or periodically (#179)."""
        ref = self.ref
        cr = max(0, ref.get_balance(EXCHANGE_ID, 'CR'))
        if round_num <= 0:
            return 0
        if cr >= REPLENISH_FLOOR:
            return 0
        delta = SEED_CR - cr
        if delta <= 0:
            return 0
        seq = ref._get_next_seq() if hasattr(ref, '_get_next_seq') else ref.current_seq + 1
        txn_id = f"exchange-replenish-r{round_num}"
        for acct, d in ((EXCHANGE_ID, delta), ('SYSTEM', -delta)):
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, 'CR', 0)", (acct,))
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'", (d, acct))
            ref.conn.execute("INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                             (txn_id, seq, acct, d))
        return delta

    def refresh_locked(self) -> None:
        """Called under ref.lock inside a transaction, once a round."""
        ref = self.ref
        round_num = ref.current_round
        self._replenish_locked(round_num)
        base = {b['agent_id']: b['net_worth'] - b.get('stocks_value', 0) for b in ref.get_leaderboard()}
        marks = ref.stock_marks(base)
        cr_budget = max(0, ref.get_balance(EXCHANGE_ID, 'CR'))
        sym_list = sorted(marks)
        for i, sym in enumerate(sym_list):
            nav = float(marks[sym]['nav'])
            hist = self.navs.setdefault(sym, [])
            hist.append(nav)
            del hist[:-ANCHOR_ROUNDS]
            # A window mean lags a growing NAV by half the window, which
            # left the exchange quoting below value and paid any buyer a
            # riskless drift (#119 sweep, 2026-09-23). Carry the mean
            # forward along the window's own trend to cancel the lag.
            anchor = sum(hist) / len(hist)
            if len(hist) >= 4:
                h = len(hist) // 2
                slope = (sum(hist[-h:]) / h - sum(hist[:h]) / h) / (len(hist) - h)
                anchor += slope * (len(hist) - 1) / 2
            anchor = max(1.0, anchor)
            p = self.price.get(sym, anchor)
            prev_p = self.prev_price.get(sym, p)
            momentum_nudge = max(-p * 0.25, min(p * 0.25, self.momentum * (p - prev_p)))
            now_held = ref.get_balance(EXCHANGE_ID, sym)
            net_sold = self.held.get(sym, now_held) - now_held
            self.held[sym] = now_held
            quoted = self.depths.get(sym, self.depth)
            p = (p + REVERSION * (anchor - p) + momentum_nudge + self.vol * p * self.rng.gauss(0.0, 1.0)
                 + IMPACT * p * net_sold / quoted)
            p = max(1.0, p)
            self.prev_price[sym] = self.price.get(sym, p)
            self.price[sym] = p

            self._clear_locked(sym)
            ask = max(2, int(math.ceil(p * (1 + self.spread))))
            bid = max(1, min(ask - 1, int(math.floor(p * (1 - self.spread)))))
            vh = self.volumes.setdefault(sym, [])
            vh.append(self.volume_now.pop(sym, 0))
            del vh[:-VOLUME_ROUNDS]
            depth = self.depth_for(sym)
            self.depths[sym] = depth
            ask_qty = min(depth, max(0, now_held))
            # Allocate remaining budget evenly among remaining symbols (#179)
            syms_left = len(sym_list) - i
            alloc = cr_budget // syms_left
            # Never bid past MAX_SHARES held (#187): the bid is the only way
            # shares reach the exchange, and a resting order fills at most
            # its qty, so holdings stay <= MAX_SHARES at every fill.
            room = max(0, MAX_SHARES - now_held)
            bid_qty = min(depth, alloc // bid, room)
            cr_budget -= bid_qty * bid
            book = ref.books[EXCHANGE_STATION][sym]
            seq = ref.current_seq
            for side, price, qty in (('bid', bid, bid_qty), ('ask', ask, ask_qty)):
                if qty <= 0:
                    continue
                oid = f"{EXCHANGE_ID}-{sym.lower()}-{side}-r{round_num}"
                order = Order(order_id=oid, agent_id=EXCHANGE_ID, instrument=sym, side=side,
                              qty=qty, limit_price=price, seq_seen=seq)
                if side == 'bid':
                    book._insert_bid(order)
                else:
                    book._insert_ask(order)
                ref.conn.execute("""
                    INSERT OR REPLACE INTO orders (order_id, agent_id, instrument, side, qty, limit_price, seq_seen,
                                                  status, resolved_seq, filled_qty, station_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, 0, ?)
                """, (oid, EXCHANGE_ID, sym, side, qty, price, seq, EXCHANGE_STATION))

    # ------------------------------------------------------------ shocks

    def event_shock_locked(self, ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply the one-off jump for an event that just became known.
        No-op when the exchange is not quoting that stock."""
        from agora.equity import FLEET_EQUITIES
        s = shock_for(ev, include_extended=self.extended_shocks)
        if not s:
            return None
        agent, pct = s
        sym = FLEET_EQUITIES.get(agent, {}).get('symbol')
        if not sym or sym not in self.price:
            return None
        before = self.price[sym]
        self.price[sym] = max(1.0, before * (1 + pct))
        rec = {'round': self.ref.current_round, 'symbol': sym, 'kind': ev.get('kind'), 'event_id': ev.get('id'),
               'pct': pct, 'before': round(before, 2), 'after': round(self.price[sym], 2)}
        self.shocks.append(rec)
        del self.shocks[:-200]
        return rec

    def summary(self) -> Dict[str, Any]:
        ref = self.ref
        return {'account': EXCHANGE_ID, 'vol': self.vol, 'spread': self.spread, 'depth': self.depth,
                'max_depth': MAX_DEPTH, 'depths': dict(sorted(self.depths.items())),
                'cr': ref.get_balance(EXCHANGE_ID, 'CR'),
                'reference_prices': {s: round(p, 2) for s, p in sorted(self.price.items())},
                'recent_shocks': self.shocks[-10:]}
