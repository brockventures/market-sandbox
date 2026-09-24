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
TAKEOVER_SHARES = 501  # baseline 50.1% threshold on 1,000 genesis shares; dynamically (live_shares // 2) + 1 (#164)


def env_corporate() -> bool:
    return os.environ.get("AGORA_CORPORATE", "").strip().lower() in ("1", "true", "yes", "on")


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {"v": 1, "kind": "reject", "payload": {"reason": reason, "detail": detail}}


def _safe_int(val: Any, name: str, min_val: Optional[int] = None, max_val: int = 2**62) -> int:
    if val is None:
        raise ValueError(f"{name} is required")
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ValueError(f"{name} must be a finite number")
    try:
        res = int(val)
    except (ValueError, TypeError, OverflowError) as e:
        raise ValueError(f"{name} must be a valid integer: {e}")
    if abs(res) > max_val:
        raise ValueError(f"{name} exceeds maximum allowed value")
    if min_val is not None and res < min_val:
        raise ValueError(f"{name} must be at least {min_val}")
    return res


def _safe_float(val: Any, name: str, min_val: Optional[float] = None, max_val: Optional[float] = None) -> float:
    if val is None:
        raise ValueError(f"{name} is required")
    try:
        res = float(val)
    except (ValueError, TypeError, OverflowError) as e:
        raise ValueError(f"{name} must be a valid number: {e}")
    if math.isnan(res) or math.isinf(res):
        raise ValueError(f"{name} must be a finite number")
    if min_val is not None and res < min_val:
        raise ValueError(f"{name} must be at least {min_val}")
    if max_val is not None and res > max_val:
        raise ValueError(f"{name} must be at most {max_val}")
    return res


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS corp_status (
        agent_id        TEXT PRIMARY KEY,
        debt            INTEGER NOT NULL DEFAULT 0,
        rounds_in_debt  INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'bankrupt', 'absorbed')),
        absorbed_by     TEXT,
        out_round       INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS corp_tender_offers (
        offer_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        raider          TEXT NOT NULL,
        target          TEXT NOT NULL,
        price           INTEGER NOT NULL,
        shares_wanted   INTEGER NOT NULL,
        shares_filled   INTEGER NOT NULL DEFAULT 0,
        escrow_cr       INTEGER NOT NULL,
        created_round   INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'filled', 'cancelled'))
    )""",
    """CREATE TABLE IF NOT EXISTS corp_poison_pills (
        pill_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        target          TEXT NOT NULL,
        trigger_raider  TEXT NOT NULL,
        activated_round INTEGER NOT NULL,
        rights_price    INTEGER NOT NULL,
        rights_issued   INTEGER NOT NULL,
        rights_exercised INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired'))
    )""",
    """CREATE TABLE IF NOT EXISTS corp_loan_offers (
        offer_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        lender          TEXT NOT NULL,
        borrower        TEXT NOT NULL,
        principal       INTEGER NOT NULL,
        interest_rate   REAL NOT NULL,
        due_amount      INTEGER NOT NULL,
        due_rounds      INTEGER NOT NULL,
        created_round   INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'cancelled'))
    )""",
    """CREATE TABLE IF NOT EXISTS corp_predatory_loans (
        loan_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        lender          TEXT NOT NULL,
        borrower        TEXT NOT NULL,
        principal       INTEGER NOT NULL,
        due_amount      INTEGER NOT NULL,
        due_round       INTEGER NOT NULL,
        collateral_sym  TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'repaid', 'defaulted'))
    )""",
]


class CorporateDesk:
    def __init__(self, ref):
        self.ref = ref
        with ref.conn:
            try:
                cols = [c[1] for c in ref.conn.execute("PRAGMA table_info(corp_poison_pills)").fetchall()]
                if cols and "pill_id" not in cols:
                    ref.conn.execute("ALTER TABLE corp_poison_pills RENAME TO corp_poison_pills_old")
                    ref.conn.execute(SCHEMA[2])
                    ref.conn.execute("INSERT INTO corp_poison_pills (target, trigger_raider, activated_round, rights_price, rights_issued, rights_exercised, status) "
                                     "SELECT target, trigger_raider, activated_round, rights_price, rights_issued, rights_exercised, status FROM corp_poison_pills_old")
                    ref.conn.execute("DROP TABLE corp_poison_pills_old")
            except Exception:
                pass
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

    def _event(self, kind: str, agent: str, detail: str, victim: Optional[str] = None) -> None:
        """A public entry in the shared corp_events log (agora/events.py)."""
        self.ref.events.record_locked(kind, 'public', actor=agent, victim=victim, detail=detail, agent_id=agent)

    def _sym(self, agent: str) -> Optional[str]:
        from agora.equity import FLEET_EQUITIES
        return FLEET_EQUITIES.get(agent, {}).get("symbol")

    def _resolve_creditor(self, agent: str) -> str:
        """Resolve the active recipient for payments owed to an agent.
        If agent is active: returns agent.
        If agent was absorbed: follows absorption chain to active raider.
        If agent is bankrupt or has no active successor: returns SYSTEM."""
        curr = agent
        seen = set()
        while curr and curr not in seen:
            seen.add(curr)
            r = self._row(curr)
            if r.get('status') == 'active':
                return curr
            if r.get('status') == 'absorbed' and r.get('absorbed_by'):
                curr = r['absorbed_by']
            else:
                return 'SYSTEM'
        return 'SYSTEM'

    def _cancel_all(self, agent: str) -> None:
        for books in self.ref.books.values():
            for b in books.values():
                for o in list(b.bids) + list(b.asks):
                    if o.agent_id == agent:
                        self.ref._cancel_order_locked(agent, o.order_id)
        # Cancel any open tender offers from agent or targeting agent
        for off in self.ref.conn.execute("SELECT offer_id FROM corp_tender_offers WHERE raider = ? AND status = 'open'", (agent,)).fetchall():
            self._cancel_tender_offer_locked(agent, off['offer_id'])
        for off in self.ref.conn.execute("SELECT offer_id, raider FROM corp_tender_offers WHERE target = ? AND status = 'open'", (agent,)).fetchall():
            self._cancel_tender_offer_locked(off['raider'], off['offer_id'])
        # Cancel any open loan offers where agent is lender OR borrower
        for off in self.ref.conn.execute("SELECT offer_id FROM corp_loan_offers WHERE lender = ? AND status = 'open'", (agent,)).fetchall():
            self._cancel_loan_offer_locked(agent, off['offer_id'])
        for off in self.ref.conn.execute("SELECT offer_id, lender FROM corp_loan_offers WHERE borrower = ? AND status = 'open'", (agent,)).fetchall():
            self._cancel_loan_offer_locked(off['lender'], off['offer_id'])

    def _settle_transits(self, src: str, dst: str) -> None:
        """Cargo still in flight for a corp that is out. Reassigning the trip
        would move `dst`'s own ship when it lands, so cancel it instead and
        hand the escrowed cargo (held by SYSTEM) straight to `dst`: the raider
        on a takeover, SYSTEM (i.e. nothing moves) on bankruptcy.
        Zero's review of #148."""
        for t in self.ref.conn.execute("SELECT transit_id, vessel_id, origin, commodity, cargo_qty FROM transits "
                                       "WHERE agent_id = ? AND status = 'in_transit'", (src,)).fetchall():
            if t["cargo_qty"] and dst != "SYSTEM":
                self._move(f"out-transit-{t['transit_id']}", (("SYSTEM", t["commodity"], -t["cargo_qty"]),
                                                             (dst, t["commodity"], t["cargo_qty"])))
            self.ref.conn.execute("UPDATE transits SET status = 'cancelled' WHERE transit_id = ?", (t["transit_id"],))
            # The trip never lands, so the ship goes back to where it left
            # from rather than staying 'in_transit' forever (#198, same choice
            # and reasoning as a salvage claim in agora/salvage.py).
            self.ref._dock_vessel_locked(src, t["origin"], t["vessel_id"])

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

    def live_shares(self, sym: str) -> int:
        if hasattr(self.ref, 'get_live_shares'):
            return self.ref.get_live_shares(sym)
        return 1000

    def takeover_threshold(self, target: str) -> int:
        sym = self._sym(target)
        if not sym:
            return TAKEOVER_SHARES
        return (self.live_shares(sym) // 2) + 1

    def summary(self) -> Dict[str, Any]:
        rows = {a: self._row(a) for a in self._fleets()}
        act = [a for a, r in rows.items() if r["status"] == "active"]
        win_ev = self.ref.conn.execute("SELECT actor, detail FROM corp_events WHERE kind = 'winner'").fetchone()
        winner = win_ev["actor"] if win_ev else (act[0] if len(act) == 1 and len(rows) > 1 else None)
        offers = [dict(r) for r in self.ref.conn.execute("SELECT * FROM corp_tender_offers WHERE status = 'open'").fetchall()]
        loan_offers = [dict(r) for r in self.ref.conn.execute("SELECT * FROM corp_loan_offers WHERE status = 'open'").fetchall()]
        pills = [dict(r) for r in self.ref.conn.execute("SELECT * FROM corp_poison_pills WHERE status = 'active'").fetchall()]
        loans = [dict(r) for r in self.ref.conn.execute("SELECT * FROM corp_predatory_loans WHERE status = 'active'").fetchall()]
        return {
            "corps": rows,
            "winner": winner,
            "win_reason": win_ev["detail"] if win_ev else ("last corp standing" if winner else None),
            "tender_offers": offers,
            "loan_offers": loan_offers,
            "poison_pills": pills,
            "predatory_loans": loans,
            # Public and exposed events only: private and secret ones are
            # served per viewer by GET /referee/corporate/events (#153).
            "events": [{k: e[k] for k in ("round", "kind", "agent_id", "actor", "victim", "detail", "exposed")}
                       for e in self.ref.events.known(limit=20)],
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
        self._settle_transits(agent, "SYSTEM")
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
            threshold = self.takeover_threshold(target)
            for raider in self.active():
                if raider == target or self.status(target) != "active":
                    continue
                if ref.get_balance(raider, sym) < threshold:
                    continue
                self._cancel_all(target)
                self._settle_transits(target, raider)
                self._sweep(target, raider, f"takeover-{raider}-{target}-{ref.current_round}")
                ref.conn.execute("UPDATE station_contracts SET owner = ?, list_price = NULL "
                                 "WHERE owner = ? AND status = 'open'", (raider, target))
                debt = self._row(target)["debt"]
                if debt:
                    r = self._row(raider)
                    self._set(raider, debt=r["debt"] + debt)
                self._set(target, status="absorbed", absorbed_by=raider, out_round=ref.current_round, debt=0)
                self._event("takeover", raider, f"took over {target} holding {ref.get_balance(raider, sym)} "
                                                f"{sym} (threshold {threshold}); absorbed its assets" + (f" and {debt} CR of debt" if debt else ""),
                            victim=target)
        act = self.active()
        if len(act) == 1 and len(self._fleets()) > 1:
            if not self.ref.conn.execute("SELECT 1 FROM corp_events WHERE kind = 'winner'").fetchone():
                rivals = [r for r in self._fleets() if r != act[0]]
                absorbed_all = bool(rivals) and all(self.status(r) == 'absorbed' and self._row(r).get('absorbed_by') == act[0] for r in rivals)
                reason = "Corporate Monopoly (absorbed all rival corporations)" if absorbed_all else "the last corp standing"
                self._event("winner", act[0], f"{act[0]} has won the game: {reason}")
        elif len(self._fleets()) > 1 and not self.ref.conn.execute("SELECT 1 FROM corp_events WHERE kind = 'winner'").fetchone():
            # Corporate Monopoly (#164)
            # Achieving majority board control in every surviving rival
            for candidate in act:
                rivals = [r for r in act if r != candidate]
                has_monopoly = bool(rivals) and all(
                    ref.get_balance(candidate, self._sym(r) or '') >= self.takeover_threshold(r)
                    for r in rivals
                )
                if has_monopoly:
                    self._event("winner", candidate, f"{candidate} has won the game: Corporate Monopoly (majority board control in all surviving corps)")
                    break

    # ------------------------------------------------------------ Hostile M&A Levers (#164)

    def create_tender_offer(self, raider: str, target: str, price: int, shares: int) -> Dict[str, Any]:
        """Solicit direct buyout bids for rival shares at a premium above NAV or market (#164)."""
        ref = self.ref
        raider = (raider or '').strip().lower()
        target = (target or '').strip().lower()
        try:
            price = _safe_int(price, 'Price', min_val=1)
            shares = _safe_int(shares, 'Shares', min_val=1)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        if raider not in self.active():
            return _reject('invalid_raider', f"Unknown or inactive raider '{raider}'")
        if target not in self.active():
            return _reject('invalid_target', f"Unknown or inactive target '{target}'")
        if raider == target:
            return _reject('self_tender', "Cannot launch a tender offer against your own fleet")

        escrow = price * shares
        if escrow > 2**62:
            return _reject('invalid_parameters', "Tender escrow exceeds maximum allowed value")

        with ref.lock, ref.conn:
            avail = ref.peer._available(raider, 'CR') if hasattr(ref, 'peer') else ref.get_balance(raider, 'CR')
            if avail < escrow:
                return _reject('insufficient_credits', f"Tender offer requires {escrow} CR in escrow; available {avail}")

            cur = ref.conn.execute(
                "INSERT INTO corp_tender_offers (raider, target, price, shares_wanted, shares_filled, escrow_cr, created_round, status) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, 'open')",
                (raider, target, price, shares, escrow, ref.current_round))
            offer_id = cur.lastrowid
            self._move(f"tender-escrow-{offer_id}-{raider}-{target}-r{ref.current_round}",
                       ((raider, 'CR', -escrow), ('SYSTEM', 'CR', escrow)))
            self._event("tender_offer", raider,
                        f"{raider} launched hostile tender offer for {shares} shares of {target} at {price} CR/share (#{offer_id})",
                        victim=target)
            return {'v': 1, 'kind': 'tender_offer_ok', 'payload': {
                'offer_id': offer_id, 'raider': raider, 'target': target,
                'price': price, 'shares_wanted': shares, 'escrow_cr': escrow,
                'round': ref.current_round
            }}

    def accept_tender_offer(self, seller: str, offer_id: int, shares: int) -> Dict[str, Any]:
        """Tender shares to an active hostile buyout offer (#164)."""
        ref = self.ref
        seller = (seller or '').strip().lower()
        try:
            offer_id = _safe_int(offer_id, 'Offer ID', min_val=1)
            shares = _safe_int(shares, 'Shares', min_val=1)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        with ref.lock, ref.conn:
            offer = ref.conn.execute("SELECT * FROM corp_tender_offers WHERE offer_id = ? AND status = 'open'", (offer_id,)).fetchone()
            if not offer:
                return _reject('offer_not_found', f"Active tender offer #{offer_id} not found")
            offer = dict(offer)
            if seller == offer['raider']:
                return _reject('invalid_seller', "Raider cannot sell to their own tender offer")

            sym = self._sym(offer['target'])
            if not sym:
                return _reject('invalid_target', f"No equity found for target '{offer['target']}'")

            avail_shares = ref.peer._available(seller, sym) if hasattr(ref, 'peer') else ref.get_balance(seller, sym)
            if avail_shares < shares:
                return _reject('insufficient_shares', f"Need {shares} {sym}; available {avail_shares}")

            remaining = offer['shares_wanted'] - offer['shares_filled']
            if shares > remaining:
                return _reject('exceeds_offer', f"Offer only has {remaining} shares remaining")

            payout = shares * offer['price']
            new_filled = offer['shares_filled'] + shares
            remaining_after = offer['shares_wanted'] - new_filled
            new_escrow = remaining_after * offer['price']
            self._move(f"tender-fill-{offer_id}-{seller}-{new_filled}-r{ref.current_round}", (
                (seller, sym, -shares),
                (offer['raider'], sym, shares),
                ('SYSTEM', 'CR', -payout),
                (seller, 'CR', payout),
            ))
            new_status = 'filled' if remaining_after <= 0 else 'open'
            ref.conn.execute("UPDATE corp_tender_offers SET shares_filled = ?, escrow_cr = ?, status = ? WHERE offer_id = ?",
                             (new_filled, new_escrow, new_status, offer_id))
            self._event("tender_accepted", seller,
                        f"{seller} tendered {shares} shares of {sym} to {offer['raider']} at {offer['price']} CR (#{offer_id})",
                        victim=offer['target'])
            self._takeovers()
            return {'v': 1, 'kind': 'tender_accept_ok', 'payload': {
                'offer_id': offer_id, 'seller': seller, 'shares_tendered': shares,
                'payout': payout, 'remaining_shares': remaining_after,
                'status': new_status
            }}

    def _cancel_tender_offer_locked(self, caller: str, offer_id: int) -> Dict[str, Any]:
        ref = self.ref
        caller = (caller or '').strip().lower()
        try:
            offer_id = _safe_int(offer_id, 'Offer ID', min_val=1)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        offer = ref.conn.execute("SELECT * FROM corp_tender_offers WHERE offer_id = ? AND status = 'open'", (offer_id,)).fetchone()
        if not offer:
            return _reject('offer_not_found', f"Active tender offer #{offer_id} not found")
        offer = dict(offer)
        if caller != offer['raider'] and caller != 'admin':
            return _reject('unauthorized', "Only the offering raider can cancel this tender offer")

        remaining = offer['shares_wanted'] - offer['shares_filled']
        refund = remaining * offer['price']
        if refund > 0:
            recipient = self._resolve_creditor(offer['raider'])
            self._move(f"tender-refund-{offer_id}-r{ref.current_round}",
                       (('SYSTEM', 'CR', -refund), (recipient, 'CR', refund)))
        ref.conn.execute("UPDATE corp_tender_offers SET escrow_cr = 0, status = 'cancelled' WHERE offer_id = ?", (offer_id,))
        self._event("tender_cancelled", offer['raider'],
                    f"{offer['raider']} cancelled tender offer #{offer_id}; refunded {refund} CR",
                    victim=offer['target'])
        return {'v': 1, 'kind': 'tender_cancel_ok', 'payload': {'offer_id': offer_id, 'refund_cr': refund}}

    def cancel_tender_offer(self, raider: str, offer_id: int) -> Dict[str, Any]:
        """Cancel an open tender offer and refund unspent escrow (#164)."""
        with self.ref.lock, self.ref.conn:
            return self._cancel_tender_offer_locked(raider, offer_id)

    # ------------------------------------------------------------ Defensive Governance: Poison Pills (#164)

    def activate_poison_pill(self, target: str, caller: Optional[str] = None) -> Dict[str, Any]:
        """Enact a dilutive rights offering if an outside entity acquires >30% stake (#164)."""
        ref = self.ref
        target = (target or '').strip().lower()
        if caller is not None and caller not in (target, 'admin'):
            return _reject('unauthorized', f"Only target corporation '{target}' can activate its poison pill (caller '{caller}' rejected)")

        if target not in self.active():
            return _reject('invalid_target', f"Target '{target}' is not an active corporation")

        sym = self._sym(target)
        if not sym:
            return _reject('invalid_equity', f"No equity symbol for target '{target}'")

        with ref.lock, ref.conn:
            existing = ref.conn.execute("SELECT * FROM corp_poison_pills WHERE target = ? AND status = 'active'", (target,)).fetchone()
            if existing:
                return _reject('pill_already_active', f"Poison pill already active for {target}")

            live_float = self.live_shares(sym)
            raiders = []
            for fleet in self.active():
                if fleet == target:
                    continue
                stake = ref.get_balance(fleet, sym)
                if stake > int(0.30 * live_float):
                    raiders.append((fleet, stake))

            if not raiders:
                return _reject('no_hostile_stake', f"No outside rival holds >30% stake in {target} (float {live_float})")

            raiders.sort(key=lambda x: -x[1])
            trigger_raider = raiders[0][0]
            base = {e["agent_id"]: e["net_worth"] - e.get("stocks_value", 0) for e in ref.get_leaderboard()}
            marks = ref.stock_marks(base).get(sym, {})
            nav = marks.get("nav", 10.0)
            rights_price = max(1, int(round(nav * 0.50)))  # 50% discount to NAV
            rights_issued = min(500, max(50, live_float // 2))

            cur = ref.conn.execute(
                "INSERT INTO corp_poison_pills (target, trigger_raider, activated_round, rights_price, rights_issued, rights_exercised, status) "
                "VALUES (?, ?, ?, ?, ?, 0, 'active')",
                (target, trigger_raider, ref.current_round, rights_price, rights_issued))
            pill_id = cur.lastrowid

            self._event("poison_pill_activated", target,
                        f"{target} enacted Poison Pill against {trigger_raider} (holding {raiders[0][1]}/{live_float} shares). "
                        f"Issued {rights_issued} rights at {rights_price} CR/share (50% NAV discount)",
                        victim=trigger_raider)

            return {'v': 1, 'kind': 'poison_pill_ok', 'payload': {
                'pill_id': pill_id,
                'target': target, 'trigger_raider': trigger_raider,
                'rights_price': rights_price, 'rights_issued': rights_issued,
                'round': ref.current_round
            }}

    def exercise_rights(self, agent: str, target: str, qty: int) -> Dict[str, Any]:
        """Exercise defensive rights offering, minting new shares to dilute raiders (#164)."""
        ref = self.ref
        agent = (agent or '').strip().lower()
        target = (target or '').strip().lower()
        try:
            qty = _safe_int(qty, 'Rights quantity', min_val=1)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        with ref.lock, ref.conn:
            pill = ref.conn.execute(
                "SELECT * FROM corp_poison_pills WHERE target = ? AND status = 'active' ORDER BY pill_id DESC",
                (target,)).fetchone()
            if not pill:
                return _reject('no_active_pill', f"No active poison pill rights offering for '{target}'")
            pill = dict(pill)
            pill_id = pill.get('pill_id', 0)

            if agent == target:
                return _reject('target_excluded', f"Target corporation '{target}' cannot purchase its own discounted rights")

            if agent == pill['trigger_raider']:
                return _reject('raider_excluded', f"Hostile raider {agent} is barred from participating in {target}'s rights offering")

            remaining = pill['rights_issued'] - pill['rights_exercised']
            if qty > remaining:
                return _reject('exceeds_rights', f"Only {remaining} rights remaining in offering")

            cost = qty * pill['rights_price']
            avail_cr = ref.peer._available(agent, 'CR') if hasattr(ref, 'peer') else ref.get_balance(agent, 'CR')
            if avail_cr < cost:
                return _reject('insufficient_credits', f"Exercising {qty} rights costs {cost} CR; available {avail_cr}")

            sym = self._sym(target)
            new_exercised = pill['rights_exercised'] + qty
            # Mint new shares: SYSTEM balance debited -qty (float expands), agent credited +qty
            self._move(f"poison-exercise-{pill_id}-{target}-{agent}-{new_exercised}-r{ref.current_round}", (
                (agent, 'CR', -cost),
                (target, 'CR', cost),
                ('SYSTEM', sym, -qty),
                (agent, sym, qty),
            ))

            new_status = 'expired' if new_exercised >= pill['rights_issued'] else 'active'
            if 'pill_id' in pill and pill['pill_id']:
                ref.conn.execute("UPDATE corp_poison_pills SET rights_exercised = ?, status = ? WHERE pill_id = ?",
                                 (new_exercised, new_status, pill['pill_id']))
            else:
                ref.conn.execute("UPDATE corp_poison_pills SET rights_exercised = ?, status = ? WHERE target = ?",
                                 (new_exercised, new_status, target))

            new_float = self.live_shares(sym)
            self._event("poison_pill_exercised", agent,
                        f"{agent} exercised {qty} poison pill rights of {sym} for {cost} CR (circulating float expanded to {new_float})",
                        victim=pill['trigger_raider'])
            self._takeovers()
            return {'v': 1, 'kind': 'exercise_rights_ok', 'payload': {
                'agent': agent, 'target': target, 'rights_exercised': qty,
                'cost': cost, 'new_circulating_float': new_float, 'status': new_status
            }}

    # ------------------------------------------------------------ Predatory Lending & Debt Buying (#164)

    def create_loan_offer(self, lender: str, borrower: str, principal: int, interest_rate: float = 0.20, due_rounds: int = 5) -> Dict[str, Any]:
        """Propose a loan to another corporation with escrowed principal (#164)."""
        ref = self.ref
        lender = (lender or '').strip().lower()
        borrower = (borrower or '').strip().lower()
        try:
            principal = _safe_int(principal, 'Principal', min_val=1)
            interest_rate = _safe_float(interest_rate, 'Interest rate', min_val=0.0, max_val=0.50)
            due_rounds = _safe_int(due_rounds, 'Due rounds', min_val=3, max_val=1000)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        if lender not in self.active():
            return _reject('invalid_lender', f"Unknown or inactive lender '{lender}'")
        if borrower not in self.active():
            return _reject('invalid_borrower', f"Unknown or inactive borrower '{borrower}'")
        if lender == borrower:
            return _reject('self_loan', "Cannot issue loan to yourself")

        due_amount = int(round(principal * (1.0 + interest_rate)))

        with ref.lock, ref.conn:
            avail = ref.peer._available(lender, 'CR') if hasattr(ref, 'peer') else ref.get_balance(lender, 'CR')
            if avail < principal:
                return _reject('insufficient_credits', f"Lender requires {principal} CR; available {avail}")

            cur = ref.conn.execute(
                "INSERT INTO corp_loan_offers (lender, borrower, principal, interest_rate, due_amount, due_rounds, created_round, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                (lender, borrower, principal, interest_rate, due_amount, due_rounds, ref.current_round))
            offer_id = cur.lastrowid

            self._move(f"loan-escrow-{offer_id}-{lender}-r{ref.current_round}", (
                (lender, 'CR', -principal),
                ('SYSTEM', 'CR', principal),
            ))

            self._event("loan_offered", lender,
                        f"{lender} offered loan #{offer_id} to {borrower}: {principal} CR at {int(interest_rate*100)}% interest for {due_rounds} rounds",
                        victim=borrower)
            return {'v': 1, 'kind': 'loan_offer_ok', 'payload': {
                'offer_id': offer_id, 'lender': lender, 'borrower': borrower,
                'principal': principal, 'due_amount': due_amount, 'due_rounds': due_rounds
            }}

    def accept_loan_offer(self, caller: str, offer_id: int) -> Dict[str, Any]:
        """Accept a loan offer, disbursing escrowed funds (#164)."""
        ref = self.ref
        caller = (caller or '').strip().lower()
        try:
            offer_id = _safe_int(offer_id, 'Offer ID', min_val=1)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        with ref.lock, ref.conn:
            offer = ref.conn.execute("SELECT * FROM corp_loan_offers WHERE offer_id = ? AND status = 'open'", (offer_id,)).fetchone()
            if not offer:
                return _reject('offer_not_found', f"Active loan offer #{offer_id} not found")
            offer = dict(offer)

            if caller != offer['borrower'] and caller != 'admin':
                return _reject('unauthorized', f"Only borrower {offer['borrower']} can accept this loan offer")

            if offer['borrower'] not in self.active():
                return _reject('inactive_borrower', f"Borrower '{offer['borrower']}' is no longer an active corporation")
            if offer['lender'] not in self.active():
                return _reject('inactive_lender', f"Lender '{offer['lender']}' is no longer an active corporation")

            borrower = offer['borrower']
            sym = self._sym(borrower)
            due_round = ref.current_round + offer['due_rounds']

            self._move(f"loan-disburse-{offer_id}-{borrower}-r{ref.current_round}", (
                ('SYSTEM', 'CR', -offer['principal']),
                (borrower, 'CR', offer['principal']),
            ))

            ref.conn.execute("UPDATE corp_loan_offers SET status = 'accepted' WHERE offer_id = ?", (offer_id,))
            cur = ref.conn.execute(
                "INSERT INTO corp_predatory_loans (lender, borrower, principal, due_amount, due_round, collateral_sym, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active')",
                (offer['lender'], borrower, offer['principal'], offer['due_amount'], due_round, sym or ''))
            loan_id = cur.lastrowid

            self._event("predatory_loan", offer['lender'],
                        f"{borrower} accepted loan #{loan_id} from {offer['lender']}: {offer['principal']} CR disbursed, {offer['due_amount']} CR due round {due_round}",
                        victim=borrower)
            return {'v': 1, 'kind': 'loan_accept_ok', 'payload': {
                'loan_id': loan_id, 'lender': offer['lender'], 'borrower': borrower,
                'principal': offer['principal'], 'due_amount': offer['due_amount'], 'due_round': due_round
            }}

    def _cancel_loan_offer_locked(self, caller: str, offer_id: int) -> Dict[str, Any]:
        ref = self.ref
        caller = (caller or '').strip().lower()
        try:
            offer_id = _safe_int(offer_id, 'Offer ID', min_val=1)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        offer = ref.conn.execute("SELECT * FROM corp_loan_offers WHERE offer_id = ? AND status = 'open'", (offer_id,)).fetchone()
        if not offer:
            return _reject('offer_not_found', f"Active loan offer #{offer_id} not found")
        offer = dict(offer)

        if caller != offer['lender'] and caller != 'admin':
            return _reject('unauthorized', f"Only lender {offer['lender']} can cancel this loan offer")

        recipient = self._resolve_creditor(offer['lender'])
        self._move(f"loan-refund-{offer_id}-{offer['lender']}-r{ref.current_round}", (
            ('SYSTEM', 'CR', -offer['principal']),
            (recipient, 'CR', offer['principal']),
        ))
        ref.conn.execute("UPDATE corp_loan_offers SET status = 'cancelled' WHERE offer_id = ?", (offer_id,))
        return {'v': 1, 'kind': 'loan_cancel_ok', 'payload': {'offer_id': offer_id, 'refund_cr': offer['principal']}}

    def cancel_loan_offer(self, lender: str, offer_id: int) -> Dict[str, Any]:
        """Cancel an open loan offer and refund escrowed principal (#164)."""
        with self.ref.lock, self.ref.conn:
            return self._cancel_loan_offer_locked(lender, offer_id)

    def issue_predatory_loan(self, lender: str, borrower: str, principal: int, interest_rate: float = 0.20, due_rounds: int = 5) -> Dict[str, Any]:
        """Backward-compatible helper creating a loan offer (#164)."""
        return self.create_loan_offer(lender, borrower, principal, interest_rate=interest_rate, due_rounds=due_rounds)

    def buy_distressed_debt(self, buyer: str, debtor: str, amount: int) -> Dict[str, Any]:
        """Purchase distressed referee debt from SYSTEM; converts into a structured debt claim (#164)."""
        ref = self.ref
        buyer = (buyer or '').strip().lower()
        debtor = (debtor or '').strip().lower()
        try:
            amount = _safe_int(amount, 'Amount', min_val=1)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        if buyer not in self.active():
            return _reject('invalid_buyer', f"Unknown or inactive buyer '{buyer}'")
        if debtor not in self.active():
            return _reject('invalid_debtor', f"Unknown or inactive debtor '{debtor}'")
        if buyer == debtor:
            return _reject('self_debt', "Cannot buy your own debt")

        with ref.lock, ref.conn:
            r = self._row(debtor)
            debt = r['debt']
            if debt <= 0:
                return _reject('no_debt', f"{debtor} has no outstanding debt")

            buy_amount = min(debt, amount)
            avail = ref.peer._available(buyer, 'CR') if hasattr(ref, 'peer') else ref.get_balance(buyer, 'CR')
            if avail < buy_amount:
                return _reject('insufficient_credits', f"Requires {buy_amount} CR; available {avail}")

            self._set(debtor, debt=debt - buy_amount)
            due_amount = buy_amount  # 0% surcharge; debtor owes exactly what buyer paid to retire referee debt
            due_round = ref.current_round + 5
            sym = self._sym(debtor)
            cur = ref.conn.execute(
                "INSERT INTO corp_predatory_loans (lender, borrower, principal, due_amount, due_round, collateral_sym, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active')",
                (buyer, debtor, buy_amount, due_amount, due_round, sym or ''))
            loan_id = cur.lastrowid

            # Pay SYSTEM to retire official referee debt and convert to private claim
            self._move(f"debt-buy-{loan_id}-{buyer}-{debtor}-r{ref.current_round}", (
                (buyer, 'CR', -buy_amount),
                ('SYSTEM', 'CR', buy_amount),
            ))
            self._event("debt_bought", buyer,
                        f"{buyer} purchased {buy_amount} CR of {debtor}'s distressed debt from SYSTEM; converted to predatory claim #{loan_id} ({due_amount} CR due round {due_round})",
                        victim=debtor)
            return {'v': 1, 'kind': 'debt_bought_ok', 'payload': {
                'loan_id': loan_id, 'buyer': buyer, 'debtor': debtor,
                'amount_bought': buy_amount, 'due_amount': due_amount, 'due_round': due_round
            }}

    def repay_loan(self, borrower: str, loan_id: int) -> Dict[str, Any]:
        """Repay outstanding predatory loan principal + interest (#164)."""
        ref = self.ref
        borrower = (borrower or '').strip().lower()
        try:
            loan_id = _safe_int(loan_id, 'Loan ID', min_val=1)
        except ValueError as e:
            return _reject('invalid_parameters', str(e))

        with ref.lock, ref.conn:
            loan = ref.conn.execute("SELECT * FROM corp_predatory_loans WHERE loan_id = ? AND status = 'active'", (loan_id,)).fetchone()
            if not loan:
                return _reject('loan_not_found', f"Active loan #{loan_id} not found")
            loan = dict(loan)
            if borrower != loan['borrower'] and borrower != 'admin':
                return _reject('unauthorized', f"Only borrower {loan['borrower']} can repay this loan")

            due = loan['due_amount']
            actual_borrower = loan['borrower']
            avail = ref.peer._available(actual_borrower, 'CR') if hasattr(ref, 'peer') else ref.get_balance(actual_borrower, 'CR')
            if avail < due:
                return _reject('insufficient_credits', f"Repaying loan #{loan_id} requires {due} CR; available {avail}")

            recipient = self._resolve_creditor(loan['lender'])
            self._move(f"loan-repay-{loan_id}-r{ref.current_round}", (
                (actual_borrower, 'CR', -due),
                (recipient, 'CR', due),
            ))
            ref.conn.execute("UPDATE corp_predatory_loans SET status = 'repaid' WHERE loan_id = ?", (loan_id,))
            self._event("loan_repaid", actual_borrower,
                        f"{actual_borrower} repaid loan #{loan_id} ({due} CR) to {recipient}")
            return {'v': 1, 'kind': 'loan_repaid_ok', 'payload': {'loan_id': loan_id, 'amount': due}}

    def _check_loan_maturities(self, round_num: int) -> None:
        """Process maturing loans: auto-repay if available cash allows, else route to corporate debt (#164)."""
        ref = self.ref
        mature = ref.conn.execute(
            "SELECT * FROM corp_predatory_loans WHERE status = 'active' AND due_round <= ?", (round_num,)
        ).fetchall()
        for loan in mature:
            loan_id = loan["loan_id"]
            borrower, lender = loan["borrower"], loan["lender"]
            due = loan["due_amount"]
            avail = ref.peer._available(borrower, "CR") if hasattr(ref, 'peer') else ref.get_balance(borrower, "CR")
            if avail >= due:
                recipient = self._resolve_creditor(lender)
                self._move(f"loan-repay-auto-{loan_id}-r{round_num}",
                           ((borrower, "CR", -due), (recipient, "CR", due)))
                ref.conn.execute("UPDATE corp_predatory_loans SET status = 'repaid' WHERE loan_id = ?", (loan_id,))
                self._event("loan_repaid", borrower, f"{borrower} auto-repaid predatory loan #{loan_id} ({due} CR) to {recipient}")
            else:
                ref.conn.execute("UPDATE corp_predatory_loans SET status = 'defaulted' WHERE loan_id = ?", (loan_id,))
                # Route unpaid amount to corporate debt so it follows fair auction caps instead of instant takeover
                self.add_debt(borrower, due, f"default on predatory loan #{loan_id}")
                self._event("loan_default", borrower,
                            f"{borrower} defaulted on predatory loan #{loan_id} ({due} CR); balance added to corporate debt",
                            victim=borrower)

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
        self._check_loan_maturities(round_num)
        self._takeovers()
        return {"active": self.active()}
