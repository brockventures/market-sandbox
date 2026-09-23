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
# Periodic liquidity replenishment from SYSTEM to prevent cash depletion (#179)
REPLENISH_ROUNDS = 100
REPLENISH_FLOOR = 25_000
# Price impact (Ryan, #agent-chat 2026-09-23 08:26: "buying the stock should
# make the price go up"): each share the exchange sold since last round
# lifts its price by IMPACT / DEPTH, each share it bought lowers it. A full
# side of DEPTH shares moves the price IMPACT (5%). The move persists and
# decays only through reversion toward NAV.
IMPACT = 0.05

# Event kind -> (whose stock moves: 'actor' or 'victim', fractional jump).
# Starting values from #151 (Amos's comment, settled with Zero 10:33).
SHOCKS: Dict[str, tuple] = {
    'upgrade':            ('actor', 0.02),
    'escort':             ('actor', 0.01),
    'raid_repelled':      ('victim', 0.01),
    'contract_lapse':     ('actor', -0.03),
    'privateer_contract': ('actor', -0.08),   # fires only on exposure
    'sabotage':           ('actor', -0.08),   # fires only on exposure
    'stake_20':           ('victim', 0.03),   # takeover premium
}
# Cargo lost to a hazard or pirates: LOSS_PER of the price per LOSS_UNIT CR
# lost (valued at agora.piracy.REF_PRICE), capped at LOSS_CAP.
LOSS_KINDS = ('hazard_loss', 'pirate_loss')
LOSS_PER, LOSS_UNIT, LOSS_CAP = -0.01, 10_000, -0.05


def shock_for(ev: Dict[str, Any]) -> Optional[tuple]:
    """(agent whose stock moves, fractional jump) for a known event, or None."""
    kind = ev.get('kind')
    if kind in LOSS_KINDS:
        lost = max(0, int(ev.get('amount') or 0))
        pct = max(LOSS_CAP, LOSS_PER * lost / LOSS_UNIT)
        return (ev.get('victim'), pct) if pct else None
    if kind not in SHOCKS:
        return None
    who, pct = SHOCKS[kind]
    agent = ev.get(who)
    return (agent, pct) if agent else None


def clamp_shares(n: Any) -> int:
    try:
        return max(0, min(MAX_SHARES, int(n)))
    except (TypeError, ValueError):
        return 0


class EquityExchange:
    def __init__(self, ref, vol: float = DEFAULT_VOL, spread: float = DEFAULT_SPREAD,
                 depth: int = DEFAULT_DEPTH, seed: int = 0):
        self.ref = ref
        self.vol = max(0.0, float(vol))
        self.spread = min(0.5, max(0.005, float(spread)))
        self.depth = max(1, int(depth))
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.rng = random.Random(f"exchange-{seed}")
        self.navs: Dict[str, List[float]] = {}
        self.price: Dict[str, float] = {}
        self.held: Dict[str, int] = {}
        self.shocks: List[Dict[str, Any]] = []

    # ------------------------------------------------------------ genesis

    @staticmethod
    def genesis_cr_legs() -> List[tuple]:
        return [(EXCHANGE_ID, 'CR', SEED_CR), ('SYSTEM', 'CR', -SEED_CR)]

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
            now_held = ref.get_balance(EXCHANGE_ID, sym)
            net_sold = self.held.get(sym, now_held) - now_held
            self.held[sym] = now_held
            p = (p + REVERSION * (anchor - p) + self.vol * p * self.rng.gauss(0.0, 1.0)
                 + IMPACT * p * net_sold / self.depth)
            p = max(1.0, p)
            self.price[sym] = p

            self._clear_locked(sym)
            ask = max(2, int(math.ceil(p * (1 + self.spread))))
            bid = max(1, min(ask - 1, int(math.floor(p * (1 - self.spread)))))
            ask_qty = min(self.depth, max(0, ref.get_balance(EXCHANGE_ID, sym)))
            # Allocate remaining budget evenly among remaining symbols (#179)
            syms_left = len(sym_list) - i
            alloc = cr_budget // syms_left
            bid_qty = min(self.depth, alloc // bid)
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
        s = shock_for(ev)
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
                'cr': ref.get_balance(EXCHANGE_ID, 'CR'),
                'reference_prices': {s: round(p, 2) for s, p in sorted(self.price.items())},
                'recent_shocks': self.shocks[-10:]}
