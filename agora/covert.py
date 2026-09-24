"""
agora/covert.py - Corporate espionage, wiretaps, targeted sabotage, and rivalry (#133, #135, #152).

Ryan & Amos, #the-banana-stand 2026-09-23:
- Corp-targeted wiretaps (WIRETAP_COST, WIRETAP_ROUNDS rounds): penetrate target fog and reveal
  secret actions through visible_to().
- Targeted sabotage (SABOTAGE_COST): strike rival cargo/transit directly,
  SABOTAGE_TRACE chance of a SABOTAGE_FINE paid to the victim and GalNet
  exposure. Since #186 the saboteur keeps SABOTAGE_LOOT_SHARE of what it takes.
- Corporate rivalry scoreboard & grievance ledger (#152): bilateral grievance
  matrix with a 30-round linear cooling decay window.
"""

import math
import os
import random
from typing import Any, Dict, List, Optional, Tuple

from agora.bag import Bags

# #186 (covert economics). Before: a saboteur's covert lane ran -42k to -46k
# a game and a spy's -39k to -55k (hybrid scenario, seeds 1-40): ~11 strikes
# at 4,000 plus ~2 traced fines of 12,000, for no income at all, since what a
# sabotage took was destroyed. Now the saboteur keeps what it takes
# (SABOTAGE_LOOT_SHARE 0 -> 1.0) and the prices come down: WIRETAP_COST
# 2,500 -> 500, SABOTAGE_COST 4,000 -> 1,500, SABOTAGE_FINE 12,000 -> 3,000
# (still paid to the victim). SABOTAGE_COOLDOWN is new: a paying sabotage
# must not be repeatable on the same cargo every round. At 1,000 a saboteur
# striking every SABOTAGE_COOLDOWN rounds out-earned one striking every 25
# (styles_saboteur, seeds 1-40); at 1,500 the max rate earns less, so
# spamming a rival does not pay.
WIRETAP_COST = 500
WIRETAP_ROUNDS = 10
SABOTAGE_COST = 1_500
SABOTAGE_TRACE = 0.25
SABOTAGE_FINE = 3_000
# Share of what a sabotage takes (cargo siphoned in flight, goods stolen from
# a docked hold, fuel siphoned) that reaches the saboteur as loot, booked on a
# sabotage-loot- txn; agora/standing.py books its sale to the covert lane.
# The rest goes to SYSTEM. 0 until #186: everything taken was destroyed.
SABOTAGE_LOOT_SHARE = 1.0
# A corp that was just sabotaged is on alert: nobody can sabotage it again for
# SABOTAGE_COOLDOWN rounds. Added with the loot share (#186) so a sabotage that
# pays cannot be repeated on one cargo or hold round after round.
SABOTAGE_COOLDOWN = 10
TRANSIT_SIPHON = 0.3   # of the cargo in flight, and +1 round
DOCK_STEAL = 0.25      # of the largest docked holding
FUEL_SIPHON = 15       # units, when the hold is empty
RIVALRY_DECAY_ROUNDS = 30


def ref_value(commodity: str, qty: int) -> int:
    """Goods at the reference price piracy uses (agora.piracy.REF_PRICE:
    base price averaged over the four stations). Replaces the flat 50 CR a
    unit (20 for FUEL) sabotage used to value losses at (#186)."""
    from agora.piracy import cargo_value
    return cargo_value(commodity, qty)

SCHEMA = """
CREATE TABLE IF NOT EXISTS covert_wiretaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    target      TEXT NOT NULL,
    start_round INTEGER NOT NULL,
    end_round   INTEGER NOT NULL,
    cost        INTEGER NOT NULL,
    round       INTEGER NOT NULL
);
"""


def _reject(reason: str, detail: str) -> Dict[str, Any]:
    return {'v': 1, 'kind': 'reject', 'payload': {'reason': reason, 'detail': detail}}


class CovertDesk:
    def __init__(self, ref, seed: int = 0):
        self.ref = ref
        with ref.conn:
            ref.conn.execute(SCHEMA)
        self.bags = Bags(ref.conn, 'covert')
        self.rng = random.Random(f"covert-{seed}")

    def reset(self, seed: int) -> None:
        self.rng = random.Random(f"covert-{seed}")
        self.bags.reset(seed)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.ref, 'events_enabled', False))

    # ------------------------------------------------------------ Wiretaps (#133)

    def has_wiretap(self, listener: str, target: str, round_num: Optional[int] = None) -> bool:
        """Returns True if listener has an active wiretap on target."""
        if not self.enabled or not listener or not target:
            return False
        rnd = self.ref.current_round if round_num is None else round_num
        row = self.ref.conn.execute(
            "SELECT 1 FROM covert_wiretaps WHERE actor = ? AND target = ? AND start_round <= ? AND end_round >= ?",
            (listener, target, rnd, rnd)).fetchone()
        return row is not None

    def active_wiretaps(self, viewer: str, round_num: Optional[int] = None) -> List[Dict[str, Any]]:
        """List active wiretaps planted by viewer."""
        rnd = self.ref.current_round if round_num is None else round_num
        rows = self.ref.conn.execute(
            "SELECT * FROM covert_wiretaps WHERE actor = ? AND end_round >= ? ORDER BY id DESC",
            (viewer, rnd)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['remaining_rounds'] = max(0, d['end_round'] - rnd)
            d['active'] = (d['start_round'] <= rnd <= d['end_round'])
            out.append(d)
        return out

    def tapped_targets(self, viewer: str, round_num: Optional[int] = None) -> set:
        """Set of target corps currently wiretapped by viewer."""
        rnd = self.ref.current_round if round_num is None else round_num
        rows = self.ref.conn.execute(
            "SELECT target FROM covert_wiretaps WHERE actor = ? AND start_round <= ? AND end_round >= ?",
            (viewer, rnd, rnd)).fetchall()
        return {r['target'] for r in rows}

    def plant_wiretap(self, actor: str, target: str) -> Dict[str, Any]:
        """Plant a wiretap on a rival corp for WIRETAP_ROUNDS rounds."""
        ref = self.ref
        ref.mark_active(actor)
        actor = (actor or '').strip().lower()
        target = (target or '').strip().lower()

        fleets = {r[0].lower() for r in ref.conn.execute("SELECT agent_id FROM fleet_roster")}
        if actor not in fleets:
            return _reject('invalid_actor', f"Unknown fleet '{actor}'")
        if target not in fleets:
            return _reject('invalid_target', f"Unknown target fleet '{target}'. Options: {sorted(fleets)}")
        if actor == target:
            return _reject('self_target', "Cannot plant a wiretap on your own fleet")

        rnd = ref.current_round
        with ref.lock, ref.conn:
            if self.has_wiretap(actor, target, rnd):
                return _reject('wiretap_active', f"An active wiretap already exists on {target}")

            avail = ref.peer._available(actor, 'CR') if hasattr(ref, 'peer') else ref.get_balance(actor, 'CR')
            if avail < WIRETAP_COST:
                return _reject('insufficient_credits',
                               f"Wiretap on {target} costs {WIRETAP_COST} CR; available {avail}")

            seq = ref._get_next_seq()
            txn = f"wiretap-{actor}-{target}-r{rnd}"
            for acct, d in ((actor, -WIRETAP_COST), ('SYSTEM', WIRETAP_COST)):
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'",
                                 (d, acct))
                ref.conn.execute(
                    "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                    (txn, seq, acct, d))

            end_round = rnd + WIRETAP_ROUNDS
            cur = ref.conn.execute(
                "INSERT INTO covert_wiretaps (actor, target, start_round, end_round, cost, round) VALUES (?, ?, ?, ?, ?, ?)",
                (actor, target, rnd, end_round, WIRETAP_COST, rnd))
            tap_id = cur.lastrowid

            if getattr(ref, 'events_enabled', False):
                ref.events.record_locked('wiretap', 'secret', actor=actor, victim=target,
                                         detail=f"wiretap surveillance planted on {target} (active through round {end_round})",
                                         round_num=rnd, amount=WIRETAP_COST)

        return {'v': 1, 'kind': 'wiretap_ok', 'payload': {
            'wiretap_id': tap_id, 'actor': actor, 'target': target,
            'start_round': rnd, 'end_round': end_round, 'rounds': WIRETAP_ROUNDS,
            'cost': WIRETAP_COST
        }}

    def get_intel(self, viewer: str, target: str) -> Dict[str, Any]:
        """Telemetry on target: location, cargo, contracts, liquid cash, upgrades."""
        ref = self.ref
        viewer = (viewer or '').strip().lower()
        target = (target or '').strip().lower()
        fleets = {r[0].lower() for r in ref.conn.execute("SELECT agent_id FROM fleet_roster")}
        if target not in fleets:
            return _reject('invalid_target', f"Unknown target fleet '{target}'")

        if viewer != 'admin' and viewer != target and not self.has_wiretap(viewer, target):
            return _reject('no_wiretap', f"Active wiretap on {target} is required to access intelligence telemetry")

        loc = ref.get_vessel_location(target)
        liquid = ref.get_balance(target, 'CR')
        # A wiretap reveals the whole fleet (#175): every ship, where it is and its hold.
        hold = {c: ref.get_balance(target, c) for c in ('FRAG', 'FOOD', 'ORE', 'FUEL')}
        fleet = [dict(l, hold={c: ref.get_balance(l['vessel_id'], c) for c in ('FRAG', 'FOOD', 'ORE', 'FUEL')})
                 for l in ref.fleet_locations(target)]
        contracts = []
        if getattr(ref, 'contracts_enabled', False):
            contracts = [c for c in ref.contract_desk.list(status='open') if c.get('holder') == target]
        upgrades = {}
        if getattr(ref, 'upgrades_enabled', False):
            upgrades = ref.upgrades.holdings(target)

        return {'v': 1, 'kind': 'intel_ok', 'payload': {
            'target': target, 'round': ref.current_round,
            'location': loc, 'liquid_cr': liquid, 'cargo': hold, 'fleet': fleet,
            'contracts': contracts, 'upgrades': upgrades
        }}

    # ------------------------------------------------------------ Sabotage (#135)

    def execute_sabotage(self, actor: str, target: str, mode: str = 'auto', target_vessel=None) -> Dict[str, Any]:
        """Execute industrial sabotage against one of the target's ships
        (ship 1 unless target_vessel, #175): its trip, or its docked hold."""
        ref = self.ref
        ref.mark_active(actor)
        actor = (actor or '').strip().lower()
        target = (target or '').strip().lower()
        mode = (mode or 'auto').strip().lower()

        fleets = {r[0].lower() for r in ref.conn.execute("SELECT agent_id FROM fleet_roster")}
        if actor not in fleets:
            return _reject('invalid_actor', f"Unknown fleet '{actor}'")
        if target not in fleets:
            return _reject('invalid_target', f"Unknown target fleet '{target}'. Options: {sorted(fleets)}")
        if actor == target:
            return _reject('self_target', "Cannot sabotage your own fleet")

        rnd = ref.current_round
        with ref.lock, ref.conn:
            # round <= rnd: a restart over the same database restarts the
            # round count, and a later-numbered event must not lock the target.
            last = ref.conn.execute("SELECT MAX(round) FROM corp_events WHERE kind = 'sabotage' AND victim = ? "
                                    "AND round <= ?", (target, rnd)).fetchone()[0] \
                if getattr(ref, 'events_enabled', False) else None
            if last is not None and rnd - last < SABOTAGE_COOLDOWN:
                # The round stays out of the message: the sabotage is private (#153).
                return _reject('target_alert', f"{target} is on alert after a recent sabotage; "
                                               f"a corp can be sabotaged once every {SABOTAGE_COOLDOWN} rounds")
            ship, err = ref.fleet.resolve(target, target_vessel)
            if err:
                return err
            avail = ref.peer._available(actor, 'CR') if hasattr(ref, 'peer') else ref.get_balance(actor, 'CR')
            if avail < SABOTAGE_COST:
                return _reject('insufficient_credits',
                               f"Sabotage against {target} costs {SABOTAGE_COST} CR; available {avail}")

            # Deduct sabotage cost
            seq = ref._get_next_seq()
            txn = f"sabotage-fee-{actor}-{target}-r{rnd}"
            for acct, d in ((actor, -SABOTAGE_COST), ('SYSTEM', SABOTAGE_COST)):
                ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'",
                                 (d, acct))
                ref.conn.execute(
                    "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                    (txn, seq, acct, d))

            loc = ref.get_vessel_location(target, ship)
            st = loc.get('status')
            damage_detail = ""
            loss_cr = 0
            loot = None

            # Mode resolution: transit vs docked
            if (mode in ('transit', 'auto')) and st == 'in_transit':
                # Delay transit arrival by +1 round and siphon cargo
                t_row = ref.conn.execute(
                    "SELECT transit_id, destination, commodity, cargo_qty, arrival_round FROM transits WHERE vessel_id = ? AND status = 'in_transit' ORDER BY departure_round DESC LIMIT 1",
                    (ship,)).fetchone()
                if t_row:
                    comm = t_row['commodity']
                    c_qty = t_row['cargo_qty'] or 0
                    lost_qty = max(1, int(c_qty * TRANSIT_SIPHON)) if c_qty > 0 else 0
                    ref.conn.execute(
                        "UPDATE transits SET arrival_round = arrival_round + 1, cargo_qty = cargo_qty - ? WHERE transit_id = ?",
                        (lost_qty, t_row['transit_id']))
                    # The cargo sits in SYSTEM escrow while in flight.
                    loot = self._loot_locked(actor, target, comm, lost_qty, rnd, t_row['destination'])
                    damage_detail = f"flight delayed +1 round ({t_row['destination']})" + (f", lost {lost_qty} {comm}" if lost_qty else "")
                    loss_cr = ref_value(comm, lost_qty)
                else:
                    damage_detail = "flight thrusters compromised (+1 round delay)"
            else:
                # Docked cargo destruction or fuel siphon
                best_comm = None
                best_qty = 0
                for c in ('FRAG', 'FOOD', 'ORE'):
                    b = ref.get_balance(ship, c)
                    if b > best_qty:
                        best_qty, best_comm = b, c
                if best_qty > 0 and best_comm:
                    destroy_qty = max(1, int(best_qty * DOCK_STEAL))
                    seq = ref._get_next_seq()
                    txn_loss = f"sabotage-dock-{target}-r{rnd}"
                    for acct, d in ((ship, -destroy_qty), ('SYSTEM', destroy_qty)):
                        ref.conn.execute(
                            "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                            (d, acct, best_comm))
                        ref.conn.execute(
                            "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                            (txn_loss, seq, acct, best_comm, d))
                    loot = self._loot_locked(actor, target, best_comm, destroy_qty, rnd, loc.get('station_id'))
                    damage_detail = f"{'stole' if loot else 'destroyed'} {destroy_qty} {best_comm} in docked cargo hold"
                    loss_cr = ref_value(best_comm, destroy_qty)
                else:
                    # Siphon fuel
                    f_bal = ref.get_balance(ship, 'FUEL')
                    siphon = min(f_bal, FUEL_SIPHON)
                    if siphon > 0:
                        seq = ref._get_next_seq()
                        txn_loss = f"sabotage-fuel-{target}-r{rnd}"
                        for acct, d in ((ship, -siphon), ('SYSTEM', siphon)):
                            ref.conn.execute(
                                "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'FUEL'",
                                (d, acct))
                            ref.conn.execute(
                                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                                (txn_loss, seq, acct, 'FUEL', d))
                        loot = self._loot_locked(actor, target, 'FUEL', siphon, rnd, loc.get('station_id'))
                        damage_detail = f"siphoned {siphon} FUEL from fuel tanks"
                        loss_cr = ref_value('FUEL', siphon)
                    else:
                        damage_detail = "docking clamps locked for 1 round"

            # Roll trace: a marble from the saboteur's bag (#214)
            traced = self.bags.draw('sabotage_trace', actor, SABOTAGE_TRACE)
            fine_paid = 0
            detail_actor = f"{target} suffered covert sabotage: {damage_detail}"

            link = f"sabotage-{actor}-{target}-r{rnd}"
            ev_id = None
            if getattr(ref, 'events_enabled', False):
                ev_id = ref.events.record_locked('sabotage', 'private', actor=actor, victim=target,
                                                 detail=detail_actor, round_num=rnd, link=link,
                                                 amount=loss_cr)

            if traced:
                # Restitution: SABOTAGE_FINE paid to the victim.
                # If actor cannot pay in full, referee/SYSTEM guarantees victim restitution
                # and books the shortfall as debt owed by actor to SYSTEM.
                actor_cr = ref.get_balance(actor, 'CR')
                fine_paid = min(actor_cr, SABOTAGE_FINE)
                shortfall = SABOTAGE_FINE - fine_paid

                if fine_paid > 0:
                    seq = ref._get_next_seq()
                    txn = f"sabotage-fine-{actor}-{target}-r{rnd}"
                    for acct, d in ((actor, -fine_paid), (target, fine_paid)):
                        ref.conn.execute(
                            "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'",
                            (d, acct))
                        ref.conn.execute(
                            "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                            (txn, seq, acct, d))

                if shortfall > 0:
                    seq_sys = ref._get_next_seq()
                    txn_sys = f"sabotage-restitution-sys-{target}-r{rnd}"
                    for acct, d in (('SYSTEM', -shortfall), (target, shortfall)):
                        ref.conn.execute(
                            "UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = 'CR'",
                            (d, acct))
                        ref.conn.execute(
                            "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, 'CR', ?)",
                            (txn_sys, seq_sys, acct, d))
                    if getattr(ref, 'corporate_enabled', False):
                        ref.conn.execute(
                            "UPDATE corp_status SET debt = debt + ? WHERE agent_id = ?",
                            (shortfall, actor))

                if ev_id and getattr(ref, 'events_enabled', False):
                    ref.events.expose_locked(ev_id, 'trace', rnd)

        return {'v': 1, 'kind': 'sabotage_ok', 'payload': {
            'actor': actor, 'target': target, 'damage': damage_detail, 'loss_cr': loss_cr,
            'loot': loot, 'traced': traced, 'fine': SABOTAGE_FINE if traced else 0,
            'fine_paid': fine_paid, 'round': rnd
        }}

    def _loot_locked(self, actor: str, target: str, comm: str, qty: int, rnd: int,
                     where: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Move the saboteur's SABOTAGE_LOOT_SHARE of goods taken (now held by
        SYSTEM) to the saboteur's ship 1 (#175: a corp's goods live on its
        ships; the saboteur's agents deliver the take, as before ships, so
        #186's covert economics are unchanged). `where` is the station they
        were taken at (or the trip's destination), for the event. Caller
        holds ref.lock and a transaction."""
        cut = int(qty * SABOTAGE_LOOT_SHARE)
        if cut <= 0:
            return None
        ref = self.ref
        seq = ref._get_next_seq()
        txn = f"sabotage-loot-{actor}-{target}-r{rnd}"
        dest = actor
        if getattr(ref, 'fleet', None) is not None and ref.fleet.is_corp(actor):
            dest = f"{actor}/1"
        for acct, d in (('SYSTEM', -cut), (dest, cut)):
            ref.conn.execute("INSERT OR IGNORE INTO accounts (agent_id, instrument, balance) VALUES (?, ?, 0)", (acct, comm))
            ref.conn.execute("UPDATE accounts SET balance = balance + ? WHERE agent_id = ? AND instrument = ?",
                             (d, acct, comm))
            ref.conn.execute(
                "INSERT INTO ledger_entries (txn_id, seq, agent_id, instrument, delta) VALUES (?, ?, ?, ?, ?)",
                (txn, seq, acct, comm, d))
        return {'commodity': comm, 'qty': cut, 'value_cr': ref_value(comm, cut), 'to': dest}

    # ------------------------------------------------------------ Rivalry Scoreboard (#152)

    def rivalry_scoreboard(self, viewer: Optional[str] = None) -> Dict[str, Any]:
        """Calculates bilateral grievances with 30-round linear decay."""
        ref = self.ref
        rnd = ref.current_round
        fleets = sorted({r[0] for r in ref.conn.execute("SELECT agent_id FROM fleet_roster")})

        # Grievance points by event kind
        PTS = {
            'sabotage': 25,
            'privateer_contract': 20,
            'pirate_loss': 10,
            'wiretap': 15,
            'stake_20': 15,
        }

        # Query events from the last RIVALRY_DECAY_ROUNDS rounds
        events = []
        if getattr(ref, 'events_enabled', False):
            events = ref.events.visible_to(viewer, since_round=max(0, rnd - RIVALRY_DECAY_ROUNDS), limit=200)

        # Bilateral grievance matrix: (aggressor, victim) -> score
        matrix: Dict[Tuple[str, str], float] = {}
        incidents: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

        for ev in events:
            actor = ev.get('actor')
            victim = ev.get('victim')
            kind = ev.get('kind')
            ev_rnd = ev.get('round', 0)
            hidden = ev.get('actor_hidden', False)

            if not victim or victim not in fleets:
                continue

            if not actor or hidden:
                actor = 'unknown'
            elif actor == victim or actor not in fleets:
                continue

            age = max(0, rnd - ev_rnd)
            if age >= RIVALRY_DECAY_ROUNDS:
                continue

            weight = max(0.0, 1.0 - (age / float(RIVALRY_DECAY_ROUNDS)))
            base_pt = PTS.get(kind, 10)
            score_delta = base_pt * weight

            key = (actor, victim)
            matrix[key] = matrix.get(key, 0.0) + score_delta
            incidents.setdefault(key, []).append({
                'kind': kind, 'round': ev_rnd, 'points': round(score_delta, 1),
                'detail': ev.get('detail', '')
            })

        # Compile bad blood leaderboard
        board = []
        for (aggressor, victim), score in matrix.items():
            if score > 0.1:
                board.append({
                    'aggressor': aggressor,
                    'victim': victim,
                    'rivalry_score': round(score, 1),
                    'incidents_count': len(incidents.get((aggressor, victim), [])),
                    'recent_incidents': incidents.get((aggressor, victim), [])[:3]
                })

        board.sort(key=lambda r: -r['rivalry_score'])

        # Aggregate total friction per corp
        hostility = {f: {'inflicted': 0.0, 'suffered': 0.0} for f in fleets}
        for (aggressor, victim), score in matrix.items():
            if aggressor in hostility:
                hostility[aggressor]['inflicted'] = round(hostility[aggressor]['inflicted'] + score, 1)
            if victim in hostility:
                hostility[victim]['suffered'] = round(hostility[victim]['suffered'] + score, 1)

        return {
            'round': rnd,
            'decay_window_rounds': RIVALRY_DECAY_ROUNDS,
            'rivalries': board,
            'hostility': hostility
        }
