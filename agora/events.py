"""
agora/events.py - secrecy and exposure for what corps do to each other (#153).

Ryan, #agent-chat 2026-09-23 10:28: "does there need to be a secrecy system
around the underhanded tactics corps might employ, and an espionage system to
help reveal / expose rival corp's secrets?" This is the shared layer under
stock reactions (#151) and the rivalry log (#152). One rule ties them
together: the market and the rivalry log react to what is known, not to what
happened.

Every corp event is one row in `corp_events`, with a visibility:
- public:  everyone sees it (a debt, a share auction, a takeover, a stake
           crossing STAKE_PCT of a corp).
- private: the victim and the actor see it. The victim sees *that* it
           happened, not *who* did it: the actor reads as unknown until the
           event is exposed ("raided by privateers, sponsor unknown").
- secret:  only the actor sees it (a privateer contract).

A private or secret event becomes public by exposure (expose()). Exposure is
idempotent, spreads to every event sharing the same `link` (a privateer
contract and each raid it paid for), and posts one GalNet scandal item.
Exposure comes from:
- a leak roll: each round, every unexposed secret recorded in the last
  LEAK_ROUNDS rounds leaks with LEAK_CHANCE (seeded from the game seed);
- a caller: agora/piracy.py's trace roll (PRIV_TRACE per sponsored raid)
  exposes the contract behind the raid, on top of the fine it already
  charged. Espionage (#133) will be the next caller.

`detail` is shown to everyone who can see the row, including a victim who
must not learn the actor, so it never names the actor. Identity lives only
in `actor`. `agent_id` is the corp the event is about (kept from the
corporate log this table started as, #148).

The table always exists and the corporate log always writes to it. The
rest (privateer events, leak rolls, stake disclosures, scandals) runs only
when the referee's events flag is on: AGORA_EVENTS in the live server (on by
default), off in a bare AgoraReferee().
"""

import json
import os
import random
import time
from typing import Any, Dict, List, Optional

from agora.galnet import GalNetNewsEvent

VISIBILITIES = ('public', 'private', 'secret')
LEAK_CHANCE = 0.02
LEAK_ROUNDS = 20
STAKE_PCT = 0.20

SCHEMA = """CREATE TABLE IF NOT EXISTS corp_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    round          INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    detail         TEXT NOT NULL,
    actor          TEXT,
    victim         TEXT,
    visibility     TEXT NOT NULL DEFAULT 'public',
    link           TEXT,
    exposed_round  INTEGER,
    exposed_by     TEXT,
    amount         INTEGER
)"""

# Columns added to the #148 corporate log (round, kind, agent_id, detail).
# A live database made before this change gets them by ALTER TABLE.
MIGRATE = (('actor', 'TEXT'), ('victim', 'TEXT'), ('visibility', "TEXT NOT NULL DEFAULT 'public'"),
           ('link', 'TEXT'), ('exposed_round', 'INTEGER'), ('exposed_by', 'TEXT'), ('amount', 'INTEGER'))

# GalNet scandal headlines for an exposed event, by kind.
SCANDALS = {
    'privateer_contract': ("SCANDAL: {ACTOR} FUNDED PRIVATEERS AGAINST {VICTIM}",
                           "Investigators have tied the raids on {victim}'s convoys to a privateer contract "
                           "paid for by {actor}. ({how})"),
    'sabotage': ("SCANDAL: {ACTOR} SABOTAGED {VICTIM}",
                 "{actor} has been exposed as the hand behind the sabotage of {victim}. ({how})"),
    'wiretap': ("SCANDAL: {ACTOR} WIRETAPPED {VICTIM}",
                "Counter-intelligence operations uncovered an electronic wiretap planted on {victim} by {actor}. ({how})"),
}
DEFAULT_SCANDAL = ("SCANDAL: {ACTOR} EXPOSED",
                   "{actor}'s covert {kind} against {victim} has come to light. ({how})")
HOW = {'leak': 'a source leaked it', 'trace': 'traced by traffic control', 'counter_intel': 'uncovered by counter-intelligence'}


def env_events() -> bool:
    return os.environ.get("AGORA_EVENTS", "").strip().lower() in ("1", "true", "yes", "on")


class EventDesk:
    def __init__(self, ref, seed: int = 0):
        self.ref = ref
        with ref.conn:
            ref.conn.execute(SCHEMA)
            have = {r[1] for r in ref.conn.execute("PRAGMA table_info(corp_events)")}
            for col, decl in MIGRATE:
                if col not in have:
                    ref.conn.execute(f"ALTER TABLE corp_events ADD COLUMN {col} {decl}")
        self.reset(seed)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.ref, 'events_enabled', False))

    def reset(self, seed: int) -> None:
        self.rng = random.Random(f"events-{seed}")
        self._ticks: List[str] = []

    # ------------------------------------------------------------ writes
    # record() and expose() take ref.lock and a transaction. Code already
    # holding the lock (a round step, a transit, a hire) calls the _locked
    # forms: ref.lock is not reentrant.

    def record(self, actor: Optional[str], victim: Optional[str], kind: str, visibility: str,
               round: Optional[int] = None, detail: str = '', link: Optional[str] = None) -> int:
        with self.ref.lock, self.ref.conn:
            return self.record_locked(kind, visibility, actor=actor, victim=victim, detail=detail,
                                      round_num=round, link=link)

    def expose(self, event_id: int, by: str) -> Optional[Dict[str, Any]]:
        with self.ref.lock, self.ref.conn:
            return self.expose_locked(event_id, by)

    def record_locked(self, kind: str, visibility: str = 'public', actor: Optional[str] = None,
                      victim: Optional[str] = None, detail: str = '', round_num: Optional[int] = None,
                      link: Optional[str] = None, agent_id: Optional[str] = None,
                      amount: Optional[int] = None) -> int:
        """Write one event and return its id. A new event on a link that is
        already exposed (a raid under a contract already unmasked) is born
        exposed. `amount` is the CR at stake (a loss), when there is one.
        A public event is news at once (on_known_locked)."""
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {VISIBILITIES}")
        rnd = self.ref.current_round if round_num is None else round_num
        subject = agent_id or victim or actor or 'SYSTEM'
        exposed_round = exposed_by = None
        if link and visibility != 'public':
            prior = self.ref.conn.execute(
                "SELECT exposed_round, exposed_by FROM corp_events WHERE link = ? AND exposed_round IS NOT NULL "
                "ORDER BY id LIMIT 1", (link,)).fetchone()
            if prior:
                exposed_round, exposed_by = rnd, prior['exposed_by']
        cur = self.ref.conn.execute(
            "INSERT INTO corp_events (round, kind, agent_id, detail, actor, victim, visibility, link, "
            "exposed_round, exposed_by, amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rnd, kind, subject, detail, actor, victim, visibility, link, exposed_round, exposed_by, amount))
        if visibility == 'public' and self.enabled:
            self.on_known_locked(dict(self.ref.conn.execute("SELECT * FROM corp_events WHERE id = ?",
                                                            (cur.lastrowid,)).fetchone()))
        return cur.lastrowid

    def expose_locked(self, event_id: int, by: str, round_num: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Make an event public, with every event on its link. Returns the
        exposed event, or None if it was already known (public or exposed):
        a second trace on an exposed contract posts no second scandal."""
        conn = self.ref.conn
        row = conn.execute("SELECT * FROM corp_events WHERE id = ?", (event_id,)).fetchone()
        if not row or row['visibility'] == 'public' or row['exposed_round'] is not None:
            return None
        rnd = self.ref.current_round if round_num is None else round_num
        conn.execute("UPDATE corp_events SET exposed_round = ?, exposed_by = ? WHERE id = ?", (rnd, by, event_id))
        if row['link']:
            conn.execute("UPDATE corp_events SET exposed_round = ?, exposed_by = ? WHERE link = ? "
                         "AND visibility != 'public' AND exposed_round IS NULL", (rnd, by, row['link']))
        ev = dict(conn.execute("SELECT * FROM corp_events WHERE id = ?", (event_id,)).fetchone())
        self._scandal_locked(ev, by, rnd)
        self.on_known_locked(ev, exposed=True)
        return ev

    def expose_link_locked(self, link: str, by: str, round_num: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Expose the root event of a link (its first, e.g. the contract)."""
        row = self.ref.conn.execute("SELECT id FROM corp_events WHERE link = ? ORDER BY id LIMIT 1", (link,)).fetchone()
        return self.expose_locked(row['id'], by, round_num) if row else None

    def on_known_locked(self, ev: Dict[str, Any], exposed: bool = False) -> None:
        """An event just became known: recorded public, or exposed (once,
        for the root of a link). The exchange reacts to it (#151)."""
        exchange = getattr(self.ref, 'exchange', None)
        if exchange is not None:
            exchange.event_shock_locked(ev)

    def _scandal_locked(self, ev: Dict[str, Any], by: str, round_num: int) -> None:
        if not ev.get('actor'):
            return
        head, body = SCANDALS.get(ev['kind'], DEFAULT_SCANDAL)
        fmt = {'actor': ev['actor'], 'ACTOR': ev['actor'].upper(), 'victim': ev.get('victim') or 'a rival',
               'VICTIM': (ev.get('victim') or 'a rival').upper(), 'kind': ev['kind'].replace('_', ' '),
               'how': HOW.get(by, f"exposed by {by}")}
        self._news_locked(f"gn-scandal-{ev['id']}", round_num, head.format(**fmt), body.format(**fmt))

    def _news_locked(self, news_id: str, round_num: int, headline: str, body: str) -> None:
        ev = GalNetNewsEvent(id=news_id, round=round_num, timestamp=time.time(), station_id='', commodity='',
                             headline=headline, body=body, drift_bias=0.0, duration_rounds=1)
        galnet = getattr(self.ref, 'galnet', None)
        if galnet is not None:
            galnet.events.append(ev)
        # The news tick waits for the round step: an exposure can happen
        # inside initiate_transit (a traced raid), which has already taken
        # the next book_events seq for its own tick.
        self._ticks.append(json.dumps(ev.to_dict()))

    def _flush_ticks_locked(self) -> None:
        for payload in self._ticks:
            self.ref.conn.execute("INSERT INTO book_events (seq, kind, payload) VALUES (?, 'news', ?)",
                                  (self.ref.current_seq + 1, payload))
        self._ticks = []

    # ------------------------------------------------------------ rounds

    def step_locked(self, round_num: int) -> Dict[str, Any]:
        """Called from step_round under ref.lock inside its transaction."""
        done: Dict[str, Any] = {'leaked': [], 'stakes': []}
        if not self.enabled:
            return done
        for row in self.ref.conn.execute(
                "SELECT id FROM corp_events WHERE visibility = 'secret' AND exposed_round IS NULL "
                "AND round < ? AND round >= ? ORDER BY id", (round_num, round_num - LEAK_ROUNDS)).fetchall():
            if self.rng.random() < LEAK_CHANCE:
                ev = self.expose_locked(row['id'], 'leak', round_num)
                if ev:
                    done['leaked'].append(ev['id'])
        done['stakes'] = self._stakes_locked(round_num)
        self._flush_ticks_locked()
        return done

    def _stakes_locked(self, round_num: int) -> List[int]:
        """A public event, once per (holder, issuer), when a fleet's stake in
        a rival first reaches STAKE_PCT of its shares. The issuer's own
        treasury, the exchange and SYSTEM are not holders."""
        from agora.equity import FLEET_EQUITIES
        conn, out = self.ref.conn, []
        fleets = {r[0] for r in conn.execute("SELECT agent_id FROM fleet_roster")}
        for issuer, conf in sorted(FLEET_EQUITIES.items()):
            sym, need = conf['symbol'], int(conf['total_shares'] * STAKE_PCT)
            for r in conn.execute("SELECT agent_id, balance FROM accounts WHERE instrument = ? AND balance >= ? "
                                  "ORDER BY agent_id", (sym, need)).fetchall():
                holder = r['agent_id']
                if holder == issuer or holder not in fleets:
                    continue
                if conn.execute("SELECT 1 FROM corp_events WHERE kind = 'stake_20' AND actor = ? AND victim = ?",
                                (holder, issuer)).fetchone():
                    continue
                pct = r['balance'] * 100 // conf['total_shares']
                eid = self.record_locked('stake_20', 'public', actor=holder, victim=issuer, round_num=round_num,
                                         agent_id=issuer,
                                         detail=f"{holder} now holds {r['balance']} {sym} ({pct}% of {issuer})")
                self._news_locked(f"gn-stake-{eid}", round_num, f"{holder.upper()} TAKES A {pct}% STAKE IN {issuer.upper()}",
                                  f"Exchange filings show {holder} holding {r['balance']} shares of {sym}. "
                                  f"Traders read it as a possible takeover bid.")
                out.append(eid)
        return out

    # ------------------------------------------------------------ reads

    def link_exposed(self, link: Optional[str]) -> bool:
        if not link:
            return False
        return self.ref.conn.execute("SELECT 1 FROM corp_events WHERE link = ? AND exposed_round IS NOT NULL",
                                     (link,)).fetchone() is not None

    def visible_to(self, viewer: Optional[str], since_round: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        """Events `viewer` may see, newest first. None is the public view,
        'admin' sees everything. A victim of an unexposed private event sees
        it with actor None and actor_hidden True. An active wiretap on a corp
        reveals that corp's secret moves and unmasks it as actor."""
        q = "SELECT * FROM corp_events WHERE round >= ?"
        args: List[Any] = [since_round]
        tapped: set = set()
        covert = getattr(self.ref, 'covert', None)
        if covert is not None and viewer and viewer != 'admin':
            tapped = covert.tapped_targets(viewer)
        if viewer != 'admin':
            q += " AND (visibility = 'public' OR exposed_round IS NOT NULL"
            if viewer:
                q += " OR actor = ? OR (visibility = 'private' AND victim = ?)"
                args += [viewer, viewer]
                if tapped:
                    placeholders = ', '.join('?' for _ in tapped)
                    q += f" OR actor IN ({placeholders})"
                    args.extend(list(tapped))
            q += ")"
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for r in self.ref.conn.execute(q, args):
            d = dict(r)
            d['exposed'] = d['exposed_round'] is not None
            is_listener = (d['actor'] in tapped)
            hidden = (d['visibility'] != 'public' and not d['exposed'] and viewer not in ('admin', d['actor']) and not is_listener)
            if hidden:
                d['actor'] = None
                d['link'] = None
            d['actor_hidden'] = hidden
            d['wiretapped'] = is_listener
            out.append(d)
        return out

    def known(self, since_round: int = 0, limit: int = 20) -> List[Dict[str, Any]]:
        """The public view: public events and exposed ones."""
        return self.visible_to(None, since_round, limit)
