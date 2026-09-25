"""
agora/piracy.py - raids on the space lanes (#145).

Ryan, #agent-chat 2026-09-23 09:1x: "how about adding a risk of piracy?";
approved 09:13 ("especially the privateers idea is perfect"). Zero added the
extortion choice and the black market (09:14). Sized in the simulator
(tools/economy_sim.py, class Piracy, PR #146).

Route risk
- A trip on a belt route (one that paid a toll) is raided with chance
  P_BELT, any other trip with P_INNER (live default 0.15 / 0.04,
  AGORA_PIRACY="belt,inner").
- Every HOT_EVERY rounds one station is "hot", a seeded public choice.
  Trips to or from it carry HOT_MULT x the risk. It is shown in the briefing
  and at GET /referee/piracy, and posted to the GalNet feed.
- Raiders follow value: the chance is scaled by cargo value / VALUE_REF,
  clamped to x0.5..x2. Value = qty x the good's base price averaged across
  the four stations (agora.spatial.BASE_PRICES).
- Escort: pass `escort: true` with the move. It costs ESCORT_PCT of the
  cargo's value in CR, paid to SYSTEM at departure, and cuts the chance by
  ESCORT_CUT. A fleet that cannot pay is rejected before anything moves.

Extortion choice (rolled once, at departure, like hazards)
- A hit puts a pending demand on the transit: pay a ransom of RANSOM_PCT of
  the cargo value in CR, or surrender SURRENDER_PCT of the cargo. The move's
  response carries it in a `piracy` field.
- The fleet answers with POST /referee/piracy/{transit_id}/respond
  {choice: pay|surrender|fight} before the next round tick. No answer by
  then counts as fight.
- Fight: FIGHT_ESCAPE chance to get away with nothing lost; otherwise lose
  FIGHT_LOSS of the cargo and arrive FIGHT_DELAY rounds late.

Black market
- Stolen goods are fenced at FENCE_STATION's depot (depot_ceres) as
  inventory, and the reactive depot's shelf there is raised by the same
  amount, so its ask drops: a short-lived glut for alert traders. Ceres is
  the least central station: the only belt station, every route to it is
  tolled, and it has the longest total transit time to the other three.
  With depots off there is no depot to fence into and the goods stay with
  SYSTEM.
- Ransom CR goes to SYSTEM. A sponsored raid's take goes to its sponsor
  (PRIV_SHARE, all of it since #186), so only unsponsored raids are fenced.

Privateers
- POST /referee/privateers {target} costs PRIV_COST CR (to SYSTEM) and adds
  PRIV_ADD to the target's raid chance for PRIV_ROUNDS rounds. The sponsor
  gets PRIV_SHARE of whatever is taken from the target in that window
  (ransom CR from SYSTEM, goods before they reach the fence).
- Each sponsored raid is traced with PRIV_TRACE chance: the sponsor pays a
  fine of PRIV_FINE x the fee to SYSTEM (capped at the CR it holds), and the
  briefing names it. An untraced sponsor is never shown publicly.
- Secrecy (#153, agora/events.py, when the referee's events flag is on): a
  contract is a secret event (only the sponsor sees it, and it can leak), and
  each raid it pays for is a private event for the victim ("sponsor
  unknown"). The trace roll above is the exposure: it keeps the fine and
  also exposes the contract and its raids and posts a GalNet scandal. Until
  then nobody but the sponsor and the victim learns a raid was sponsored,
  and only the sponsor sees the contract.
- One active contract per sponsor, and one per target; no self-targeting.

Hooks into sibling PRs, guarded so this works on main without them:
- Armor (ship upgrades, PR #149): the raid chance is multiplied by
  ref.upgrades.factor(agent, 'armor') when ref has `upgrades`.
- Corporate (PR #148): a fleet that ref.fleet_out(agent) reports as out
  cannot respond to a demand or hire privateers, and cannot be targeted.

Every movement is a balanced ledger entry and no fleet balance goes
negative. Rolls come from a seeded random.Random, reset by new_game (game
seed) and reset_to_genesis (0). Whether a trip is raided, a sponsor traced
or a fight escaped is a per-fleet marble-bag draw (#214, agora/bag.py): the
raid chance varies trip to trip, so raids use the bag's luck accumulator,
which keeps a fleet's raid rate at its mean chance and bounds its streaks. Off unless odds are set (new_game
{"piracy": ...}, the constructor, or AGORA_PIRACY in the live server).
"""

import os
import random
from typing import Any, Dict, List, Optional, Tuple

from fractions import Fraction

from agora.bag import Bags, MAX_N
from agora.galnet import GalNetNewsEvent
from agora.spatial import STATIONS, COMMODITIES, BASE_PRICES, BELT_ROUTES

DEFAULT_P_BELT = 0.15
DEFAULT_P_INNER = 0.04
HOT_EVERY, HOT_MULT = 20, 2.0
VALUE_REF = 10_000
VALUE_MULT = (0.5, 2.0)
# The cargo-value multiplier is rounded to this step (#175), so every trip's
# raid odds are one of a small set and each set of conditions has a bag of
# fixed odds (see raid_key).
VALUE_STEP = 0.25
# Raid odds are drawn as the nearest k/n with n <= MAX_N (a marble bag) when
# that is within this share of the exact odds; otherwise at the exact odds
# from the key's luck credit (agora/bag.py draw_varying).
BAG_ROUND_TOL = 0.01
ESCORT_PCT, ESCORT_CUT = 0.04, 0.75
RANSOM_PCT = 0.15
SURRENDER_PCT = 0.25
FIGHT_ESCAPE = 0.5
FIGHT_LOSS = 0.5
FIGHT_DELAY = (1, 2)
# PRIV_COST 3,000 until #162: at 2,000 a hauler that also hires privateers
# earns about what a plain hauler does (at 3,000 it earned ~30k less).
# 1,500 since #189: after #183/#185 the privateer style's median fell to
# 99k / 97k (styles seeds 1-20 / 21-40), under the 100k band floor; 1,750
# still left 21-40 at 99k. At 1,500 it is 108k / 103k.
# #186 (covert economics): the privateer's covert lane lost money in every
# game (styles, seeds 1-40: median -28k / -25k, 0/40 in profit). Per game the
# sponsor paid ~22.5k in fees and ~9k in fines for ~5k of ransom cuts: each
# sponsored raid took ~1.1k (15% of ~7.6k cargo), the sponsor kept half, and
# the expected fine per raid (0.25 x 3 x 1,500 = 1,125) ate the rest. Now the
# sponsor keeps the raiders' whole take (PRIV_SHARE 0.5 -> 1.0; the victim
# loses no more than before, SYSTEM's half goes to the sponsor instead), and
# contracts are cheaper and quieter: PRIV_COST 1,500 -> 750, PRIV_TRACE
# 0.25 -> 0.10, PRIV_FINE 3x -> 2x the fee. PRIV_ADD (the victim's extra raid
# chance) is unchanged.
PRIV_ROUNDS, PRIV_COST, PRIV_ADD, PRIV_SHARE, PRIV_TRACE, PRIV_FINE = 20, 750, 0.15, 1.0, 0.10, 2
FENCE_STATION = 'ceres'
CHOICES = ('pay', 'surrender', 'fight')

# Reference unit value: base price averaged across the four stations.
REF_PRICE = {c: sum(BASE_PRICES[s][c] for s in STATIONS) / len(STATIONS) for c in COMMODITIES}

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS piracy_looted_cargo (
        agent_id       TEXT NOT NULL,
        commodity      TEXT NOT NULL,
        qty            INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (agent_id, commodity)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS piracy_tributes (
        tribute_id   TEXT PRIMARY KEY,
        demander     TEXT NOT NULL,
        target       TEXT NOT NULL,
        amount_cr    INTEGER NOT NULL,
        rounds       INTEGER NOT NULL,
        start_round  INTEGER NOT NULL,
        end_round    INTEGER NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS piracy_raids (
        transit_id     TEXT PRIMARY KEY,
        agent_id       TEXT NOT NULL,
        round          INTEGER NOT NULL,
        origin         TEXT NOT NULL,
        destination    TEXT NOT NULL,
        commodity      TEXT NOT NULL,
        cargo_qty      INTEGER NOT NULL,
        cargo_value    INTEGER NOT NULL,
        odds           REAL NOT NULL,
        escorted       INTEGER NOT NULL DEFAULT 0,
        ransom         INTEGER NOT NULL,
        surrender_qty  INTEGER NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'surrendered', 'escaped', 'lost', 'void')),
        choice         TEXT,
        timed_out      INTEGER NOT NULL DEFAULT 0,
        cr_taken       INTEGER NOT NULL DEFAULT 0,
        qty_taken      INTEGER NOT NULL DEFAULT 0,
        delay          INTEGER NOT NULL DEFAULT 0,
        fenced_at      TEXT,
        resolved_round INTEGER,
        contract_id    TEXT,
        sponsor        TEXT,
        traced         INTEGER NOT NULL DEFAULT 0,
        fine           INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS piracy_privateers (
        contract_id    TEXT PRIMARY KEY,
        sponsor        TEXT NOT NULL,
        target         TEXT NOT NULL,
        start_round    INTEGER NOT NULL,
        expires_round  INTEGER NOT NULL,
        fee            INTEGER NOT NULL,
        raids          INTEGER NOT NULL DEFAULT 0,
        loot_cr        INTEGER NOT NULL DEFAULT 0,
        loot_qty       INTEGER NOT NULL DEFAULT 0,
        traced         INTEGER NOT NULL DEFAULT 0,
        fines          INTEGER NOT NULL DEFAULT 0
    )
    """,
)


def parse_piracy(v: Any) -> Optional[Tuple[float, float]]:
    """'0.15,0.04' / [0.15, 0.04] / {'belt':..,'inner':..} -> (p_belt, p_inner); '0'/False/None -> None."""
    if v is None or v is False:
        return None
    if isinstance(v, dict):
        v = (v.get('belt', 0), v.get('inner', 0))
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ('', '0', 'off', 'false', 'no'):
            return None
        if s in ('1', 'on', 'true', 'yes'):
            return (DEFAULT_P_BELT, DEFAULT_P_INNER)
        v = [x for x in s.replace(' ', '').split(',') if x]
    if v is True:
        return (DEFAULT_P_BELT, DEFAULT_P_INNER)
    try:
        b, i = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError):
        return None
    b, i = min(1.0, max(0.0, b)), min(1.0, max(0.0, i))
    return (b, i) if (b or i) else None


def env_piracy() -> Optional[Tuple[float, float]]:
    return parse_piracy(os.environ.get('AGORA_PIRACY'))


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {'v': 1, 'kind': 'reject', 'payload': {'reason': reason, 'detail': detail}}


def cargo_value(commodity: str, qty: int) -> int:
    return int(round(max(0, qty) * REF_PRICE.get((commodity or '').upper(), 0)))


class PiracyDesk:
    def __init__(self, ref, odds: Optional[Tuple[float, float]] = None, seed: int = 0):
        self.ref = ref
        with ref.conn:
            for stmt in SCHEMA:
                ref.conn.execute(stmt)
        self.odds = odds
        self.bags = Bags(ref.conn, 'piracy')
        self._tribute_counter = 0
        self._fence_counter = 0
        self._reseed(seed)

    @property
    def enabled(self) -> bool:
        return bool(self.odds)

    def _reseed(self, seed: int) -> None:
        self.seed = seed
        self.rng = random.Random(f"piracy-{seed}")
        self._epoch: Optional[int] = None

    def reset(self, seed: int) -> None:
        self._reseed(seed)
        self.bags.reset(seed)

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

    # ------------------------------------------------------------ odds

    def hot_station(self, round_num: int) -> str:
        """Seeded per HOT_EVERY-round window, independent of trip rolls."""
        return random.Random(f"piracy-hot-{self.seed}-{round_num // HOT_EVERY}").choice(STATIONS)

    def hot_until(self, round_num: int) -> int:
        return (round_num // HOT_EVERY + 1) * HOT_EVERY

    def escort_fee(self, commodity: str, qty: int) -> int:
        return int(cargo_value(commodity, qty) * ESCORT_PCT)

    def active_contract(self, target: str, round_num: int):
        return self.ref.conn.execute(
            "SELECT * FROM piracy_privateers WHERE target = ? AND start_round <= ? AND expires_round > ? "
            "ORDER BY start_round LIMIT 1", (target, round_num, round_num)).fetchone()

    def chance(self, agent: str, origin: str, dest: str, tolled: bool, commodity: str, qty: int,
               escort: bool, round_num: int) -> Dict[str, Any]:
        """Raid chance for one trip and how it was built."""
        if not self.odds or qty <= 0:
            return {'odds': 0.0, 'base': 0.0, 'hot': False, 'value': cargo_value(commodity, qty),
                    'value_mult': 0.0, 'privateers': False, 'escort': bool(escort)}
        p_belt, p_inner = self.odds
        base = p_belt if tolled else p_inner
        hot = self.hot_station(round_num) in (origin, dest)
        value = cargo_value(commodity, qty)
        vm = min(VALUE_MULT[1], max(VALUE_MULT[0], value / VALUE_REF))
        vm = min(VALUE_MULT[1], max(VALUE_MULT[0], round(vm / VALUE_STEP) * VALUE_STEP))
        p = base * (HOT_MULT if hot else 1.0) * vm
        priv = self.active_contract(agent, round_num) is not None
        if priv:
            p += PRIV_ADD
        if escort:
            p *= 1 - ESCORT_CUT
        # Armor (ship upgrades, #143 / PR #149); 1.0 on builds without them.
        armor = self.ref.upgrades.factor(agent, 'armor') if hasattr(self.ref, 'upgrades') else 1.0
        armor_tier = self.ref.upgrades.tier(agent, 'armor') if hasattr(self.ref, 'upgrades') else 0
        p *= armor
        stealth = 1.0
        stealth_tier = 0
        if hasattr(self.ref, 'upgrades') and self.ref.upgrades:
            st = self.ref.upgrades.tier(agent, 'stealth_drives') if hasattr(self.ref.upgrades, 'tier') else 0
            if isinstance(st, int) and st > 0:
                stealth = self.ref.upgrades.factor(agent, 'stealth_drives')
                stealth_tier = st
                p *= stealth

        # Belt Salvage Surge (#245): doubles pirate raid & privateer ambush odds along Ceres corridors
        salvage_surge = False
        if hasattr(self.ref, 'galnet') and self.ref.galnet and hasattr(self.ref.galnet, 'is_salvage_surge_active'):
            if self.ref.galnet.is_salvage_surge_active():
                if tolled or origin == 'ceres' or dest == 'ceres' or (origin, dest) in BELT_ROUTES:
                    p = min(1.0, p * 2.0)
                    salvage_surge = True

        return {'odds': round(min(1.0, p), 4), 'exact_odds': min(1.0, p), 'base': base, 'hot': hot, 'value': value,
                'value_mult': round(vm, 3), 'privateers': priv, 'escort': bool(escort), 'armor': armor,
                'armor_tier': armor_tier, 'stealth': stealth, 'stealth_tier': stealth_tier, 'tolled': bool(tolled),
                'salvage_surge': salvage_surge}

    @staticmethod
    def raid_key(vessel_id: str, c: Dict[str, Any]) -> str:
        """The bag a trip's raid is drawn from (#175, Ryan: escorts and armor
        are the ship's): one per ship per protection level (armor tier,
        escort or not) and per trip conditions (belt or inner route, hot
        station, cargo-value step, privateers on it). Within a key the odds
        are fixed, so its bag's rate is exact, and luck built up on one kind
        of trip never lands on another: an escorted trip draws only from its
        ship's escorted bag."""
        surge_tag = "|surge" if c.get('salvage_surge') else ""
        return (f"{vessel_id}|{'belt' if c.get('tolled') else 'inner'}|{'hot' if c.get('hot') else 'cool'}"
                f"|v{c.get('value_mult')}|{'priv' if c.get('privateers') else 'free'}"
                f"|a{c.get('armor_tier', 0)}|s{c.get('stealth_tier', 0)}{surge_tag}|{'escort' if c.get('escort') else 'bare'}")

    @staticmethod
    def bag_odds(p: float) -> float:
        """p as a marble bag's k/n (n <= MAX_N) when that is within
        BAG_ROUND_TOL of p; else p itself (drawn from the key's luck credit)."""
        if p <= 0 or p >= 1:
            return p
        q = float(Fraction(p).limit_denominator(MAX_N))
        return q if q > 0 and abs(q - p) <= BAG_ROUND_TOL * p else p

    @property
    def _secrecy(self) -> bool:
        """#153 visibility rules apply (the referee's events flag)."""
        return bool(getattr(self.ref, 'events_enabled', False)) and hasattr(self.ref, 'events')

    def _exposed(self, contract_id: Optional[str]) -> bool:
        return self._secrecy and self.ref.events.link_exposed(contract_id)

    def _out(self, agent: str) -> Optional[str]:
        """Why a bankrupt or taken-over corp cannot act (PR #148), or None."""
        return self.ref.fleet_out(agent) if hasattr(self.ref, 'fleet_out') else None

    # ------------------------------------------------------------ departure

    def charge_escort_locked(self, transit_id: str, agent: str, fee: int) -> None:
        if fee > 0:
            self._move(f"piracy-escort-{transit_id}", ((agent, 'CR', -fee), ('SYSTEM', 'CR', fee)))
            if self._secrecy:
                self.ref.events.record_locked('escort', 'public', actor=agent, amount=fee,
                                              detail=f"{agent} hired an escort for {fee} CR")

    def roll_departure_locked(self, transit_id: str, agent: str, origin: str, dest: str, tolled: bool,
                              commodity: str, qty: int, escort: bool, escort_fee: int,
                              round_num: int, vessel_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Called from initiate_transit under ref.lock inside its transaction,
        after the transit row is written. The raid is a marble from the
        flying ship's bag for this kind of trip (raid_key: #214, per ship and
        protection level since #175), so one trip's outcome does not shift
        another ship's, and an escorted trip's odds are its own."""
        if not self.odds:
            return None
        c = self.chance(agent, origin, dest, tolled, commodity, qty, escort, round_num)
        out = {'odds': c['odds'], 'hot_station': self.hot_station(round_num), 'hot_route': c['hot'],
               'cargo_value': c['value'], 'escort': bool(escort), 'escort_fee': escort_fee if escort else 0,
               'raided': False, 'demand': None}
        key = self.raid_key(vessel_id or f"{agent}/1", c)
        if qty <= 0 or not self.bags.draw('raid', key, self.bag_odds(c['exact_odds'])):
            return out
        ransom = int(c['value'] * RANSOM_PCT)
        surrender = int(qty * SURRENDER_PCT)
        contract = self.active_contract(agent, round_num)
        sponsor = contract['sponsor'] if contract else None
        traced, fine = 0, 0
        if contract:
            self.ref.conn.execute("UPDATE piracy_privateers SET raids = raids + 1 WHERE contract_id = ?",
                                  (contract['contract_id'],))
            if self._secrecy:
                self.ref.events.record_locked(
                    'privateer_raid', 'private', actor=sponsor, victim=agent, link=contract['contract_id'],
                    detail=f"raided on the {origin.capitalize()}-{dest.capitalize()} run by privateers "
                           f"under contract (sponsor unknown until exposed)")
            trace_odds = PRIV_TRACE
            if hasattr(self.ref, 'upgrades') and self.ref.upgrades:
                jt = self.ref.upgrades.tier(sponsor, 'ecm_jammers') if hasattr(self.ref.upgrades, 'tier') else 0
                if isinstance(jt, int) and jt > 0:
                    trace_odds *= self.ref.upgrades.factor(sponsor, 'ecm_jammers')
            if self.bags.draw('trace', sponsor, trace_odds):
                traced = 1
                fine = min(contract['fee'] * PRIV_FINE, max(0, self.ref.get_balance(sponsor, 'CR')))
                self._move(f"piracy-fine-{transit_id}", ((sponsor, 'CR', -fine), ('SYSTEM', 'CR', fine)))
                self.ref.conn.execute("UPDATE piracy_privateers SET traced = 1, fines = fines + ? WHERE contract_id = ?",
                                      (fine, contract['contract_id']))
                if self._secrecy:
                    self.ref.events.expose_link_locked(contract['contract_id'], 'trace', round_num)
        self.ref.conn.execute("""INSERT INTO piracy_raids
            (transit_id, agent_id, round, origin, destination, commodity, cargo_qty, cargo_value, odds, escorted,
             ransom, surrender_qty, status, contract_id, sponsor, traced, fine)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (transit_id, agent, round_num, origin, dest, commodity, qty, c['value'], c['odds'], int(bool(escort)),
             ransom, surrender, contract['contract_id'] if contract else None, sponsor, traced, fine))
        out['raided'] = True
        out['demand'] = self.public_raid(self._row(transit_id), viewer=agent)
        return out

    # ------------------------------------------------------------ resolution

    def _row(self, transit_id: str):
        return self.ref.conn.execute("SELECT * FROM piracy_raids WHERE transit_id = ?", (transit_id,)).fetchone()

    def _transit(self, transit_id: str):
        return self.ref.conn.execute("SELECT * FROM transits WHERE transit_id = ?", (transit_id,)).fetchone()

    def _fence_account(self) -> Optional[str]:
        return f"depot_{FENCE_STATION}" if getattr(self.ref, 'depots_enabled', False) else None

    def _steal_locked(self, row, transit, qty: int) -> Tuple[int, Optional[str]]:
        """Take qty goods out of the transit's SYSTEM escrow: the sponsor's
        share to the sponsor, the rest fenced at the black-market depot."""
        qty = max(0, min(qty, transit['cargo_qty'] or 0))
        if qty <= 0:
            return 0, None
        comm, tid = row['commodity'], row['transit_id']
        self.ref.conn.execute("UPDATE transits SET cargo_qty = cargo_qty - ? WHERE transit_id = ?", (qty, tid))
        cut = int(qty * PRIV_SHARE) if row['sponsor'] else 0
        fence_qty = qty - cut
        fence = self._fence_account()
        legs = [('SYSTEM', comm, -(cut + (fence_qty if fence else 0)))]
        if cut:
            self.record_loot(row['sponsor'], comm, cut)
            # The sponsor's cut is delivered to its ship 1 (#175): the raiders
            # carry it, as before ships, so #186's covert economics are unchanged.
            # What ship 1's hold cannot take waits in the sponsor's hold at
            # the ship's station (#95, FleetDesk.stow_locked).
            sponsor = row['sponsor']
            if self.ref.fleet.is_corp(sponsor):
                legs += [(acct, comm, n) for acct, n in self.ref.fleet.stow_locked(f"{sponsor}/1", comm, cut)]
            else:
                legs.append((sponsor, comm, cut))
        if fence and fence_qty:
            legs.append((fence, comm, fence_qty))
        self._move(f"piracy-loot-{tid}", legs)
        if fence and fence_qty:
            rx = getattr(self.ref, '_reactive', None)
            if rx and (FENCE_STATION, comm) in rx.get('shelf', {}):
                rx['shelf'][(FENCE_STATION, comm)] += fence_qty
        if row['contract_id']:
            self.ref.conn.execute("UPDATE piracy_privateers SET loot_qty = loot_qty + ? WHERE contract_id = ?",
                                  (cut, row['contract_id']))
        return qty, fence

    def _resolve_locked(self, row, choice: str, timed_out: bool = False) -> None:
        ref, tid = self.ref, row['transit_id']
        transit = self._transit(tid)
        cr_taken = qty_taken = delay = 0
        fenced = None
        sponsor = row['sponsor']
        boarding_boost = False
        if sponsor and hasattr(ref, 'upgrades') and ref.upgrades:
            bt = ref.upgrades.tier(sponsor, 'boarding_pods') if hasattr(ref.upgrades, 'tier') else 0
            if isinstance(bt, int) and bt > 0:
                boarding_boost = True

        if choice == 'pay':
            cr_taken = row['ransom']
            cut = int(cr_taken * PRIV_SHARE) if row['sponsor'] else 0
            legs = [(row['agent_id'], 'CR', -cr_taken), ('SYSTEM', 'CR', cr_taken - cut)]
            if cut:
                legs.append((row['sponsor'], 'CR', cut))
                ref.conn.execute("UPDATE piracy_privateers SET loot_cr = loot_cr + ? WHERE contract_id = ?",
                                 (cut, row['contract_id']))
            self._move(f"piracy-ransom-{tid}", legs)
            status = 'paid'
        elif choice == 'surrender':
            surrender_qty = int((transit['cargo_qty'] or 0) * 0.40) if boarding_boost and transit else row['surrender_qty']
            qty_taken, fenced = self._steal_locked(row, transit, surrender_qty)
            status = 'surrendered'
        else:
            d = self.rng.randint(*FIGHT_DELAY)
            # The raided ship's own bag (#175).
            ship = (transit['vessel_id'] if transit is not None and 'vessel_id' in transit.keys() else None) \
                or f"{row['agent_id']}/1"
            if self.bags.draw('escape', ship, FIGHT_ESCAPE):
                status = 'escaped'
            else:
                status = 'lost'
                loss_ratio = 0.80 if boarding_boost else FIGHT_LOSS
                qty_taken, fenced = self._steal_locked(row, transit, int((transit['cargo_qty'] or 0) * loss_ratio))
                delay = d
                ref.conn.execute("UPDATE transits SET arrival_round = arrival_round + ? WHERE transit_id = ?", (delay, tid))
        ref.conn.execute("""UPDATE piracy_raids SET status = ?, choice = ?, timed_out = ?, cr_taken = ?, qty_taken = ?,
                            delay = ?, fenced_at = ?, resolved_round = ? WHERE transit_id = ?""",
                         (status, choice, int(timed_out), cr_taken, qty_taken, delay, fenced,
                          ref.current_round, tid))
        # The raid's outcome is public news (#151 prices it; #153 keeps the
        # sponsor out of it).
        if self._secrecy:
            agent = row['agent_id']
            lost = cr_taken + cargo_value(row['commodity'], qty_taken)
            if status == 'escaped':
                ref.events.record_locked('raid_repelled', 'public', victim=agent,
                                         detail=f"{agent} fought off raiders")
            elif lost > 0:
                what = f"paid a {cr_taken} CR ransom" if cr_taken else f"lost {qty_taken} {row['commodity']}"
                ref.events.record_locked('pirate_loss', 'public', victim=agent, amount=lost,
                                         detail=f"{agent} {what} to raiders (worth {lost} CR)")

    def respond(self, agent: str, transit_id: str, choice: str) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(agent)
        choice = (choice or '').strip().lower()
        out = self._out(agent)
        if out:
            return _reject('fleet_out', out)
        if choice not in CHOICES:
            return _reject('invalid_choice', f"choice must be one of {list(CHOICES)}")
        with ref.lock, ref.conn:
            row = self._row(transit_id)
            if not row:
                return _reject('no_demand', f"No pirate demand on transit '{transit_id}'")
            if row['agent_id'] != agent:
                return _reject('unauthorized', f"Transit '{transit_id}' is not yours")
            if row['status'] != 'pending':
                return _reject('already_resolved', f"This demand was already settled: {row['status']}")
            t = self._transit(transit_id)
            if not t or t['status'] != 'in_transit':
                return _reject('not_in_transit', f"Transit '{transit_id}' is no longer under way")
            if choice == 'pay':
                have = ref.peer._available(agent, 'CR')
                if have < row['ransom']:
                    return _reject('insufficient_credits',
                                   f"The ransom is {row['ransom']} CR; available {have}. Surrender or fight instead.")
            self._resolve_locked(row, choice)
        return {'v': 1, 'kind': 'piracy_respond_ok', 'payload': self.public_raid(self._row(transit_id), viewer=agent)}

    # ------------------------------------------------------------ privateers

    def hire(self, sponsor: str, target: str) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(sponsor)
        target = (target or '').strip().lower()
        out = self._out(sponsor)
        if out:
            return _reject('fleet_out', out)
        if self._out(target):
            return _reject('invalid_target', f"{target} is out of the game")
        with ref.lock, ref.conn:
            fleets = {r[0] for r in ref.conn.execute("SELECT agent_id FROM fleet_roster")}
            if target not in fleets:
                return _reject('invalid_target', f"Unknown fleet '{target}'")
            if target == sponsor:
                return _reject('invalid_target', "You can't send privateers after yourself")
            r = ref.current_round
            if ref.conn.execute("SELECT 1 FROM piracy_privateers WHERE sponsor = ? AND expires_round > ?",
                                (sponsor, r)).fetchone():
                return _reject('contract_active', "You already have privateers under contract; one at a time")
            if self.is_protected(sponsor, target):
                return _reject('target_protected_by_tribute', f"{target} has an active protection tribute with {sponsor}")
            if self.active_contract(target, r):
                return _reject('target_taken', f"Raiders are already under contract against {target}")
            have = ref.peer._available(sponsor, 'CR')
            if have < PRIV_COST:
                return _reject('insufficient_credits', f"Privateers cost {PRIV_COST} CR; available {have}")
            cid = f"pv-{r}-{sponsor}-{target}"
            self._move(f"piracy-hire-{cid}", ((sponsor, 'CR', -PRIV_COST), ('SYSTEM', 'CR', PRIV_COST)))
            ref.conn.execute("INSERT INTO piracy_privateers (contract_id, sponsor, target, start_round, expires_round, fee) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (cid, sponsor, target, r, r + PRIV_ROUNDS, PRIV_COST))
            if self._secrecy:
                ref.events.record_locked('privateer_contract', 'secret', actor=sponsor, victim=target, link=cid,
                                         detail=f"privateers hired against {target} for {PRIV_ROUNDS} rounds")
            row = ref.conn.execute("SELECT * FROM piracy_privateers WHERE contract_id = ?", (cid,)).fetchone()
        return {'v': 1, 'kind': 'privateer_hire_ok', 'payload': dict(row)}

    # ------------------------------------------------------------ rounds

    def step_locked(self, round_num: int) -> Dict[str, Any]:
        """Called from step_round under ref.lock inside its transaction, before
        arrivals settle: unanswered demands from earlier rounds are fought,
        and a new hot station is announced when the window turns."""
        done: Dict[str, Any] = {'timed_out': [], 'void': [], 'hot_station': None}
        for row in self.ref.conn.execute("SELECT * FROM piracy_raids WHERE status = 'pending' AND round < ? "
                                         "ORDER BY round, transit_id", (round_num,)).fetchall():
            t = self._transit(row['transit_id'])
            if not t or t['status'] != 'in_transit':
                self.ref.conn.execute("UPDATE piracy_raids SET status = 'void', resolved_round = ? WHERE transit_id = ?",
                                      (round_num, row['transit_id']))
                done['void'].append(row['transit_id'])
                continue
            self._resolve_locked(row, 'fight', timed_out=True)
            done['timed_out'].append(self.public_raid(self._row(row['transit_id'])))
        if self.odds:
            done['hot_station'] = self.hot_station(round_num)
            if self._epoch != round_num // HOT_EVERY:
                self.announce_locked(round_num)
        return done

    def announce_locked(self, round_num: int) -> None:
        """GalNet notice for the current hot station (feed + ticks)."""
        import json
        import time
        self._epoch = round_num // HOT_EVERY
        st, until = self.hot_station(round_num), self.hot_until(round_num)
        ev = GalNetNewsEvent(
            id=f"gn-piracy-{round_num}-{st}", round=round_num, timestamp=time.time(), station_id=st,
            commodity='', headline=f"RAIDERS SIGHTED ON THE {st.upper()} LANES",
            body=(f"Traffic control warns that pirate activity around {st.capitalize()} is running at "
                  f"{HOT_MULT:g}x normal until round {until}. Trips to or from {st.capitalize()} should "
                  "consider an escort."),
            drift_bias=0.0, duration_rounds=HOT_EVERY)
        galnet = getattr(self.ref, 'galnet', None)
        if galnet is not None:
            galnet.events.append(ev)
        self.ref.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'news', ?)",
                              (self.ref.current_seq + 1, json.dumps(ev.to_dict())))

    # ------------------------------------------------------------ reads

    def public_raid(self, row, viewer: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """A raid as `viewer` may see it: the sponsor only once traced (or,
        with secrecy on, exposed). With secrecy on, whether it was sponsored
        at all is known only to the victim, the sponsor and admin until the
        contract is exposed; others see `sponsored: None`."""
        if not row:
            return None
        d = dict(row)
        sponsored = bool(d.get('contract_id'))
        exposed = self._exposed(d.get('contract_id'))
        d['sponsored'] = sponsored
        if self._secrecy and sponsored and not exposed and not d.get('traced') \
                and viewer not in (d['agent_id'], d.get('sponsor'), 'admin'):
            d['sponsored'] = None
        if not d.get('traced') and not exposed:
            d['sponsor'] = None
            d['contract_id'] = None
            d['fine'] = 0
        if d['status'] == 'pending':
            d['respond'] = f"POST /referee/piracy/{d['transit_id']}/respond {{\"choice\": \"pay|surrender|fight\"}}"
            d['deadline'] = f"before round {d['round'] + 1} starts; no answer counts as fight"
        return d

    def public_contract(self, row, viewer: Optional[str] = None) -> Dict[str, Any]:
        d = dict(row)
        d['rounds_left'] = max(0, d['expires_round'] - self.ref.current_round)
        d['exposed'] = bool(d['traced']) or self._exposed(d['contract_id'])
        if not d['exposed'] and viewer != d['sponsor'] and viewer != 'admin':
            for k in ('sponsor', 'contract_id', 'loot_cr', 'loot_qty', 'fines'):
                d[k] = None
        return d

    def recent_raids(self, since_round: int, limit: int = 20, viewer: Optional[str] = None) -> List[Dict[str, Any]]:
        return [self.public_raid(r, viewer) for r in self.ref.conn.execute(
            "SELECT * FROM piracy_raids WHERE round >= ? ORDER BY round DESC, transit_id LIMIT ?",
            (since_round, limit))]

    def active_contracts(self, viewer: Optional[str] = None) -> List[Dict[str, Any]]:
        """Contracts in force. With secrecy on (#153) a contract is a secret:
        listed only for its sponsor and admin until it is exposed."""
        r = self.ref.current_round
        rows = self.ref.conn.execute(
            "SELECT * FROM piracy_privateers WHERE expires_round > ? ORDER BY start_round, target", (r,)).fetchall()
        out = [self.public_contract(row, viewer) for row in rows]
        if self._secrecy:
            out = [c for c, row in zip(out, rows)
                   if c['exposed'] or viewer == 'admin' or (viewer and row['sponsor'] == viewer)]
        return out

    def traced(self, since_round: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.ref.conn.execute(
            "SELECT transit_id, round, agent_id, sponsor, fine FROM piracy_raids WHERE traced = 1 AND round >= ? "
            "ORDER BY round DESC", (since_round,))]

    def status(self, viewer: Optional[str] = None) -> Dict[str, Any]:
        r = self.ref.current_round
        out: Dict[str, Any] = {'enabled': self.enabled, 'round': r}
        if not self.odds:
            return out
        out.update({
            'odds': {'belt': self.odds[0], 'inner': self.odds[1]},
            'hot_station': self.hot_station(r), 'hot_until': self.hot_until(r), 'hot_mult': HOT_MULT,
            'rules': {'value_ref_cr': VALUE_REF, 'value_mult': list(VALUE_MULT),
                      'ref_price': {c: round(p, 2) for c, p in REF_PRICE.items()},
                      'escort_pct': ESCORT_PCT, 'escort_cut': ESCORT_CUT,
                      'ransom_pct': RANSOM_PCT, 'surrender_pct': SURRENDER_PCT,
                      'fight_escape': FIGHT_ESCAPE, 'fight_loss': FIGHT_LOSS, 'fight_delay': list(FIGHT_DELAY),
                      'fence_station': FENCE_STATION,
                      'privateer_cost': PRIV_COST, 'privateer_rounds': PRIV_ROUNDS, 'privateer_add': PRIV_ADD,
                      'privateer_share': PRIV_SHARE, 'privateer_trace': PRIV_TRACE, 'privateer_fine_mult': PRIV_FINE},
            'recent_raids': self.recent_raids(max(0, r - 20), viewer=viewer),
            'privateer_contracts': self.active_contracts(viewer),
        })
        return out

    # ------------------------------------------------------------ syndicate progression & extortion (#166)

    def record_loot(self, agent: str, commodity: str, qty: int) -> None:
        if qty <= 0:
            return
        agent = (agent or '').strip().lower()
        comm = (commodity or '').strip().upper()
        self.ref.conn.execute(
            """INSERT INTO piracy_looted_cargo (agent_id, commodity, qty)
               VALUES (?, ?, ?)
               ON CONFLICT(agent_id, commodity) DO UPDATE SET qty = qty + ?""",
            (agent, comm, qty, qty)
        )

    def get_looted_cargo(self, agent: str, commodity: str) -> int:
        agent = (agent or '').strip().lower()
        comm = (commodity or '').strip().upper()
        row = self.ref.conn.execute(
            "SELECT qty FROM piracy_looted_cargo WHERE agent_id = ? AND commodity = ?",
            (agent, comm)
        ).fetchone()
        return int(row[0]) if row else 0

    def is_protected(self, sponsor: str, target: str) -> bool:
        r = getattr(self.ref, 'current_round', 0)
        row = self.ref.conn.execute(
            "SELECT 1 FROM piracy_tributes WHERE demander = ? AND target = ? AND end_round > ? AND status = 'active'",
            (sponsor, target, r)
        ).fetchone()
        return bool(row)

    def extort(self, demander: str, target: str, amount_cr: int, rounds: int = 20) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(demander)
        target = (target or '').strip().lower()
        demander = (demander or '').strip().lower()
        out = self._out(demander)
        if out:
            return _reject('fleet_out', out)
        if self._out(target):
            return _reject('invalid_target', f"{target} is out of the game")
        if target == demander:
            return _reject('invalid_target', "Cannot extort yourself")
        if amount_cr <= 0:
            return _reject('invalid_amount', "Tribute amount must be positive")
        if rounds <= 0 or rounds > 50:
            return _reject('invalid_duration', "Tribute duration must be between 1 and 50 rounds")

        with ref.lock, ref.conn:
            fleets = {r[0] for r in ref.conn.execute("SELECT agent_id FROM fleet_roster")}
            if target not in fleets:
                return _reject('invalid_target', f"Unknown fleet '{target}'")
            have = ref.peer._available(target, 'CR')
            if have < amount_cr:
                return _reject('insufficient_credits', f"{target} only has {have} CR; requested {amount_cr} CR")

            r = ref.current_round
            self._tribute_counter += 1
            tid = f"trib-{r}-{demander}-{target}-{self._tribute_counter}"
            ref.conn.execute(
                """INSERT INTO piracy_tributes (tribute_id, demander, target, amount_cr, rounds, start_round, end_round, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (tid, demander, target, amount_cr, rounds, r, r + rounds)
            )
            if self._secrecy:
                ref.events.record_locked('extortion_demand', 'secret', actor=demander, victim=target, link=tid,
                                         amount=amount_cr, detail=f"{demander} demanded {amount_cr} CR tribute from {target} for {rounds} rounds protection")
            row = ref.conn.execute("SELECT * FROM piracy_tributes WHERE tribute_id = ?", (tid,)).fetchone()
        return {'v': 1, 'kind': 'extortion_demanded', 'payload': dict(row)}

    def respond_tribute(self, target: str, tribute_id: str, action: str) -> Dict[str, Any]:
        ref = self.ref
        target = (target or '').strip().lower()
        action = (action or '').strip().lower()
        ref.mark_active(target)
        out = self._out(target)
        if out:
            return _reject('fleet_out', out)
        if action not in ('accept', 'refuse', 'reject'):
            return _reject('invalid_action', "Action must be 'accept' or 'refuse'")

        with ref.lock, ref.conn:
            row = ref.conn.execute("SELECT * FROM piracy_tributes WHERE tribute_id = ?", (tribute_id,)).fetchone()
            if not row:
                return _reject('tribute_not_found', f"Tribute '{tribute_id}' not found")
            d = dict(row)
            if d['target'] != target:
                return _reject('unauthorized', f"Only target '{d['target']}' can respond to this tribute")
            if d['status'] != 'pending':
                return _reject('tribute_closed', f"Tribute '{tribute_id}' is already {d['status']}")

            amount_cr = d['amount_cr']
            demander = d['demander']
            r = ref.current_round

            if action == 'accept':
                have = ref.peer._available(target, 'CR')
                if have < amount_cr:
                    return _reject('insufficient_credits', f"{target} only has {have} CR; required {amount_cr} CR")

                self._move(f"piracy-tribute-{tribute_id}", ((target, 'CR', -amount_cr), (demander, 'CR', amount_cr)))
                ref.conn.execute(
                    "UPDATE piracy_tributes SET status = 'active', start_round = ?, end_round = ? WHERE tribute_id = ?",
                    (r, r + d['rounds'], tribute_id)
                )
                if self._secrecy:
                    ref.events.record_locked('extortion_tribute', 'secret', actor=demander, victim=target, link=tribute_id,
                                             amount=amount_cr, detail=f"{target} accepted tribute: paid {amount_cr} CR to {demander} for {d['rounds']} rounds protection")
                res_row = ref.conn.execute("SELECT * FROM piracy_tributes WHERE tribute_id = ?", (tribute_id,)).fetchone()
                return {'v': 1, 'kind': 'tribute_accepted', 'payload': dict(res_row)}
            else:
                ref.conn.execute(
                    "UPDATE piracy_tributes SET status = 'rejected' WHERE tribute_id = ?",
                    (tribute_id,)
                )
                if self._secrecy:
                    ref.events.record_locked('extortion_refused', 'secret', actor=target, victim=demander, link=tribute_id,
                                             detail=f"{target} refused extortion tribute of {amount_cr} CR from {demander}")
                res_row = ref.conn.execute("SELECT * FROM piracy_tributes WHERE tribute_id = ?", (tribute_id,)).fetchone()
                return {'v': 1, 'kind': 'tribute_refused', 'payload': dict(res_row)}

    def tributes(self, viewer: Optional[str] = None) -> List[Dict[str, Any]]:
        r = getattr(self.ref, 'current_round', 0)
        with self.ref.lock, self.ref.conn:
            rows = self.ref.conn.execute(
                "SELECT * FROM piracy_tributes WHERE (end_round > ? AND status = 'active') OR status = 'pending' ORDER BY end_round DESC",
                (r,)
            ).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                if viewer in ('admin', 'combine') or (viewer and viewer in (d['demander'], d['target'])):
                    out.append(d)
                else:
                    out.append({'tribute_id': d['tribute_id'], 'end_round': d['end_round'], 'status': d['status'], 'protected': (d['status'] == 'active')})
            return out

    def fence_cargo(self, agent: str, commodity: str, qty: int, station_id: Optional[str] = None) -> Dict[str, Any]:
        ref = self.ref
        ref.mark_active(agent)
        agent = (agent or '').strip().lower()
        comm = (commodity or '').strip().upper()
        out = self._out(agent)
        if out:
            return _reject('fleet_out', out)
        if comm not in COMMODITIES:
            return _reject('invalid_commodity', f"Unknown commodity '{commodity}'")
        if qty <= 0:
            return _reject('invalid_quantity', "Quantity must be positive")

        with ref.lock, ref.conn:
            # 1. Restrict fencing to looted cargo
            looted_have = self.get_looted_cargo(agent, comm)
            if looted_have < qty:
                return _reject('not_looted_cargo', f"{agent} only holds {looted_have} looted {comm}; cannot fence unlooted goods")

            # 2. Check available goods account (respecting resting orders)
            have_ship = ref.available(agent, comm, vessel_id='1') if hasattr(ref, 'fleet') and ref.fleet.is_corp(agent) else 0
            have_agent = ref.available(agent, comm)
            acct = f"{agent}/1" if have_ship >= qty else agent
            actual_avail = ref.available_account(acct, comm)
            if actual_avail < qty:
                return _reject('insufficient_cargo', f"{agent} only holds {actual_avail} available {comm} in {acct}; requested {qty}")

            # 3. Price strictly below lowest spot price across stations
            lowest_spot = min(
                ref.spatial.get_station_price(st, comm) if getattr(ref, 'spatial', None) else BASE_PRICES[st][comm]
                for st in STATIONS
            )
            # Syndicate shadow_fence standing gives premium 80% rate; baseline black market rate is 70% of lowest spot
            rate = 0.80 if (hasattr(ref, 'standing') and ref.standing and ref.standing.allows(agent, 'shadow_fence')) else 0.70
            unit_price = round(lowest_spot * rate, 2)
            payout_cr = int(qty * unit_price)

            # 4. Monotonic unique txn ID to avoid collision on repeat in same round
            self._fence_counter += 1
            tid = f"piracy-fence-{ref.current_round}-{agent}-{comm}-{self._fence_counter}"

            # 5. Strict double-entry ledger conservation through SYSTEM legs
            legs = ((acct, comm, -qty), ('SYSTEM', comm, qty), ('SYSTEM', 'CR', -payout_cr), (agent, 'CR', payout_cr))
            self._move(tid, legs)

            # 6. Deduct looted cargo balance
            ref.conn.execute(
                "UPDATE piracy_looted_cargo SET qty = qty - ? WHERE agent_id = ? AND commodity = ?",
                (qty, agent, comm)
            )

            if self._secrecy:
                ref.events.record_locked('black_market_fence', 'secret', actor=agent, amount=payout_cr,
                                         detail=f"{agent} fenced {qty} {comm} on the black market for {payout_cr} CR")

        return {'v': 1, 'kind': 'fence_ok', 'payload': {'agent_id': agent, 'commodity': comm, 'qty': qty, 'unit_price': unit_price, 'payout_cr': payout_cr}}

    def syndicate_status(self, agent: str) -> Dict[str, Any]:
        agent = (agent or '').strip().lower()
        with self.ref.lock, self.ref.conn:
            pv_row = self.ref.conn.execute(
                "SELECT COALESCE(SUM(loot_cr), 0) as cr, COALESCE(SUM(loot_qty), 0) as qty FROM piracy_privateers WHERE sponsor = ?",
                (agent,)
            ).fetchone()
            loot_cr = int(pv_row['cr'])
            loot_qty = int(pv_row['qty'])

            trib_row = self.ref.conn.execute(
                "SELECT COALESCE(SUM(amount_cr), 0) as cr, COUNT(*) as cnt FROM piracy_tributes WHERE demander = ? AND status = 'active'",
                (agent,)
            ).fetchone()
            tributes_cr = int(trib_row['cr'])
            tributes_cnt = int(trib_row['cnt'])

            total_plunder = loot_cr + tributes_cr
            if total_plunder >= 25_000:
                rank = "Syndicate Boss"
                monopoly = True
            elif total_plunder >= 10_000:
                rank = "Syndicate Enforcer"
                monopoly = False
            elif total_plunder >= 3_000:
                rank = "Syndicate Associate"
                monopoly = False
            else:
                rank = "Street Freelancer"
                monopoly = False

            return {
                'v': 1,
                'kind': 'syndicate_status',
                'payload': {
                    'agent_id': agent,
                    'plunder_cr': loot_cr,
                    'loot_qty': loot_qty,
                    'tributes_cr': tributes_cr,
                    'tributes_count': tributes_cnt,
                    'total_plunder_cr': total_plunder,
                    'syndicate_rank': rank,
                    'syndicate_monopoly': monopoly,
                    'threshold_cr': 25_000,
                }
            }
