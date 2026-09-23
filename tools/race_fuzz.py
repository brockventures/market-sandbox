#!/usr/bin/env python3
"""
race_fuzz.py -- concurrent ledger fuzzer for the AGORA HTTP API.

Starts the real HTTP handler (agora.server.make_handler on a
ThreadingHTTPServer) in-process on a free port against a fresh on-disk
SQLite DB, then runs N worker threads that hammer the API concurrently with
randomized valid and invalid actions -- station and stock-exchange orders,
cancels, cancel_all, duplicate order ids, transits, peer trades, contracts,
covert ops, upgrades, piracy, equity loans, salvage, circuit breaker, GalNet,
the unlocked admin fleet-roster write, and a spread of GET reads -- while a
separate thread steps rounds concurrently.

After the run, and every --check-every completed ops during it, a checker
takes the referee's lock and verifies:

  * verify_ledger_invariants() (per-txn conservation, non-negativity,
    account <-> ledger reconciliation)
  * per-(txn_id, instrument) conservation (the built-in check groups by
    txn_id only, so a CR leg netting against a FRAG leg would pass it)
  * supply: every instrument's SUM(accounts.balance) equals its SUM(ledger
    delta) and its supply right after genesis
  * orphaned escrow: SYSTEM's net position from peer-trade txns equals what
    open/accepted station_escrow rows hold; SYSTEM's net from equity
    collateral txns equals the collateral of active loans; contract bonds
    exist only on open, owned contracts
  * the in-memory order books match the DB `orders` table (every resting
    order is an open row with the same remaining qty, and every open row
    with remaining qty is resting)
  * vessels: for every fleet, vessels['<corp>/1'].station_id,
    vessel_locations.station_id and "has an in_transit transit" agree

Also recorded, but not by themselves a failure unless --fail-on-server-error:
unhandled exceptions inside the server's request threads (captured via
handle_error), per-endpoint HTTP status histograms, and a coverage summary
read back from the DB (trades, transits, peer escrows, contracts, loans...)
so a run that never reached the interesting code says so.

A stall watchdog dumps every thread's stack and fails if no op completes
for --stall-timeout seconds.

Exit codes: 0 clean, 1 invariant violation or stall, 2 harness error.

Replay: each worker's RNG is seeded from (seed, worker id), so each thread
draws the same sequence of choices for a given seed. Thread interleaving,
server-assigned ids (escrow, transit, contract, loan) and anything a worker
learned from a response are not deterministic, so a replay re-runs the same
op mix, not the identical schedule. On a violation the tool prints the seed,
the exact command line, and the last --last-k ops of every thread; with
--log-jsonl it writes every op.

    python tools/race_fuzz.py --seed 1 --threads 8 --ops 4000 --rounds 40
    python tools/race_fuzz.py --seed 1 --serial --ops 1500      # no concurrency

Hunting past a known finding, and seeing who touched the connection:

    python tools/race_fuzz.py --seed 2 --ops 4000 --rounds 40 \\
        --weights admin_fleets=20,equity=15 --skip-checks vessels,book-db \\
        --trace-sql 300000 --keep-db

--trace-sql logs every statement, commit and rollback on the shared
connection with the thread that issued it, and writes the log next to the
kept DB on a violation.
"""
from __future__ import annotations

import argparse
import collections
import faulthandler
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agora.equity import EQUITY_SYMBOLS  # noqa: E402
from agora.server import build_referee_from_env, make_handler  # noqa: E402
from agora.spatial import COMMODITIES, STATIONS  # noqa: E402

FLEETS = ["amos", "marvin", "zero", "aerial"]
TOKENS = {a: f"fz-tok-{a}" for a in FLEETS}
TOKENS["admin"] = "fz-tok-admin"
TOKENS["combine"] = "fz-tok-combine"
UPGRADE_KINDS = ["shielding", "hold", "armor", "engines"]

EXIT_CLEAN, EXIT_VIOLATION, EXIT_HARNESS = 0, 1, 2


# --------------------------------------------------------------------------- server

class CapturingServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that records unhandled handler exceptions instead
    of printing them to stderr: on a shared sqlite connection those are the
    most likely visible symptom of a race, and the client only sees a
    dropped connection."""
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.errors: List[Dict[str, Any]] = []
        self._err_lock = threading.Lock()

    def handle_error(self, request, client_address):
        exc_type, exc, _ = sys.exc_info()
        if exc_type in (BrokenPipeError, ConnectionResetError):
            return
        # Where it blew up: the innermost frame inside agora/, and the handler line.
        frames = traceback.extract_tb(sys.exc_info()[2])
        ag = [f for f in frames if "/agora/" in f.filename.replace("\\", "/")]
        site = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in (ag[-1:] + ag[:1] if len(ag) > 1 else ag))
        with self._err_lock:
            self.errors.append({"type": exc_type.__name__ if exc_type else "?", "msg": str(exc)[:300],
                                "site": site, "tb": traceback.format_exc(), "thread": threading.current_thread().name,
                                "t": time.monotonic()})


def http(base: str, method: str, path: str, token: Optional[str] = None, body: Any = None,
         raw: Optional[bytes] = None, timeout: float = 30.0) -> Tuple[int, Any]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:  # dropped connection = server thread died mid-request
        return -1, {"error": f"{type(e).__name__}: {e}"}
    try:
        return status, json.loads(txt)
    except ValueError:
        return status, {"_text": txt[:200]}


def summarize(resp: Any) -> str:
    if not isinstance(resp, dict):
        return str(resp)[:160]
    p = resp.get("payload") if isinstance(resp.get("payload"), dict) else {}
    bits = [str(resp.get(k)) for k in ("kind", "status", "reason", "error") if resp.get(k) not in (None, "ok")]
    for k in ("reason", "escrow_id", "transit_id", "loan_id", "order_id", "status"):
        if p.get(k) is not None:
            bits.append(f"{k}={p[k]}")
    if not bits and "detail" in resp:
        bits.append(str(resp["detail"]))
    return " ".join(bits)[:160] or "ok"


# --------------------------------------------------------------------------- sql trace

def tracing_connection(buf):
    """A sqlite3.Connection subclass that logs, in Python and before handing
    over to sqlite, every statement plus commit/rollback and `with conn`
    exits, tagged with the calling thread and whether that thread holds
    ref.lock is unknown here -- the thread name is what matters. Logging from
    Python rather than set_trace_callback matters: the C-level trace callback
    runs while sqlite holds the connection mutex and needs the GIL, which
    deadlocks as soon as two threads share the connection."""
    import sqlite3
    t0 = time.monotonic()

    def log(kind, sql=""):
        buf.append((round(time.monotonic() - t0, 5), threading.current_thread().name, kind,
                    " ".join(str(sql).split())[:220]))

    class TCursor(sqlite3.Cursor):
        def execute(self, sql, *a):
            log("exec", sql)
            return super().execute(sql, *a)

        def executemany(self, sql, *a):
            log("execmany", sql)
            return super().executemany(sql, *a)

    class TConn(sqlite3.Connection):
        def cursor(self, factory=TCursor):
            return super().cursor(factory)

        def execute(self, sql, *a):
            log("exec", sql)
            return super().execute(sql, *a)

        def executemany(self, sql, *a):
            log("execmany", sql)
            return super().executemany(sql, *a)

        def executescript(self, sql):
            log("script", sql[:80])
            return super().executescript(sql)

        def commit(self):
            log("COMMIT(explicit)", f"in_transaction={self.in_transaction}")
            return super().commit()

        def rollback(self):
            log("ROLLBACK(explicit)", f"in_transaction={self.in_transaction}")
            return super().rollback()

        def __exit__(self, et, ev, tb):
            log("ROLLBACK(with-exit)" if et else "COMMIT(with-exit)",
                f"in_transaction={self.in_transaction}" + (f" exc={et.__name__}: {ev}" if et else ""))
            return super().__exit__(et, ev, tb)
    return TConn


# --------------------------------------------------------------------------- checker

def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def snapshot_supply(ref) -> Dict[str, int]:
    with ref.lock:
        return {r[0]: r[1] for r in _q(ref.conn, "SELECT instrument, SUM(balance) FROM accounts GROUP BY instrument")}


def check_invariants(ref, supply0: Optional[Dict[str, int]]) -> List[str]:
    """Caller must NOT hold ref.lock. Only lock-free referee helpers are used
    in here: ref.lock is a plain Lock, so anything that re-takes it would
    deadlock the checker."""
    v: List[str] = []
    with ref.lock:
        conn = ref.conn
        ok, errs = ref.verify_ledger_invariants()
        v += [f"[builtin] {e}" for e in errs]

        for txn, inst, net in _q(conn, "SELECT txn_id, instrument, SUM(delta) FROM ledger_entries "
                                       "GROUP BY txn_id, instrument HAVING SUM(delta) != 0"):
            v.append(f"[txn-instrument] {txn} {inst} nets {net}")

        acc = {r[0]: r[1] for r in _q(conn, "SELECT instrument, SUM(balance) FROM accounts GROUP BY instrument")}
        led = {r[0]: r[1] for r in _q(conn, "SELECT instrument, SUM(delta) FROM ledger_entries GROUP BY instrument")}
        for inst in sorted(set(acc) | set(led)):
            if acc.get(inst, 0) != led.get(inst, 0):
                v.append(f"[supply] {inst}: SUM(accounts)={acc.get(inst, 0)} != SUM(ledger)={led.get(inst, 0)}")
            if supply0 is not None and acc.get(inst, 0) != supply0.get(inst, 0):
                v.append(f"[supply] {inst}: supply {acc.get(inst, 0)} != genesis supply {supply0.get(inst, 0)}")

        # Peer escrow: SYSTEM holds exactly what open offers and accepted-but-uncollected trades owe.
        held: Dict[str, int] = collections.Counter()
        for r in _q(conn, "SELECT instrument, SUM(delta) FROM ledger_entries WHERE agent_id='SYSTEM' "
                          "AND txn_id LIKE 'peer-%' GROUP BY instrument"):
            held[r[0]] += r[1]
        owed: Dict[str, int] = collections.Counter()
        for r in _q(conn, "SELECT status, instrument, qty, price FROM station_escrow WHERE status IN ('offered','accepted')"):
            owed[r[1]] += r[2]
            if r[0] == "accepted":
                owed["CR"] += r[2] * r[3]
        for inst in sorted(set(held) | set(owed)):
            if held.get(inst, 0) != owed.get(inst, 0):
                v.append(f"[escrow-peer] SYSTEM holds {held.get(inst, 0)} {inst} from peer txns, "
                         f"open escrow rows owe {owed.get(inst, 0)}")

        # Equity collateral: SYSTEM's net from collateral txns == active loans' collateral.
        col = _q(conn, "SELECT COALESCE(SUM(delta),0) FROM ledger_entries WHERE agent_id='SYSTEM' AND instrument='CR' "
                       "AND (txn_id LIKE 'col-%' OR txn_id LIKE 'ret-col-%' OR txn_id LIKE 'feecol-%' OR txn_id LIKE 'liq-%')")[0][0]
        active = _q(conn, "SELECT COALESCE(SUM(collateral_cr),0) FROM equity_loans WHERE status='active'")[0][0]
        if col != active:
            v.append(f"[escrow-equity] SYSTEM net collateral {col} CR != active loans' collateral {active} CR")
        for r in _q(conn, "SELECT loan_id, collateral_cr FROM equity_loans WHERE status!='active' AND collateral_cr!=0 AND status='liquidated'"):
            v.append(f"[escrow-equity] liquidated loan {r[0]} still carries collateral {r[1]}")

        # Contract bonds only on open, owned contracts; never negative.
        for r in _q(conn, "SELECT contract_id, status, owner, bond FROM station_contracts "
                          "WHERE bond < 0 OR (bond > 0 AND (status != 'open' OR owner IS NULL))"):
            v.append(f"[escrow-contract] {r[0]} status={r[1]} owner={r[2]} bond={r[3]}")

        # In-memory books vs orders table.
        resting = {}
        for st, books in ref.books.items():
            for inst, book in books.items():
                for o in list(book.bids) + list(book.asks):
                    resting[(o.agent_id, o.order_id)] = (st, inst, o)
        rows = {(r["agent_id"], r["order_id"]): r for r in
                _q(conn, "SELECT agent_id, order_id, station_id, instrument, side, qty, filled_qty, status FROM orders")}
        for key, (st, inst, o) in resting.items():
            r = rows.get(key)
            if r is None:
                v.append(f"[book-db] resting {key} at {st}/{inst} has no orders row")
            elif r["status"] != "open":
                v.append(f"[book-db] resting {key} at {st}/{inst} but orders row status={r['status']}")
            elif r["qty"] - r["filled_qty"] != o.remaining_qty:
                v.append(f"[book-db] {key} remaining in book {o.remaining_qty} != db {r['qty'] - r['filled_qty']}")
            elif r["station_id"] != st or r["instrument"] != inst:
                v.append(f"[book-db] {key} rests at {st}/{inst} but db says {r['station_id']}/{r['instrument']}")
        for key, r in rows.items():
            if r["status"] == "open" and r["qty"] - r["filled_qty"] > 0 and key not in resting:
                v.append(f"[book-db] orders row {key} open with {r['qty'] - r['filled_qty']} remaining, not resting in any book")

        # Vessels vs vessel_locations vs transits, per fleet.
        for (agent,) in _q(conn, "SELECT agent_id FROM fleet_roster"):
            vl = _q(conn, "SELECT station_id FROM vessel_locations WHERE agent_id=?", (agent,))
            vs = _q(conn, "SELECT station_id, status FROM vessels WHERE vessel_id=?", (f"{agent}/1",))
            n_tr = _q(conn, "SELECT COUNT(*) FROM transits WHERE agent_id=? AND status='in_transit'", (agent,))[0][0]
            if not vl or not vs:
                v.append(f"[vessels] {agent}: vessel_locations row={bool(vl)} vessels row={bool(vs)}")
                continue
            loc, vst, vstatus = vl[0][0], vs[0][0], vs[0][1]
            if n_tr > 1:
                v.append(f"[vessels] {agent}: {n_tr} simultaneous in_transit transits")
            if loc != vst:
                v.append(f"[vessels] {agent}: vessel_locations={loc} but vessels['{agent}/1']={vst}")
            if (n_tr > 0) != (loc == "in_transit"):
                v.append(f"[vessels] {agent}: vessel_locations={loc} but in_transit transits={n_tr}")
            if (vstatus == "in_transit") != (vst == "in_transit"):
                v.append(f"[vessels] {agent}: vessels status={vstatus} station={vst}")
    return v


def coverage(ref) -> Dict[str, Any]:
    with ref.lock:
        c = ref.conn
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "round": ref.current_round,
            "trades": one("SELECT COUNT(DISTINCT txn_id) FROM ledger_entries WHERE txn_id LIKE 'trade-%'"),
            "fleet_orders": one("SELECT COUNT(*) FROM orders WHERE agent_id IN ('amos','marvin','zero','aerial')"),
            "fleet_cancelled": one("SELECT COUNT(*) FROM orders WHERE status='cancelled' AND agent_id IN ('amos','marvin','zero','aerial')"),
            "transits": dict(c.execute("SELECT status, COUNT(*) FROM transits GROUP BY status").fetchall()),
            "peer_escrow": dict(c.execute("SELECT status, COUNT(*) FROM station_escrow GROUP BY status").fetchall()),
            "contracts_owned": one("SELECT COUNT(*) FROM station_contracts WHERE owner IS NOT NULL"),
            "contracts": dict(c.execute("SELECT status, COUNT(*) FROM station_contracts GROUP BY status").fetchall()),
            "loans": dict(c.execute("SELECT status, COUNT(*) FROM equity_loans GROUP BY status").fetchall()),
            "upgrades": one("SELECT COUNT(*) FROM fleet_upgrades"),
            "raids": one("SELECT COUNT(*) FROM piracy_raids"),
            "privateers": one("SELECT COUNT(*) FROM piracy_privateers"),
            "beacons": one("SELECT COUNT(*) FROM distress_beacons"),
            "halts": one("SELECT COUNT(*) FROM circuit_breaker_halts"),
            "ledger_rows": one("SELECT COUNT(*) FROM ledger_entries"),
        }


# --------------------------------------------------------------------------- fuzzer

class Fuzzer:
    def __init__(self, args):
        self.a = args
        self.stop = threading.Event()
        self.done = 0
        self.done_lock = threading.Lock()
        self.logs: Dict[str, collections.deque] = {}
        self.full_log: List[Dict[str, Any]] = [] if args.log_jsonl else None
        self.hist: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.hist_lock = threading.Lock()
        self.violations: List[str] = []
        self.violation_at: Optional[int] = None
        self.checks_run = 0
        self.next_check = args.check_every
        self.check_lock = threading.Lock()
        self.stall: Optional[str] = None
        self.errors_at_last_clean = 0
        self.skip = {x.strip() for x in (args.skip_checks or "").split(",") if x.strip()}
        w = dict(self.OPS)
        for kv in filter(None, (args.weights or "").split(",")):
            k, _, val = kv.partition("=")
            if k.strip() not in w:
                raise SystemExit(f"--weights: unknown op '{k}'. Known: {', '.join(w)}")
            w[k.strip()] = float(val)
        self.ops_w = [(k, x) for k, x in w.items() if x > 0]
        self.total_w = sum(x for _, x in self.ops_w)
        self.window_errors: List[Dict[str, Any]] = []
        self.injected = False

    # ------------------------------------------------------------ setup
    def start(self):
        a = self.a
        self.tmpdir = a.db_dir or tempfile.mkdtemp(prefix="race_fuzz_")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.db_path = os.path.join(self.tmpdir, f"agora-seed{a.seed}.db")
        for suffix in ("", "-journal", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.remove(self.db_path + suffix)
        self.sql_trace = collections.deque(maxlen=a.trace_sql) if a.trace_sql else None
        if self.sql_trace is not None:
            # Build the referee on a tracing Connection subclass, so every
            # engine that captured ref.conn at construction is traced too.
            import agora.referee as _refmod
            _real = _refmod.sqlite3.connect
            _refmod.sqlite3.connect = lambda *x, **k: _real(*x, factory=tracing_connection(self.sql_trace), **k)
            try:
                self.ref = build_referee_from_env(self.db_path)
            finally:
                _refmod.sqlite3.connect = _real
        else:
            self.ref = build_referee_from_env(self.db_path)
        if not a.fsync:
            # Commits still happen exactly as in production; this only skips
            # the fsync, which otherwise dominates run time on slow disks.
            self.ref.conn.execute("PRAGMA synchronous=OFF")
        self.ref.new_game(seed=a.seed, warmup_rounds=a.warmup_rounds)
        self.supply0 = snapshot_supply(self.ref)
        self.server = CapturingServer(("127.0.0.1", 0), make_handler(self.ref, auth_tokens=dict(TOKENS)))
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, name="http-server", daemon=True).start()
        r = self.ref
        self.features = {
            "depots": r.depots_enabled, "asymmetric": r.asymmetric_enabled, "peer_trades": r.peer_trades,
            "contracts": r.contracts_enabled, "corporate": r.corporate_enabled, "upgrades": r.upgrades_enabled,
            "events": r.events_enabled, "piracy": bool(r.piracy.enabled), "hazards": r.hazards.odds,
            "idle_fee": r.idle_fee, "exchange_shares": r.exchange_shares, "rival_shares": r.rival_shares,
            "fog": bool(r.fog), "order_flow": bool(r.order_flow.enabled),
        }
        env = {k: v for k, v in os.environ.items() if k.startswith("AGORA_")}
        self.say(f"race_fuzz: seed={a.seed} threads={a.threads} ops={a.ops} rounds={a.rounds} "
                 f"serial={a.serial} db={self.db_path}")
        self.say(f"features: {json.dumps(self.features, default=str)}" + (f"  AGORA_* env: {env}" if env else ""))
        pre = check_invariants(self.ref, self.supply0)
        if pre:
            self.say("INVARIANTS ALREADY BROKEN AT GENESIS (logic bug, not a race):")
            for x in pre:
                self.say("  " + x)
            self.violations = ["[genesis] " + x for x in pre]
            self.violation_at = 0

    def say(self, s: str):
        if not self.a.quiet or s.startswith(("VIOL", "INVAR", "STALL")):
            print(s, flush=True)

    # ------------------------------------------------------------ plumbing
    def record(self, wname: str, i: int, who: str, method: str, path: str, body: Any, status: int, resp: Any):
        ent = {"w": wname, "i": i, "t": round(time.monotonic() - self.t0, 4), "round": self.ref.current_round,
               "as": who, "m": method, "path": path, "body": body, "status": status, "resp": summarize(resp)}
        self.logs[wname].append(ent)
        if self.full_log is not None:
            self.full_log.append(ent)
        ep = path.split("?")[0]
        parts = ep.strip("/").split("/")
        if len(parts) == 4 and parts[1] in ("contracts", "piracy"):
            ep = f"/{parts[0]}/{parts[1]}/{{id}}/{parts[3]}"
        with self.hist_lock:
            self.hist[f"{method} {ep}"][status] += 1

    def call(self, w, method, path, who=None, body=None, raw=None, token=None):
        tok = token if token is not None else (TOKENS.get(who) if who else None)
        status, resp = http(self.base, method, path, tok, body, raw)
        w["i"] += 1
        self.record(w["name"], w["i"], who or "-", method, path, body if raw is None else raw.decode("utf-8", "replace"),
                    status, resp)
        self.tick(w)
        return status, resp

    def tick(self, w):
        with self.done_lock:
            self.done += 1
            n = self.done
        if self.a.inject_corruption and n >= self.a.inject_corruption and not self.injected:
            self.injected = True
            with self.ref.lock, self.ref.conn:
                self.ref.conn.execute("UPDATE accounts SET balance = balance + 1 WHERE agent_id='amos' AND instrument='CR'")
            self.say(f"(injected test corruption at op {n}: amos CR +1 without a ledger row)")
        if self.a.serial:
            if w.get("step_every") and n % w["step_every"] == 0 and w["rounds_done"] < self.a.rounds:
                w["rounds_done"] += 1
                self.call(w, "POST", "/stations/step_round", body={})
            if n >= self.next_check:
                self.next_check += self.a.check_every
                self.run_check(n)
        if n >= self.a.ops:
            self.stop.set()

    def run_check(self, n: int):
        with self.check_lock:
            if self.violations:
                return
            vs = check_invariants(self.ref, self.supply0)
            if self.skip:
                vs = [x for x in vs if x[1:].split("]")[0] not in self.skip]
            self.checks_run += 1
            if vs:
                self.violations = vs
                self.violation_at = n
                self.window_errors = self.server.errors[self.errors_at_last_clean:]
                self.stop.set()
            else:
                self.errors_at_last_clean = len(self.server.errors)

    # ------------------------------------------------------------ op generators
    def pick_who(self, rng, home):
        x = rng.random()
        if x < 0.78:
            return home
        if x < 0.95:
            return rng.choice(FLEETS)
        return rng.choice(["admin", "combine"])

    def order_price(self, rng, st, inst):
        if inst in EQUITY_SYMBOLS:
            return max(1, int(rng.gauss(20, 6)))
        try:
            spot = self.ref.spatial.get_station_price(st, inst)
        except Exception:
            spot = 10
        return max(1, int(round(spot * rng.uniform(0.75, 1.3))))

    def op(self, w, rng):
        home, who = w["home"], self.pick_who(rng, w["home"])
        agent = home if who in ("admin", "combine") else who
        loc = w["loc"].get(agent) or "ceres"
        mine = w["orders"].setdefault(agent, collections.deque(maxlen=40))
        ops = self.ops_w
        r = rng.random() * self.total_w
        for name, weight in ops:
            r -= weight
            if r <= 0:
                break
        getattr(self, "op_" + name)(w, rng, who, agent, loc, mine)

    OPS = [
        ("order", 22), ("eq_order", 8), ("quick_order", 3), ("dup_order", 3), ("cancel", 6), ("cancel_all", 3),
        ("transit", 6), ("peer_offer", 5), ("peer_accept", 5), ("peer_cancel", 3), ("contract", 6),
        ("covert", 3), ("upgrade", 2), ("piracy", 3), ("equity", 5), ("salvage", 3), ("breaker", 1),
        ("galnet", 2), ("admin_fleets", 2), ("reads", 12), ("junk", 5), ("locate", 4),
    ]

    def _order_body(self, rng, who, agent, st, inst, mine, oid=None):
        side = rng.choice(["bid", "ask"])
        oid = oid or f"{agent}-w{rng.randrange(10**9):09d}"
        qty = rng.choice([1, 2, 5, 10, 20, 50, rng.randint(1, 400)])
        body = {"v": 1, "kind": "order", "payload": {
            "order_id": oid, "agent_id": agent, "instrument": inst, "side": side, "qty": qty,
            "limit_price": self.order_price(rng, st, inst), "station_id": st, "seq_seen": 0}}
        mine.append(oid)
        return body

    def op_order(self, w, rng, who, agent, loc, mine):
        st = loc if rng.random() < 0.85 else rng.choice(STATIONS)
        inst = rng.choice(COMMODITIES)
        self.call(w, "POST", "/referee/orders", who, self._order_body(rng, who, agent, st, inst, mine))

    def op_eq_order(self, w, rng, who, agent, loc, mine):
        self.call(w, "POST", "/referee/orders", who,
                  self._order_body(rng, who, agent, rng.choice(STATIONS), rng.choice(EQUITY_SYMBOLS), mine))

    def op_quick_order(self, w, rng, who, agent, loc, mine):
        inst = rng.choice(COMMODITIES + EQUITY_SYMBOLS)
        oid = f"{agent}-q{rng.randrange(10**9):09d}"
        mine.append(oid)
        self.call(w, "POST", "/referee/quick_order", who, {
            "agent_id": agent, "side": rng.choice(["buy", "sell"]), "qty": rng.randint(1, 30),
            "price": self.order_price(rng, loc, inst), "instrument": inst, "station_id": loc, "order_id": oid})

    def op_dup_order(self, w, rng, who, agent, loc, mine):
        if not mine:
            return self.op_order(w, rng, who, agent, loc, mine)
        oid = rng.choice(list(mine))
        body = self._order_body(rng, who, agent, loc, rng.choice(COMMODITIES), mine, oid=oid)
        self.call(w, "POST", "/referee/orders", who, body)
        if rng.random() < 0.5:  # immediately resend identically: idempotency under contention
            self.call(w, "POST", "/referee/orders", who, body)

    def op_cancel(self, w, rng, who, agent, loc, mine):
        oid = rng.choice(list(mine)) if mine and rng.random() < 0.9 else f"nope-{rng.randrange(10**6)}"
        body = {"order_id": oid}
        if who in ("admin", "combine") or rng.random() < 0.1:
            body["agent_id"] = agent
        self.call(w, "POST", "/referee/orders/cancel", who, body)

    def op_cancel_all(self, w, rng, who, agent, loc, mine):
        self.call(w, "POST", "/referee/orders/cancel_all", who, {"agent_id": agent} if who == "admin" else {})

    def op_transit(self, w, rng, who, agent, loc, mine):
        body = {"destination": rng.choice(STATIONS + (["pluto"] if rng.random() < 0.05 else [])),
                "commodity": rng.choice(COMMODITIES), "cargo_qty": rng.choice([0, 0, 5, 20, 50, 100, 300]),
                "escort": rng.random() < 0.3}
        if who in ("admin", "combine"):
            body["agent_id"] = agent
        st, resp = self.call(w, "POST", "/stations/transit", who, body)
        if st == 200 and isinstance(resp, dict):
            p = resp.get("payload") or {}
            w["loc"][agent] = p.get("destination")  # optimistic: where it will dock
            if p.get("transit_id"):
                w["transits"].append((agent, p["transit_id"]))

    def op_locate(self, w, rng, who, agent, loc, mine):
        target = agent if rng.random() < 0.85 else f"ghost{rng.randrange(10**6)}"
        st, resp = self.call(w, "GET", f"/stations/locations?agent_id={target}", None)
        if st == 200 and target == agent:
            l = (resp.get("location") or {})
            if l.get("status") == "docked":
                w["loc"][agent] = l.get("station_id")
            elif l.get("transit"):
                w["loc"][agent] = l["transit"].get("destination")

    def op_peer_offer(self, w, rng, who, agent, loc, mine):
        body = {"station_id": loc if rng.random() < 0.9 else rng.choice(STATIONS),
                "instrument": rng.choice(COMMODITIES), "qty": rng.choice([1, 5, 10, 50, 0, -3]),
                "price": rng.choice([1, 5, 10, 20, 40])}
        if who in ("admin", "combine"):
            body["agent_id"] = agent
        st, resp = self.call(w, "POST", "/referee/peer/offer", who, body)
        if st == 200 and isinstance(resp, dict):
            eid = (resp.get("payload") or {}).get("escrow_id")
            if eid:
                w["escrows"].append(eid)

    def _fresh_offers(self, w, rng):
        st, resp = self.call(w, "GET", "/referee/peer/offers", None)
        if st == 200 and isinstance(resp, dict):
            for o in resp.get("offers") or []:
                w["escrows"].append(o.get("escrow_id"))

    def op_peer_accept(self, w, rng, who, agent, loc, mine):
        if not w["escrows"] or rng.random() < 0.3:
            self._fresh_offers(w, rng)
        eid = rng.choice(list(w["escrows"])) if w["escrows"] else "x0"
        body = {"escrow_id": eid}
        if who in ("admin", "combine"):
            body["agent_id"] = agent
        self.call(w, "POST", "/referee/peer/accept", who, body)

    def op_peer_cancel(self, w, rng, who, agent, loc, mine):
        eid = rng.choice(list(w["escrows"])) if w["escrows"] else "x0"
        body = {"escrow_id": eid}
        if who in ("admin", "combine"):
            body["agent_id"] = agent
        self.call(w, "POST", "/referee/peer/cancel", who, body)

    def op_contract(self, w, rng, who, agent, loc, mine):
        if not w["contracts"] or rng.random() < 0.3:
            st, resp = self.call(w, "GET", "/referee/contracts?status=open", None)
            if st == 200 and isinstance(resp, dict):
                w["contracts"].extend(c.get("contract_id") for c in resp.get("contracts") or [])
        cid = rng.choice(list(w["contracts"])) if w["contracts"] else "c-none"
        action = rng.choice(["claim", "claim", "list", "buy", "deliver", "deliver"])
        body: Dict[str, Any] = {}
        if action == "list":
            body["price"] = rng.choice([0, 1, 50, 500])
        if action == "deliver" and rng.random() < 0.5:
            body["qty"] = rng.choice([1, 10, 100])
        if who in ("admin", "combine"):
            body["agent_id"] = agent
        self.call(w, "POST", f"/referee/contracts/{cid}/{action}", who, body)

    def op_covert(self, w, rng, who, agent, loc, mine):
        target = rng.choice(FLEETS + ["nobody"])
        if rng.random() < 0.5:
            body = {"target": target}
            path = "/referee/covert/wiretap"
        else:
            body = {"target": target, "mode": rng.choice(["auto", "cargo", "clamps", "fuel", "bogus"])}
            path = "/referee/covert/sabotage"
        if who in ("admin", "combine"):
            body["agent_id"] = agent
        self.call(w, "POST", path, who, body)

    def op_upgrade(self, w, rng, who, agent, loc, mine):
        body = {"kind": rng.choice(UPGRADE_KINDS + ["warp"])}
        if who in ("admin", "combine"):
            body["agent_id"] = agent
        self.call(w, "POST", "/referee/upgrades/buy", who, body)

    def op_piracy(self, w, rng, who, agent, loc, mine):
        if rng.random() < 0.25:
            body = {"target": rng.choice(FLEETS)}
            if who in ("admin", "combine"):
                body["agent_id"] = agent
            return self.call(w, "POST", "/referee/privateers", who, body)
        own = [t for a, t in w["transits"] if a == agent]
        tid = rng.choice(own) if own else "t-none"
        body = {"choice": rng.choice(["pay", "surrender", "fight", "flee"])}
        if who in ("admin", "combine"):
            body["agent_id"] = agent
        self.call(w, "POST", f"/referee/piracy/{tid}/respond", who, body)

    def op_equity(self, w, rng, who, agent, loc, mine):
        if rng.random() < 0.65:
            sym = rng.choice(EQUITY_SYMBOLS)
            body = {"equity_symbol": sym, "shares": rng.choice([1, 5, 10, 25, 0])}
            if rng.random() < 0.3:
                body["collateral_cr"] = rng.choice([10, 500, 5000])
            if who == "admin":
                body["borrower_id"] = agent
            # Burst: same borrower+symbol back to back, to land in one millisecond (loan_id collides).
            for _ in range(1 if rng.random() < 0.7 else 3):
                st, resp = self.call(w, "POST", "/equity/borrow", who, body)
                if st == 200 and isinstance(resp, dict) and resp.get("loan_id"):
                    w["loans"].append((agent, resp["loan_id"]))
        else:
            if not w["loans"] or rng.random() < 0.3:
                st, resp = self.call(w, "GET", f"/equity/loans?borrower_id={agent}", None)
                if st == 200 and isinstance(resp, dict):
                    w["loans"].extend((agent, l.get("loan_id")) for l in resp.get("loans") or [])
            own = [l for a, l in w["loans"] if a == agent]
            body = {"loan_id": rng.choice(own) if own else "loan-none"}
            if who == "admin":
                body["borrower_id"] = agent
            self.call(w, "POST", "/equity/return", who, body)

    def op_salvage(self, w, rng, who, agent, loc, mine):
        x = rng.random()
        if x < 0.3:
            body = {"location": loc, "fuel_needed": rng.choice([5, 15, 40]), "max_reward_cr": rng.choice([0, 100, 1000])}
            own = [t for a, t in w["transits"] if a == agent]
            if own and rng.random() < 0.5:
                body["transit_id"] = own[-1]
            self.call(w, "POST", "/salvage/distress", who, body)
        elif x < 0.55:
            st, resp = self.call(w, "GET", "/salvage/rfqs?status=open", None)
            rfqs = (resp.get("rfqs") or []) if st == 200 and isinstance(resp, dict) else []
            if rfqs:
                rfq = rng.choice(rfqs)
                st, resp = self.call(w, "POST", "/salvage/quote", who, {
                    "rfq_id": rfq.get("rfq_id"), "fuel_offered": rng.choice([5, 15, 50]), "price_cr": rng.choice([0, 50, 500])})
                qid = (resp or {}).get("quote_id") or ((resp or {}).get("quote") or {}).get("quote_id") if isinstance(resp, dict) else None
                if qid:
                    w["quotes"].append(qid)
        elif x < 0.8:
            qid = rng.choice(list(w["quotes"])) if w["quotes"] else "q-none"
            self.call(w, "POST", "/salvage/accept_quote", who, {"quote_id": qid})
        else:
            st, resp = self.call(w, "GET", "/salvage/beacons?status=active", None)
            beacons = (resp.get("beacons") or []) if st == 200 and isinstance(resp, dict) else []
            bid = rng.choice(beacons).get("beacon_id") if beacons else "b-none"
            self.call(w, "POST", "/salvage/claim", who, {"beacon_id": bid})

    def op_breaker(self, w, rng, who, agent, loc, mine):
        body = {"station_id": rng.choice(STATIONS), "instrument": rng.choice(COMMODITIES)}
        if rng.random() < 0.5:
            body["trigger_price"] = rng.choice([1.0, 9999.0])
            self.call(w, "POST", "/circuit_breaker/halt", who, body)
        else:
            self.call(w, "POST", "/circuit_breaker/reopen", who, body)

    def op_galnet(self, w, rng, who, agent, loc, mine):
        if rng.random() < 0.6:
            self.call(w, "POST", "/galnet/step", None, {})
        else:
            self.call(w, "POST", "/galnet/shock", None, {"template_idx": rng.choice([None, 0, 1, 2])})

    def op_admin_fleets(self, w, rng, who, agent, loc, mine):
        # Unlocked execute + commit on the shared connection: upsert a
        # roster row with its current values, so the game itself is unchanged.
        row = self.roster.get(agent)
        if row:
            self.call(w, "POST", "/referee/admin/fleets", "admin", dict(row))

    READS = ["/referee/health", "/referee/book", "/referee/book?station_id={loc}&instrument=FRAG", "/referee/accounts",
             "/referee/leaderboard", "/referee/briefing", "/referee/vessels", "/referee/vessels?agent_id={agent}",
             "/referee/fleets", "/referee/peer/offers?status=accepted", "/referee/contracts?status=lapsed",
             "/referee/piracy", "/referee/corporate", "/referee/corporate/events", "/referee/upgrades",
             "/referee/order-flow", "/referee/ticks?since_seq=0", "/referee/depots", "/referee/covert/wiretaps",
             "/referee/covert/intel?target={other}", "/equity/summary", "/salvage/summary",
             "/circuit_breaker/halts", "/circuit_breaker/bands", "/stations/prices?station_id={loc}",
             "/stations/locations", "/stations/routes?origin={loc}&destination=mars", "/galnet/feed"]

    def op_reads(self, w, rng, who, agent, loc, mine):
        path = rng.choice(self.READS).format(loc=loc, agent=agent, other=rng.choice(FLEETS))
        tok_who = rng.choice([who, agent, None, "admin"])
        self.call(w, "GET", path, tok_who)

    def op_junk(self, w, rng, who, agent, loc, mine):
        k = rng.randrange(7)
        if k == 0:
            self.call(w, "POST", "/referee/orders", who, raw=b'{"v":1,"kind":"order","payload":{')
        elif k == 1:
            self.call(w, "POST", "/referee/orders", None, token="not-a-token",
                      body={"kind": "order", "payload": {"agent_id": agent}})
        elif k == 2:  # impersonation
            other = rng.choice([f for f in FLEETS if f != agent])
            self.call(w, "POST", "/referee/orders", agent, self._order_body(rng, agent, other, loc, "FRAG", mine))
        elif k == 3:  # wrong types
            b = self._order_body(rng, who, agent, loc, "FRAG", mine)
            b["payload"]["qty"] = rng.choice([-5, 0, "10", 3.5, None])
            b["payload"]["limit_price"] = rng.choice([-1, 0, "x", 1e12])
            self.call(w, "POST", "/referee/orders", who, b)
        elif k == 4:
            self.call(w, "POST", "/stations/transit", who, {"destination": None, "cargo_qty": "lots"})
        elif k == 5:
            self.call(w, "POST", "/referee/peer/offer", who, {"station_id": loc, "instrument": "FRAG", "qty": "ten", "price": []})
        else:
            self.call(w, "POST", f"/referee/contracts/{rng.randrange(99)}/deliver", who, {"qty": "all"})

    # ------------------------------------------------------------ threads
    def worker(self, tid: int):
        rng = random.Random(f"{self.a.seed}:{tid}")
        name = f"w{tid}"
        w = {"name": name, "i": 0, "home": FLEETS[tid % len(FLEETS)], "loc": {}, "orders": {},
             "escrows": collections.deque(maxlen=60), "contracts": collections.deque(maxlen=60),
             "transits": collections.deque(maxlen=40), "loans": collections.deque(maxlen=40),
             "quotes": collections.deque(maxlen=40), "rounds_done": 0,
             "step_every": max(1, self.a.ops // max(1, self.a.rounds)) if self.a.serial else 0}
        self.logs[name] = collections.deque(maxlen=self.a.last_k)
        while not self.stop.is_set():
            try:
                self.op(w, rng)
            except Exception:
                self.harness_errors.append(f"{name}: {traceback.format_exc()}")
                if len(self.harness_errors) > 20:
                    self.stop.set()

    def stepper(self):
        """Steps rounds concurrently, spread over the op budget."""
        rng = random.Random(f"{self.a.seed}:stepper")
        w = {"name": "stepper", "i": 0}
        self.logs["stepper"] = collections.deque(maxlen=self.a.last_k)
        per_round = max(1, self.a.ops // max(1, self.a.rounds))
        stepped = 0
        while not self.stop.is_set() and stepped < self.a.rounds:
            if self.done >= (stepped + 1) * per_round - rng.randrange(max(1, per_round // 3)):
                stepped += 1
                self.call(w, "POST", "/stations/step_round", None, {})
            else:
                time.sleep(0.002)
        self.rounds_stepped = stepped

    def checker(self):
        while not self.stop.is_set():
            if self.done >= self.next_check:
                self.next_check += self.a.check_every
                self.run_check(self.done)
            time.sleep(0.005)

    def run(self) -> int:
        a = self.a
        self.harness_errors: List[str] = []
        self.rounds_stepped = 0
        with self.ref.lock:
            self.roster = {r["agent_id"]: {k: r[k] for k in ("agent_id", "display_name", "home_station", "genesis_cr",
                                                             "genesis_frag", "genesis_fuel")}
                           for r in self.ref.conn.execute("SELECT * FROM fleet_roster")}
        self.t0 = time.monotonic()
        threads = []
        if not self.violations:
            nworkers = 1 if a.serial else a.threads
            for tid in range(nworkers):
                threads.append(threading.Thread(target=self.worker, args=(tid,), name=f"w{tid}", daemon=True))
            if not a.serial:
                threads.append(threading.Thread(target=self.stepper, name="stepper", daemon=True))
                threads.append(threading.Thread(target=self.checker, name="checker", daemon=True))
            for t in threads:
                t.start()
            last, last_t = -1, time.monotonic()
            # Backstop for a hang that holds the GIL (the Python watchdog below
            # could not run): faulthandler's C thread dumps every stack and exits.
            faulthandler.dump_traceback_later(a.stall_timeout * 2, exit=True)
            deadline = time.monotonic() + a.duration if a.duration else None
            while any(t.is_alive() for t in threads if t.name.startswith("w")):
                time.sleep(0.05)
                now = time.monotonic()
                if self.done != last:
                    last, last_t = self.done, now
                    faulthandler.dump_traceback_later(a.stall_timeout * 2, exit=True)
                elif now - last_t > a.stall_timeout:
                    self.stall = self.dump_stacks()
                    self.stop.set()
                    break
                if deadline and now > deadline:
                    self.stop.set()
            self.stop.set()
            for t in threads:
                t.join(timeout=a.stall_timeout)
            faulthandler.cancel_dump_traceback_later()
        elapsed = time.monotonic() - self.t0
        if not self.violations and not self.stall:
            self.run_check(self.done)  # final, quiescent
        # Committed state as a fresh connection sees it, not the shared one.
        if not self.violations and not self.stall:
            self.final_fresh_conn_check()
        return self.report(elapsed)

    def final_fresh_conn_check(self):
        import sqlite3
        with self.ref.lock:
            self.ref.conn.commit()
            c = sqlite3.connect(self.db_path)
            try:
                a = c.execute("SELECT agent_id, instrument, balance FROM accounts ORDER BY 1,2").fetchall()
                b = [tuple(r) for r in self.ref.conn.execute("SELECT agent_id, instrument, balance FROM accounts ORDER BY 1,2")]
                if a != b:
                    self.violations = [f"[durable] accounts on disk differ from the live connection "
                                       f"({len(set(a) ^ set(b))} rows)"]
            finally:
                c.close()

    def dump_stacks(self) -> str:
        frames = sys._current_frames()
        out = []
        for t in threading.enumerate():
            f = frames.get(t.ident)
            if f is not None:
                out.append(f"--- {t.name}\n" + "".join(traceback.format_stack(f)))
        return "\n".join(out)

    # ------------------------------------------------------------ report
    def report(self, elapsed: float) -> int:
        a = self.a
        cov = coverage(self.ref) if not self.stall else {}
        self.say(f"\nops={self.done} elapsed={elapsed:.1f}s ({self.done / max(elapsed, 1e-9):.0f} ops/s) "
                 f"rounds_stepped={self.rounds_stepped if not a.serial else '(inline)'} checks={self.checks_run}")
        if cov:
            self.say("coverage: " + json.dumps(cov))
            if cov.get("trades", 0) == 0 or not cov.get("transits"):
                self.say("WARNING: run never produced trades or transits -- it proves little")
        if a.verbose_hist or self.violations or self.stall:
            self.say("status histogram:")
            for ep in sorted(self.hist):
                self.say(f"  {ep:48s} {dict(sorted(self.hist[ep].items()))}")
        errs = self.server.errors
        if errs:
            sig = collections.Counter(f"{e['type']}: {e['msg'][:90]}  @ {e['site']}" for e in errs)
            self.say(f"server exceptions (unhandled in request threads): {len(errs)}")
            for s, n in sig.most_common():
                self.say(f"  {n:5d} x {s}")
            if a.show_tracebacks:
                seen = set()
                for e in errs:
                    k = (e["type"], e["msg"][:90], e["site"])
                    if k not in seen:
                        seen.add(k)
                        self.say(e["tb"])
        if self.harness_errors:
            self.say(f"HARNESS ERRORS: {len(self.harness_errors)}")
            self.say(self.harness_errors[0])
        if a.log_jsonl and self.full_log is not None:
            with open(a.log_jsonl, "w") as f:
                for e in self.full_log:
                    f.write(json.dumps(e, default=str) + "\n")
            self.say(f"full op log: {a.log_jsonl}")

        failed = bool(self.violations or self.stall)
        if failed:
            self.say("")
            if self.stall:
                self.say(f"STALL: no op completed for {a.stall_timeout}s. Thread stacks:\n{self.stall}")
            if self.violations:
                self.say(f"VIOLATION at op {self.violation_at} (seed={a.seed}):")
                for x in self.violations[:50]:
                    self.say("  " + x)
                if len(self.violations) > 50:
                    self.say(f"  ... {len(self.violations) - 50} more")
                if self.window_errors:
                    self.say(f"server exceptions since the last clean check ({len(self.window_errors)}), "
                             f"the likely cause -- full tracebacks:")
                    for e in self.window_errors:
                        self.say(f"  [{e['thread']}] {e['type']}: {e['msg'][:200]}  @ {e['site']}")
                        self.say("    " + e["tb"].strip().replace("\n", "\n    "))
            if self.sql_trace is not None:
                tp = os.path.join(self.tmpdir, "sql-trace.txt")
                with open(tp, "w") as f:
                    for t, th, kind, stmt in list(self.sql_trace):
                        f.write(f"{t:10.5f} {th:40s} {kind:20s} {stmt}\n")
                self.say(f"sql trace (last {len(self.sql_trace)} statements, all threads): {tp}")
            self.say(f"\nreplay: {self.replay_cmd()}")
            self.say(f"db kept at: {self.db_path}")
            self.say(f"last {a.last_k} ops per thread:")
            for name in sorted(self.logs):
                self.say(f"-- {name}")
                for e in list(self.logs[name]):
                    self.say(f"   #{e['i']:<5d} t={e['t']:<8} r={e['round']:<3} {e['as']:8s} {e['m']:4s} {e['path']} "
                             f"{json.dumps(e['body'], default=str)[:220] if e['body'] is not None else ''} "
                             f"-> {e['status']} {e['resp']}")
        else:
            self.say(f"CLEAN: seed={a.seed} ops={self.done} -- all invariants held")
            if not a.keep_db and not a.db_dir:
                shutil.rmtree(self.tmpdir, ignore_errors=True)
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        if failed:
            return EXIT_VIOLATION
        if self.harness_errors:
            return EXIT_HARNESS
        if a.fail_on_server_error and errs:
            return EXIT_VIOLATION
        return EXIT_CLEAN

    def replay_cmd(self) -> str:
        a = self.a
        parts = [f"python tools/race_fuzz.py --seed {a.seed}", f"--ops {a.ops}", f"--rounds {a.rounds}",
                 f"--warmup-rounds {a.warmup_rounds}", f"--check-every {a.check_every}"]
        parts.append("--serial" if a.serial else f"--threads {a.threads}")
        if a.weights:
            parts.append(f"--weights {a.weights}")
        if a.skip_checks:
            parts.append(f"--skip-checks {a.skip_checks}")
        if a.inject_corruption:
            parts.append(f"--inject-corruption {a.inject_corruption}")
        env = " ".join(f"{k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("AGORA_"))
        return (env + " " if env else "") + " ".join(parts)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--ops", type=int, default=2000, help="total ops across all threads")
    p.add_argument("--duration", type=float, default=0, help="optional wall-clock cap in seconds")
    p.add_argument("--rounds", type=int, default=30, help="rounds stepped over the run")
    p.add_argument("--warmup-rounds", type=int, default=3)
    p.add_argument("--check-every", type=int, default=250, help="run the invariant checker every N completed ops")
    p.add_argument("--last-k", type=int, default=15, help="ops per thread printed on a violation")
    p.add_argument("--weights", help="override op weights, e.g. admin_fleets=20,locate=10,junk=0 "
                                    "(ops: " + ", ".join(k for k, _ in Fuzzer.OPS) + ")")
    p.add_argument("--skip-checks", help="comma-separated checker tags to ignore, e.g. vessels,book-db -- to hunt "
                                        "past a known finding (tags: builtin, txn-instrument, supply, escrow-peer, "
                                        "escrow-equity, escrow-contract, book-db, vessels, durable)")
    p.add_argument("--serial", action="store_true", help="one worker, rounds stepped inline: no concurrency (calibration)")
    p.add_argument("--stall-timeout", type=float, default=45.0)
    p.add_argument("--log-jsonl", help="write every op to this JSONL file")
    p.add_argument("--db-dir", help="put the DB here and keep it (default: a temp dir, removed on a clean run)")
    p.add_argument("--keep-db", action="store_true")
    p.add_argument("--fsync", action="store_true", help="keep sqlite synchronous=FULL (default: OFF, for speed)")
    p.add_argument("--inject-corruption", type=int, default=0, metavar="N",
                   help="(self-test) after N ops, bump amos CR by 1 with no ledger row; the checker must catch it")
    p.add_argument("--trace-sql", type=int, default=0, metavar="N",
                   help="keep the last N SQL statements run on the shared connection, with the thread that ran "
                        "each, and write them next to the DB on a violation (diagnosis; slows the run)")
    p.add_argument("--fail-on-server-error", action="store_true",
                   help="exit 1 if any request thread raised an unhandled exception")
    p.add_argument("--show-tracebacks", action="store_true")
    p.add_argument("--verbose-hist", action="store_true", help="always print the per-endpoint status histogram")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)
    try:
        fz = Fuzzer(a)
        fz.start()
    except Exception:
        traceback.print_exc()
        return EXIT_HARNESS
    return fz.run()


if __name__ == "__main__":
    sys.exit(main())
