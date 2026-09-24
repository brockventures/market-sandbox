"""
agora/bag.py - marble-bag draws for yes/no outcome rolls (#214).

Ryan, 2026-09-23: players should not be able to hit long streaks of bad (or
good) luck. An independent `rng.random() < p` roll has no memory, so a fleet
can be breached three trips running, or go 80 trips without one. Every roll
that *happens to a player* (hazards, raids, traces, escapes, leaks) now draws
from a Bags desk instead. Content generation (contracts, news, order flow,
prices) keeps its plain RNG.

Two draw kinds, one state row per (desk, event type, fleet):

Marble bag, for a fixed p (`draw`)
    p is written as k hits in a bag of n marbles: k/n is the fraction with the
    smallest n <= MAX_N that equals p up to float noise (EPS). So 0.10 is 1 hit
    in 10, 0.14 is 7 in 50, 0.25 is 1 in 4, 0.045 (0.10 x a 0.45 upgrade) is 9
    in 200. The bag is filled as k runs of marbles, each holding one hit at
    a random place, run lengths n/k rounded up or down and shuffled; it is
    drawn without replacement and refilled when empty, so over each full bag
    the rate is exactly p. Laying it out in runs keeps a big bag from bunching
    its hits: streak bounds, across the join of two bags, are at most 2 hits
    in a row (for p <= 1/2) and 2(ceil(n/k) - 1) misses (18 at p = 0.10, 44
    at 9 in 200).
    A p that is not k/n for any n <= MAX_N (e.g. 1/3 + 1e-6) is not rounded
    into a bag; it is drawn with the accumulator below, which is exact for any
    p. If a fleet's p changes (it bought an upgrade), its bag is rebuilt for
    the new p at once; the rest of the old bag is dropped.

Deficit accumulator, for a p that varies per call (`draw_varying`)
    Each draw adds p to the fleet's credit c; the draw hits when c reaches a
    threshold t, and a hit takes 1 off c and draws a new t, uniform in (0, 1].
    So after any run of draws |sum(p) - hits| < 2: the long-run rate is the
    mean p, exactly. c never drops to -1 or below, so the longest miss streak
    is under 2 / p_min draws, and a hit streak is under 2 / (1 - p_max) draws
    (at most 2 in a row while p <= 1/3; a raid at 0.6 odds can make 4).
    The random t keeps a hit's timing unpredictable within each unit of credit.
    One side effect: the credit is the fleet's, not the trip's, so a raid can
    land on an escorted trip; escorts, armor and cheap cargo still cut the
    number of raids a fleet takes, in proportion.

Both kinds draw their randomness from a Random seeded by (desk, game seed,
event, fleet, refill number), so a game repeats per seed, and one fleet's
draws never move another fleet's. State lives in rng_bags, written in the
caller's open transaction (a rolled-back trip gives its marble back), so a
restart keeps it; the referee wipes the table on reset and new game.

p <= 0 never hits and p >= 1 always hits; neither touches state. Tests force
outcomes with force(event, *outcomes).
"""

import random
from collections import deque
from fractions import Fraction
from typing import Deque, Dict, Optional, Tuple

MAX_N = 200
EPS = 1e-9
SEED_EVENT = '__seed__'

SCHEMA = """
CREATE TABLE IF NOT EXISTS rng_bags (
    ns       TEXT NOT NULL,
    event    TEXT NOT NULL,
    fleet    TEXT NOT NULL,
    seed     INTEGER NOT NULL,
    p        REAL NOT NULL DEFAULT 0,
    marbles  TEXT NOT NULL DEFAULT '',
    refills  INTEGER NOT NULL DEFAULT 0,
    credit   REAL NOT NULL DEFAULT 0,
    draws    INTEGER NOT NULL DEFAULT 0,
    hits     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ns, event, fleet)
)
"""


def composition(p: float) -> Optional[Tuple[int, int]]:
    """(hits, bag size) for p, or None when no bag of <= MAX_N marbles holds p."""
    f = Fraction(p).limit_denominator(MAX_N)
    if f.numerator <= 0 or f.numerator >= f.denominator or abs(float(f) - p) > EPS:
        return None
    return f.numerator, f.denominator


class Bags:
    """One desk's bags (ns = 'hazards', 'piracy', ...). conn is the referee's
    sqlite connection; draws never open or commit a transaction."""

    def __init__(self, conn, ns: str):
        self.conn, self.ns = conn, ns
        with conn:
            conn.execute(SCHEMA)
        self._forced: Dict[str, Deque[bool]] = {}
        row = conn.execute("SELECT seed FROM rng_bags WHERE ns = ? AND event = ? AND fleet = ''",
                           (ns, SEED_EVENT)).fetchone()
        self.seed = row[0] if row else 0

    def reset(self, seed: int) -> None:
        """New game: forget every bag of this desk and remember the seed."""
        self.seed = seed
        self._forced.clear()
        with self.conn:
            self.conn.execute("DELETE FROM rng_bags WHERE ns = ?", (self.ns,))
            self.conn.execute("INSERT INTO rng_bags (ns, event, fleet, seed) VALUES (?, ?, '', ?)",
                              (self.ns, SEED_EVENT, seed))

    def force(self, event: str, *outcomes: bool) -> None:
        """Tests: the next draws of `event` (any fleet) return these outcomes."""
        self._forced.setdefault(event, deque()).extend(bool(o) for o in outcomes)

    # ------------------------------------------------------------ draws

    def draw(self, event: str, fleet: str, p: float) -> bool:
        """Fixed-p roll: a marble from the fleet's bag for this event."""
        forced = self._pop_forced(event)
        if forced is not None:
            return forced
        if p <= 0:
            return False
        if p >= 1:
            return True
        comp = composition(p)
        if comp is None:
            return self.draw_varying(event, fleet, p)
        k, n = comp
        row = self._load(event, fleet)
        seed, refills = row['seed'], row['refills']
        marbles = row['marbles']
        if abs(row['p'] - p) > EPS:
            marbles = ''  # p changed: rebuild the bag for the new odds
        if not marbles:
            rng = self._rng(event, fleet, seed, refills)
            sizes = [n // k + (1 if i < n % k else 0) for i in range(k)]
            rng.shuffle(sizes)
            bag = []
            for size in sizes:  # one hit in each run
                run = ['0'] * size
                run[rng.randrange(size)] = '1'
                bag += run
            marbles, refills = ''.join(bag), refills + 1
        hit = marbles[0] == '1'
        self._save(event, fleet, seed, p, marbles[1:], refills, 0.0, row, hit)
        return hit

    def draw_varying(self, event: str, fleet: str, p: float) -> bool:
        """Varying-p roll: the fleet's luck credit for this event."""
        forced = self._pop_forced(event)
        if forced is not None:
            return forced
        if p <= 0:
            return False
        if p >= 1:
            return True
        row = self._load(event, fleet)
        seed, cycle = row['seed'], row['refills']
        credit = (row['credit'] if not row['marbles'] else 0.0) + p
        t = 1.0 - self._rng(event, fleet, seed, cycle).random()  # (0, 1]
        hit = credit >= t
        if hit:
            credit -= 1.0
            cycle += 1
        self._save(event, fleet, seed, p, '', cycle, credit, row, hit)
        return hit

    # ------------------------------------------------------------ state

    def stats(self, event: str, fleet: str) -> Dict[str, int]:
        row = self._load(event, fleet)
        return {'draws': row['draws'], 'hits': row['hits']}

    def _pop_forced(self, event: str) -> Optional[bool]:
        q = self._forced.get(event)
        return q.popleft() if q else None

    def _rng(self, event: str, fleet: str, seed: int, n: int) -> random.Random:
        return random.Random(f"bag-{self.ns}-{seed}-{event}-{fleet}-{n}")

    def _load(self, event: str, fleet: str) -> Dict:
        r = self.conn.execute("SELECT seed, p, marbles, refills, credit, draws, hits FROM rng_bags "
                              "WHERE ns = ? AND event = ? AND fleet = ?", (self.ns, event, fleet or '')).fetchone()
        if r is None:
            return {'seed': self.seed, 'p': -1.0, 'marbles': '', 'refills': 0, 'credit': 0.0,
                    'draws': 0, 'hits': 0}
        return {'seed': r[0], 'p': r[1], 'marbles': r[2], 'refills': r[3], 'credit': r[4],
                'draws': r[5], 'hits': r[6]}

    def _save(self, event, fleet, seed, p, marbles, refills, credit, row, hit) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO rng_bags (ns, event, fleet, seed, p, marbles, refills, credit, draws, hits) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.ns, event, fleet or '', seed, p, marbles, refills, credit, row['draws'] + 1,
             row['hits'] + int(hit)))
