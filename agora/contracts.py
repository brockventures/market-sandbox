"""
agora/contracts.py - owned, tradable station contracts (#115).

Spec: docs/fleet-market-spec.md section 2. Simulator evidence: #100, #112,
#113 (claim-and-flip finding), #141 (deposit sizing).

- Posting. Every POST_EVERY rounds a station posts a contract: good, qty
  300-800, deadline 6-10 rounds out, price 1.3-1.6x base.
- Claim. The first fleet to CLAIM owns it, at most MAX_OPEN per fleet.
  Claiming locks a deposit of BOND_PCT of the undelivered value with SYSTEM
  (Ryan, #agent-chat 2026-09-23 08:37: "a deposit is a good idea, add
  that"). Without it, claiming was free and claim-then-resell beat
  delivering (#113).
- Delivery. Only the owner delivers, docked at the contract's station.
  Partial delivery is allowed. The station pays from SYSTEM, and the deposit
  is refunded in proportion.
- Resale. The owner LISTs at a price; any fleet anywhere BUYs. The buyer
  pays the price plus the deposit to the seller, and carries the deposit.
- Lapse. At the deadline the undelivered part lapses. The deposit goes to
  the station, and the owner pays a PENALTY of the undelivered value
  (Ryan, 2026-09-22 23:24: 50%); a corp's first lapse of the game costs
  FIRST_PENALTY instead (#162). A penalty the owner cannot cover is
  charged to what it holds and the rest is recorded as `shortfall`, for
  corporate debt (#116).

Every move is a balanced ledger entry. Off unless the referee's contracts
flag is set (new_game or AGORA_CONTRACTS=1).
"""

import os
import random
from typing import Any, Dict, List, Optional

from agora.spatial import STATIONS, BASE_PRICES

POST_EVERY = 4
QTY = (300, 800)
DEADLINE = (6, 10)
PREMIUM = (1.3, 1.6)
GOODS = ("FRAG", "FOOD", "ORE", "FUEL")
MAX_OPEN = 2
BOND_PCT = 0.25
PENALTY = 0.5
# A corp's first lapse of the game costs FIRST_PENALTY instead (#162, novice
# survival: "one bad claim teaches rather than kills"). Every later one
# costs PENALTY. #162 sweep, 60 seeds: novices out of the game 35% -> 30%.
FIRST_PENALTY = 0.25


def env_contracts() -> bool:
    return os.environ.get("AGORA_CONTRACTS", "").strip().lower() in ("1", "true", "yes", "on")


SCHEMA = """
CREATE TABLE IF NOT EXISTS station_contracts (
    contract_id    TEXT PRIMARY KEY,
    station_id     TEXT NOT NULL,
    instrument     TEXT NOT NULL,
    qty_total      INTEGER NOT NULL,
    qty_remaining  INTEGER NOT NULL,
    price          INTEGER NOT NULL,
    posted_round   INTEGER NOT NULL,
    deadline       INTEGER NOT NULL,
    owner          TEXT,
    bond           INTEGER NOT NULL DEFAULT 0,
    list_price     INTEGER,
    penalty        INTEGER NOT NULL DEFAULT 0,
    shortfall      INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL CHECK (status IN ('open', 'fulfilled', 'lapsed'))
)
"""


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {'v': 1, 'kind': 'reject', 'payload': {'reason': reason, 'detail': detail}}


class ContractDesk:
    def __init__(self, ref, seed: int = 0):
        self.ref = ref
        with ref.conn:
            ref.conn.execute(SCHEMA)
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.rng = random.Random(f"contracts-{seed}")
        self.seq = 0

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

    def _available(self, agent: str, inst: str) -> int:
        return self.ref.peer._available(agent, inst)

    def _row(self, cid: str):
        return self.ref.conn.execute("SELECT * FROM station_contracts WHERE contract_id = ?", (cid,)).fetchone()

    def _open_count(self, agent: str) -> int:
        return self.ref.conn.execute("SELECT COUNT(*) FROM station_contracts WHERE owner = ? AND status = 'open'",
                                     (agent,)).fetchone()[0]

    def _station_account(self, st: str) -> str:
        return f"depot_{st}" if getattr(self.ref, 'depots_enabled', False) else 'SYSTEM'

    def bond_for(self, price: int, qty: int) -> int:
        return int(price * qty * BOND_PCT)

    # ------------------------------------------------------------ actions

    def claim(self, agent: str, cid: str) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(agent)
        if ref.fleet_out(agent):
            return _reject('fleet_out', ref.fleet_out(agent))
        with ref.lock, ref.conn:
            row = self._row(cid)
            if not row or row['status'] != 'open' or row['deadline'] < ref.current_round:
                return _reject('not_open', f"Contract '{cid}' is not open")
            if row['owner']:
                return _reject('already_claimed', f"Contract '{cid}' belongs to {row['owner']}; it may be listed for sale")
            if self._open_count(agent) >= MAX_OPEN:
                return _reject('too_many_contracts', f"At most {MAX_OPEN} open contracts per fleet")
            bond = self.bond_for(row['price'], row['qty_remaining'])
            if self._available(agent, 'CR') < bond:
                return _reject('insufficient_credits',
                               f"Claiming locks a {bond} CR deposit ({int(BOND_PCT * 100)}%); available {self._available(agent, 'CR')}")
            self._move(f"contract-claim-{cid}", ((agent, 'CR', -bond), ('SYSTEM', 'CR', bond)))
            ref.conn.execute("UPDATE station_contracts SET owner = ?, bond = ? WHERE contract_id = ?", (agent, bond, cid))
        return {'v': 1, 'kind': 'contract_claim_ok', 'payload': self.get(cid)}

    def list_for_sale(self, agent: str, cid: str, price: Optional[int]) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(agent)
        with ref.lock, ref.conn:
            row = self._row(cid)
            if not row or row['status'] != 'open':
                return _reject('not_open', f"Contract '{cid}' is not open")
            if row['owner'] != agent:
                return _reject('unauthorized', f"Contract '{cid}' is not yours")
            p = None if price is None or int(price) <= 0 else int(price)
            ref.conn.execute("UPDATE station_contracts SET list_price = ? WHERE contract_id = ?", (p, cid))
        return {'v': 1, 'kind': 'contract_list_ok', 'payload': self.get(cid)}

    def buy(self, buyer: str, cid: str) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(buyer)
        if ref.fleet_out(buyer):
            return _reject('fleet_out', ref.fleet_out(buyer))
        with ref.lock, ref.conn:
            row = self._row(cid)
            if not row or row['status'] != 'open' or row['deadline'] < ref.current_round:
                return _reject('not_open', f"Contract '{cid}' is not open")
            if not row['list_price']:
                return _reject('not_listed', f"Contract '{cid}' is not for sale")
            if row['owner'] == buyer:
                return _reject('self_trade', 'You already own this contract')
            if self._open_count(buyer) >= MAX_OPEN:
                return _reject('too_many_contracts', f"At most {MAX_OPEN} open contracts per fleet")
            cost = row['list_price'] + row['bond']
            if self._available(buyer, 'CR') < cost:
                return _reject('insufficient_credits',
                               f"Buying costs {row['list_price']} CR plus the {row['bond']} CR deposit it carries; "
                               f"available {self._available(buyer, 'CR')}")
            self._move(f"contract-buy-{cid}-{ref.current_round}-{buyer}",
                       ((buyer, 'CR', -cost), (row['owner'], 'CR', cost)))
            ref.conn.execute("UPDATE station_contracts SET owner = ?, list_price = NULL WHERE contract_id = ?",
                             (buyer, cid))
        return {'v': 1, 'kind': 'contract_buy_ok', 'payload': self.get(cid)}

    def deliver(self, agent: str, cid: str, qty: Optional[int] = None) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(agent)
        with ref.lock, ref.conn:
            row = self._row(cid)
            if not row or row['status'] != 'open' or row['deadline'] < ref.current_round:
                return _reject('not_open', f"Contract '{cid}' is not open")
            if row['owner'] != agent:
                return _reject('unauthorized', f"Only the owner can deliver into '{cid}'")
            loc = ref.get_vessel_location(agent)
            if loc.get('status') != 'docked' or loc.get('station_id') != row['station_id']:
                return _reject('vessel_not_docked', f"Deliver while docked at {row['station_id']}")
            have = self._available(agent, row['instrument'])
            n = min(row['qty_remaining'], have if qty is None else min(int(qty), have))
            if n <= 0:
                return _reject('insufficient_balance', f"No {row['instrument']} available to deliver")
            pay = n * row['price']
            refund = row['bond'] * n // row['qty_remaining']
            self._move(f"contract-deliver-{cid}-{ref.current_round}-{row['qty_remaining']}", (
                (agent, row['instrument'], -n), ('SYSTEM', row['instrument'], n),
                ('SYSTEM', 'CR', -(pay + refund)), (agent, 'CR', pay + refund)))
            left = row['qty_remaining'] - n
            ref.conn.execute("UPDATE station_contracts SET qty_remaining = ?, bond = ?, status = ? WHERE contract_id = ?",
                             (left, row['bond'] - refund, 'fulfilled' if left == 0 else 'open', cid))
        out = self.get(cid)
        out['delivered'], out['paid'], out['bond_refund'] = n, pay, refund
        return {'v': 1, 'kind': 'contract_deliver_ok', 'payload': out}

    # ------------------------------------------------------------ rounds

    def step_locked(self, round_num: int) -> Dict[str, List[str]]:
        """Called from step_round under ref.lock inside its transaction:
        lapse overdue contracts, then post a new one every POST_EVERY rounds."""
        ref, done = self.ref, {'lapsed': [], 'posted': []}
        for row in ref.conn.execute("SELECT * FROM station_contracts WHERE status = 'open' AND deadline < ?",
                                    (round_num,)).fetchall():
            penalty = shortfall = 0
            if row['owner'] and row['qty_remaining'] > 0:
                rate = PENALTY if self.lapses(row['owner']) else FIRST_PENALTY
                penalty = int(row['price'] * row['qty_remaining'] * rate)
                paid = min(penalty, max(0, ref.get_balance(row['owner'], 'CR')))
                shortfall = penalty - paid
                self._move(f"contract-lapse-{row['contract_id']}", (
                    ('SYSTEM', 'CR', -row['bond']), (self._station_account(row['station_id']), 'CR', row['bond']),
                    (row['owner'], 'CR', -paid), ('SYSTEM', 'CR', paid)))
            if penalty and getattr(ref, 'events_enabled', False):
                ref.events.record_locked('contract_lapse', 'public', actor=row['owner'], amount=penalty,
                                         detail=f"{row['owner']} missed contract {row['contract_id']} "
                                                f"({row['qty_remaining']} {row['instrument']} short, {penalty} CR penalty)")
            if shortfall and getattr(ref, 'corporate_enabled', False):
                ref.corporate.add_debt(row['owner'], shortfall, f"unpaid penalty on contract {row['contract_id']}")
            ref.conn.execute("UPDATE station_contracts SET status = 'lapsed', bond = 0, list_price = NULL, "
                             "penalty = ?, shortfall = ? WHERE contract_id = ?",
                             (penalty, shortfall, row['contract_id']))
            done['lapsed'].append(row['contract_id'])
        if round_num % POST_EVERY == 0:
            done['posted'].append(self._post_locked(round_num))
        return done

    def lapses(self, agent: str) -> int:
        """Contracts this corp has already let lapse with a penalty this game."""
        return self.ref.conn.execute("SELECT COUNT(*) FROM station_contracts WHERE owner = ? AND status = 'lapsed' "
                                     "AND penalty > 0", (agent,)).fetchone()[0]

    def penalty_rate(self, agent: str) -> float:
        """The lapse penalty `agent` would pay next: FIRST_PENALTY until its first lapse."""
        return PENALTY if self.lapses(agent) else FIRST_PENALTY

    def _post_locked(self, round_num: int) -> str:
        comm = self.rng.choice(GOODS)
        cheap = min(STATIONS, key=lambda st: BASE_PRICES[st][comm])
        st = self.rng.choice([x for x in STATIONS if x != cheap])
        qty = self.rng.randint(*QTY)
        self.seq += 1
        cid = f"k{round_num}{st[:2]}{self.seq}"
        self.ref.conn.execute("""INSERT INTO station_contracts
            (contract_id, station_id, instrument, qty_total, qty_remaining, price, posted_round, deadline, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (cid, st, comm, qty, qty, int(round(BASE_PRICES[st][comm] * self.rng.uniform(*PREMIUM))),
             round_num, round_num + self.rng.randint(*DEADLINE)))
        return cid

    # ------------------------------------------------------------ reads

    def get(self, cid: str) -> Optional[Dict[str, Any]]:
        row = self._row(cid)
        return dict(row) if row else None

    def list(self, status: str = 'open', station_id: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM station_contracts WHERE status = ?", [status]
        if station_id:
            q += " AND station_id = ?"
            args.append(station_id.lower())
        return [dict(r) for r in self.ref.conn.execute(q + " ORDER BY deadline, contract_id", args)]

    def holdings_adjustment(self) -> Dict[str, int]:
        """Deposits are the owner's money, held until delivery or lapse."""
        out: Dict[str, int] = {}
        for r in self.ref.conn.execute("SELECT owner, bond FROM station_contracts WHERE status = 'open' AND owner IS NOT NULL"):
            out[r['owner']] = out.get(r['owner'], 0) + r['bond']
        return out

    def shortfalls(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.ref.conn.execute("SELECT owner, SUM(shortfall) s FROM station_contracts "
                                       "WHERE shortfall > 0 GROUP BY owner"):
            out[r['owner']] = r['s']
        return out
