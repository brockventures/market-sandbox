"""
agora/corporate.py - corporate debt, distress sales, bankruptcy and hostile
takeovers (#116, #117). Live port of the simulator's Corporate class (#112),
which Ryan asked to push to the real game (#agent-chat 2026-09-23 10:04).

- Debt. What a corp owes beyond its cash. Today the source is a contract
  penalty it could not pay (agora/contracts.py records the shortfall and
  hands it here). Each round the corp's CR pays it down first.
- Distress. A corp still in debt sells goods to the local depot at the
  depot's bid (only when docked), then auctions its own treasury shares:
  at most AUCTION_CAP a round, at DISCOUNT x the better of NAV, the board
  mark and PRICE_FLOOR, split between the rivals in proportion to their
  cash (the rule the simulator used; highest-bidder-takes-all was the open
  alternative in #116). Loose treasury shares are what make takeovers
  possible: at genesis rivals hold only 30-40%.
- Bankruptcy. Still in debt after BANKRUPT_ROUNDS rounds with no treasury
  shares left: the corp is out. Its orders are cancelled, its balances go to
  its creditors (SYSTEM), its contracts return to unclaimed.
- Takeover. A rival holding TAKEOVER_SHARES (51%) of a corp's stock absorbs
  it: every balance, open contract and debt passes to the raider, and the
  target is out. The last corp standing wins the game.

An out corp can no longer trade, move, claim or offer. Every move is a
balanced ledger entry. Off unless the referee's corporate flag is set
(new_game or AGORA_CORPORATE=1).
"""

import math
import os
from typing import Any, Dict, List, Optional

AUCTION_CAP = 100
DISCOUNT = 0.7
PRICE_FLOOR = 10
BANKRUPT_ROUNDS = 10
TAKEOVER_SHARES = 510


def env_corporate() -> bool:
    return os.environ.get("AGORA_CORPORATE", "").strip().lower() in ("1", "true", "yes", "on")


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS corp_status (
        agent_id        TEXT PRIMARY KEY,
        debt            INTEGER NOT NULL DEFAULT 0,
        rounds_in_debt  INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'bankrupt', 'absorbed')),
        absorbed_by     TEXT,
        out_round       INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS corp_events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        round     INTEGER NOT NULL,
        kind      TEXT NOT NULL,
        agent_id  TEXT NOT NULL,
        detail    TEXT NOT NULL
    )""",
]


class CorporateDesk:
    def __init__(self, ref):
        self.ref = ref
        with ref.conn:
            for q in SCHEMA:
                ref.conn.execute(q)

    # ------------------------------------------------------------ helpers

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

    def _fleets(self) -> List[str]:
        return [r[0] for r in self.ref.conn.execute("SELECT agent_id FROM fleet_roster ORDER BY agent_id")]

    def _row(self, agent: str) -> Dict[str, Any]:
        self.ref.conn.execute("INSERT OR IGNORE INTO corp_status (agent_id) VALUES (?)", (agent,))
        return dict(self.ref.conn.execute("SELECT * FROM corp_status WHERE agent_id = ?", (agent,)).fetchone())

    def _set(self, agent: str, **kw) -> None:
        self._row(agent)
        cols = ", ".join(f"{k} = ?" for k in kw)
        self.ref.conn.execute(f"UPDATE corp_status SET {cols} WHERE agent_id = ?", (*kw.values(), agent))

    def _event(self, kind: str, agent: str, detail: str) -> None:
        self.ref.conn.execute("INSERT INTO corp_events (round, kind, agent_id, detail) VALUES (?, ?, ?, ?)",
                              (self.ref.current_round, kind, agent, detail))

    def _sym(self, agent: str) -> Optional[str]:
        from agora.equity import FLEET_EQUITIES
        return FLEET_EQUITIES.get(agent, {}).get("symbol")

    def _cancel_all(self, agent: str) -> None:
        for books in self.ref.books.values():
            for b in books.values():
                for o in list(b.bids) + list(b.asks):
                    if o.agent_id == agent:
                        self.ref._cancel_order_locked(agent, o.order_id)

    def _sweep(self, src: str, dst: str, txn: str) -> None:
        legs = []
        for r in self.ref.conn.execute("SELECT instrument, balance FROM accounts WHERE agent_id = ? AND balance != 0",
                                       (src,)).fetchall():
            legs += [(src, r["instrument"], -r["balance"]), (dst, r["instrument"], r["balance"])]
        self._move(txn, legs)

    # ------------------------------------------------------------ reads

    def status(self, agent: str) -> str:
        r = self.ref.conn.execute("SELECT status FROM corp_status WHERE agent_id = ?", (agent,)).fetchone()
        return r["status"] if r else "active"

    def out_reason(self, agent: str) -> Optional[str]:
        r = self.ref.conn.execute("SELECT status, absorbed_by, out_round FROM corp_status WHERE agent_id = ?",
                                  (agent,)).fetchone()
        if not r or r["status"] == "active":
            return None
        if r["status"] == "absorbed":
            return f"{agent} was taken over by {r['absorbed_by']} in round {r['out_round']} and can no longer act"
        return f"{agent} went bankrupt in round {r['out_round']} and can no longer act"

    def active(self) -> List[str]:
        return [a for a in self._fleets() if self.status(a) == "active"]

    def summary(self) -> Dict[str, Any]:
        rows = {a: self._row(a) for a in self._fleets()}
        act = [a for a, r in rows.items() if r["status"] == "active"]
        return {
            "corps": rows,
            "winner": act[0] if len(act) == 1 and len(rows) > 1 else None,
            "events": [dict(e) for e in self.ref.conn.execute(
                "SELECT round, kind, agent_id, detail FROM corp_events ORDER BY id DESC LIMIT 20")],
        }

    # ------------------------------------------------------------ debt

    def add_debt(self, agent: str, amount: int, why: str) -> None:
        """Caller holds ref.lock inside a transaction."""
        if amount <= 0 or self.status(agent) != "active":
            return
        r = self._row(agent)
        self._set(agent, debt=r["debt"] + int(amount))
        self._event("debt", agent, f"owes {int(amount)} CR more ({why}); total {r['debt'] + int(amount)}")

    def _pay_down(self, agent: str) -> int:
        r = self._row(agent)
        pay = min(r["debt"], max(0, self.ref.get_balance(agent, "CR")))
        if pay > 0:
            self._move(f"debt-pay-{agent}-{self.ref.current_round}-{r['debt']}", ((agent, "CR", -pay), ("SYSTEM", "CR", pay)))
            self._set(agent, debt=r["debt"] - pay)
        return r["debt"] - pay

    def _sell_goods_to_depot(self, agent: str) -> None:
        ref = self.ref
        loc = ref.get_vessel_location(agent)
        if loc.get("status") != "docked":
            return
        st = loc.get("station_id")
        depot = f"depot_{st}"
        for comm in ("FRAG", "FOOD", "ORE"):
            debt = self._row(agent)["debt"]
            have = ref.peer._available(agent, comm)
            book = ref.books.get(st, {}).get(comm)
            bids = sorted((o for o in (book.bids if book else []) if o.agent_id == depot and o.remaining_qty > 0),
                          key=lambda o: -o.limit_price)
            if debt <= 0 or have <= 0 or not bids:
                continue
            o = bids[0]
            qty = min(have, o.remaining_qty, math.ceil(debt / o.limit_price),
                      max(0, ref.get_balance(depot, "CR")) // o.limit_price)
            if qty <= 0:
                continue
            self._move(f"distress-goods-{agent}-{comm}-{ref.current_round}", (
                (agent, comm, -qty), (depot, comm, qty), (depot, "CR", -qty * o.limit_price), (agent, "CR", qty * o.limit_price)))
            # Consume the depot's resting bid so the same depth isn't sold twice.
            o.filled_qty += qty
            ref.conn.execute("UPDATE orders SET filled_qty = filled_qty + ? WHERE order_id = ?", (qty, o.order_id))
            if o.remaining_qty <= 0:
                book.bids.remove(o)
            self._event("distress_sale", agent, f"sold {qty} {comm} to the {st} depot at {o.limit_price}")
            self._pay_down(agent)

    def _auction_treasury(self, agent: str) -> None:
        ref = self.ref
        sym = self._sym(agent)
        debt = self._row(agent)["debt"]
        if not sym or debt <= 0:
            return
        base = {e["agent_id"]: e["net_worth"] - e.get("stocks_value", 0) for e in ref.get_leaderboard()}
        m = ref.stock_marks(base)[sym]
        px = max(1, int(max(m["nav"], m["mark"], PRICE_FLOOR) * DISCOUNT))
        n = min(ref.get_balance(agent, sym), -(-debt // px), AUCTION_CAP)
        rivals = [(max(0, ref.peer._available(b, "CR")), b) for b in self.active() if b != agent]
        total = sum(c for c, _ in rivals) or 1
        sold = []
        for cash, b in sorted(rivals, reverse=True):
            if n <= 0:
                break
            k = min(n, cash // px, max(1, round(AUCTION_CAP * cash / total)))
            if k <= 0:
                continue
            self._move(f"distress-auction-{sym}-{b}-{ref.current_round}",
                       ((agent, sym, -k), (b, sym, k), (b, "CR", -k * px), (agent, "CR", k * px)))
            sold.append(f"{k} to {b}")
            n -= k
        if sold:
            self._event("share_auction", agent, f"auctioned {sym} at {px}: " + ", ".join(sold))
            self._pay_down(agent)

    def _bankrupt(self, agent: str) -> None:
        ref = self.ref
        self._cancel_all(agent)
        self._sweep(agent, "SYSTEM", f"bankrupt-{agent}-{ref.current_round}")
        ref.conn.execute("UPDATE station_contracts SET owner = NULL, bond = 0, list_price = NULL "
                         "WHERE owner = ? AND status = 'open'", (agent,))
        debt = self._row(agent)["debt"]
        self._set(agent, status="bankrupt", out_round=ref.current_round, debt=0)
        self._event("bankrupt", agent, f"bankrupt with {debt} CR unpaid; assets seized")

    def _takeovers(self) -> None:
        ref = self.ref
        for target in self.active():
            sym = self._sym(target)
            if not sym:
                continue
            for raider in self.active():
                if raider == target or self.status(target) != "active":
                    continue
                if ref.get_balance(raider, sym) < TAKEOVER_SHARES:
                    continue
                self._cancel_all(target)
                self._sweep(target, raider, f"takeover-{raider}-{target}-{ref.current_round}")
                ref.conn.execute("UPDATE station_contracts SET owner = ?, list_price = NULL "
                                 "WHERE owner = ? AND status = 'open'", (raider, target))
                debt = self._row(target)["debt"]
                if debt:
                    r = self._row(raider)
                    self._set(raider, debt=r["debt"] + debt)
                self._set(target, status="absorbed", absorbed_by=raider, out_round=ref.current_round, debt=0)
                self._event("takeover", raider, f"took over {target} holding {ref.get_balance(raider, sym)} "
                                                f"{sym}; absorbed its assets" + (f" and {debt} CR of debt" if debt else ""))
        act = self.active()
        if len(act) == 1 and len(self._fleets()) > 1:
            if not self.ref.conn.execute("SELECT 1 FROM corp_events WHERE kind = 'winner'").fetchone():
                self._event("winner", act[0], f"{act[0]} is the last corp standing and wins the game")

    # ------------------------------------------------------------ rounds

    def step_locked(self, round_num: int) -> Dict[str, Any]:
        """Called from step_round under ref.lock inside its transaction."""
        for agent in self.active():
            if self._pay_down(agent) <= 0:
                self._set(agent, rounds_in_debt=0)
                continue
            self._sell_goods_to_depot(agent)
            self._auction_treasury(agent)
            r = self._row(agent)
            if r["debt"] <= 0:
                self._set(agent, rounds_in_debt=0)
                continue
            self._set(agent, rounds_in_debt=r["rounds_in_debt"] + 1)
            sym = self._sym(agent)
            if (not sym or self.ref.get_balance(agent, sym) <= 0) and r["rounds_in_debt"] + 1 >= BANKRUPT_ROUNDS:
                self._bankrupt(agent)
        self._takeovers()
        return {"active": self.active()}
