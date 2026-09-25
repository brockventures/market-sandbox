"""
agora/history.py - Price history (OHLCV) engine for commodities and equities (Issue #122).

Tracks per-round Open, High, Low, Close, Volume for:
- Commodities across all Sol stations (FRAG, FUEL, FOOD, ORE)
- Fleet equities on the public exchange (EQ_AMOS, EQ_ZERO, etc.)

Supports fog of war:
- Exact history where a fleet is docked.
- Remote station history is lagged and jittered per viewer, with volume hidden.
- Stock exchange history is public and never fogged.
"""

import sqlite3
from typing import Any, Dict, List, Optional

from agora.spatial import BASE_PRICES, COMMODITIES, STATIONS

STOCK_EXCHANGE_STATION = "ceres"


class PriceHistoryEngine:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._init_db()

    def _init_db(self) -> None:
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    station_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (station_id, instrument, round)
                );
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_price_history_lookup
                ON price_history (station_id, instrument, round DESC);
            """)

    def backfill_if_empty(self, ref: Any) -> None:
        """If price_history is empty but station_prices has rows, backfill from station_prices."""
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM price_history")
            row = cur.fetchone()
            if row and row['c'] > 0:
                return

            cur.execute("SELECT station_id, commodity, round, spot_price FROM station_prices")
            rows = cur.fetchall()
            with self.conn:
                for r in rows:
                    st = (r['station_id'] or '').lower().strip()
                    comm = (r['commodity'] or '').upper().strip()
                    rnd = int(r['round'])
                    p = round(float(r['spot_price']), 2)
                    self.conn.execute("""
                        INSERT OR IGNORE INTO price_history
                            (station_id, instrument, round, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """, (st, comm, rnd, p, p, p, p))
        except Exception:
            pass

    def record_genesis(
        self,
        opening_prices: Dict[str, Dict[str, float]],
        stock_marks: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record round 0 baseline prices for commodities and stocks."""
        with self.conn:
            for st, comms in opening_prices.items():
                st_key = st.lower().strip()
                for comm, px in comms.items():
                    comm_key = comm.upper().strip()
                    p = round(float(px), 2)
                    self.conn.execute("""
                        INSERT OR REPLACE INTO price_history
                            (station_id, instrument, round, open, high, low, close, volume)
                        VALUES (?, ?, 0, ?, ?, ?, ?, 0)
                    """, (st_key, comm_key, p, p, p, p))

            if stock_marks:
                for sym, info in stock_marks.items():
                    sym_key = sym.upper().strip()
                    if isinstance(info, (int, float)):
                        mark = round(float(info), 2)
                    elif isinstance(info, dict):
                        mark = round(float(info.get('mark', info.get('nav', 20.0))), 2)
                    else:
                        mark = 20.0
                    self.conn.execute("""
                        INSERT OR REPLACE INTO price_history
                            (station_id, instrument, round, open, high, low, close, volume)
                        VALUES (?, ?, 0, ?, ?, ?, ?, 0)
                    """, (STOCK_EXCHANGE_STATION, sym_key, mark, mark, mark, mark))

    def record_round_start(
        self,
        round_num: int,
        spot_prices: List[Any],
        stock_marks: Optional[Dict[str, Any]] = None
    ) -> None:
        """Called at step_round to initialize the candle for round_num."""
        for sp in spot_prices:
            st = sp.station_id.lower().strip()
            comm = sp.commodity.upper().strip()
            p = round(float(sp.spot_price), 2)
            self.conn.execute("""
                INSERT OR REPLACE INTO price_history
                    (station_id, instrument, round, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """, (st, comm, round_num, p, p, p, p))

        if stock_marks:
            for sym, info in stock_marks.items():
                sym_key = sym.upper().strip()
                if isinstance(info, (int, float)):
                    mark = round(float(info), 2)
                elif isinstance(info, dict):
                    mark = round(float(info.get('mark', info.get('nav', 20.0))), 2)
                else:
                    mark = 20.0
                self.conn.execute("""
                    INSERT OR REPLACE INTO price_history
                        (station_id, instrument, round, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """, (STOCK_EXCHANGE_STATION, sym_key, round_num, mark, mark, mark, mark))

    def record_trade(
        self,
        station_id: str,
        instrument: str,
        price: float,
        qty: int,
        round_num: int
    ) -> None:
        """Called whenever an order matches or an auction clears."""
        st = station_id.lower().strip()
        inst = instrument.upper().strip()
        p = round(float(price), 2)
        q = int(qty)
        cur = self.conn.cursor()
        cur.execute("""
            SELECT open, high, low, close, volume
            FROM price_history
            WHERE station_id = ? AND instrument = ? AND round = ?
        """, (st, inst, round_num))
        row = cur.fetchone()
        if row:
            new_high = max(float(row['high']), p)
            new_low = min(float(row['low']), p)
            new_close = p
            new_vol = int(row['volume']) + q
            self.conn.execute("""
                UPDATE price_history
                SET high = ?, low = ?, close = ?, volume = ?
                WHERE station_id = ? AND instrument = ? AND round = ?
            """, (new_high, new_low, new_close, new_vol, st, inst, round_num))
        else:
            self.conn.execute("""
                INSERT INTO price_history
                    (station_id, instrument, round, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (st, inst, round_num, p, p, p, p, q))

    def get_history(
        self,
        ref: Any,
        station_id: str,
        instrument: str,
        rounds: int = 20,
        viewer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        st = station_id.lower().strip()
        inst = instrument.upper().strip()
        is_stock = inst.startswith("EQ_")
        if is_stock:
            st = STOCK_EXCHANGE_STATION
        rounds = max(1, min(300, int(rounds)))
        fog = getattr(ref, 'fog', None)

        with ref.lock:
            cur = self.conn.cursor()
            fetch_limit = rounds + (fog.lag if fog else 0) + 5
            cur.execute("""
                SELECT round, open, high, low, close, volume
                FROM price_history
                WHERE station_id = ? AND instrument = ?
                ORDER BY round DESC
                LIMIT ?
            """, (st, inst, fetch_limit))
            rows = [dict(r) for r in cur.fetchall()]

        # Sort ascending by round
        rows.sort(key=lambda r: r['round'])

        # If empty but station_prices has data, try backfilling and refetching
        if not rows and not is_stock:
            self.backfill_if_empty(ref)
            with ref.lock:
                cur = self.conn.cursor()
                cur.execute("""
                    SELECT round, open, high, low, close, volume
                    FROM price_history
                    WHERE station_id = ? AND instrument = ?
                    ORDER BY round DESC
                    LIMIT ?
                """, (st, inst, fetch_limit))
                rows = [dict(r) for r in cur.fetchall()]
            rows.sort(key=lambda r: r['round'])

        # Fog processing: remote stations are lagged and jittered per viewer, with volume hidden
        if not is_stock and fog and not fog.exact_station(ref, viewer, st):
            max_visible_round = max(0, ref.current_round - fog.lag)
            filtered = []
            for r in rows:
                rnd = r['round']
                if rnd > max_visible_round:
                    continue
                o = fog._jitter(viewer, rnd, st, inst, 'open', r['open'])
                c = fog._jitter(viewer, rnd, st, inst, 'close', r['close'])
                h = fog._jitter(viewer, rnd, st, inst, 'high', r['high'])
                l = fog._jitter(viewer, rnd, st, inst, 'low', r['low'])
                filtered.append({
                    'round': rnd,
                    'open': o,
                    'high': max(h, o, c),
                    'low': min(l, o, c),
                    'close': c,
                    'volume': None,  # volume hidden under fog
                })
            return filtered[-rounds:]

        return rows[-rounds:]

    def get_recent_summary(
        self,
        ref: Any,
        station_id: str,
        rounds: int = 5,
        viewer: Optional[str] = None
    ) -> Dict[str, Any]:
        """Returns per-round close prices for all 4 commodities for the briefing table."""
        st = station_id.lower().strip()
        data_by_comm: Dict[str, Dict[int, float]] = {}
        all_rounds: set = set()
        for comm in COMMODITIES:
            hist = self.get_history(ref, st, comm, rounds=rounds, viewer=viewer)
            comm_map: Dict[int, float] = {}
            for item in hist:
                comm_map[item['round']] = item['close']
                all_rounds.add(item['round'])
            data_by_comm[comm] = comm_map

        sorted_rounds = sorted(all_rounds)
        round_rows = []
        for r in sorted_rounds[-rounds:]:
            entry = {'round': r}
            for comm in COMMODITIES:
                entry[comm] = data_by_comm.get(comm, {}).get(r)
            round_rows.append(entry)

        return {
            'station_id': st,
            'rounds': round_rows,
        }

    def briefing_table(
        self,
        ref: Any,
        station_id: str,
        rounds: int = 5,
        viewer: Optional[str] = None
    ) -> List[str]:
        """Generates markdown price history table for the briefing."""
        summary = self.get_recent_summary(ref, station_id, rounds=rounds, viewer=viewer)
        rows = summary.get('rounds', [])
        if not rows:
            return []

        lines = [
            f"## Recent price history ({station_id.capitalize()})",
            "| Round | " + " | ".join(COMMODITIES) + " |",
            "|---|" + "---|" * len(COMMODITIES),
        ]
        for r in rows:
            cells = []
            for comm in COMMODITIES:
                val = r.get(comm)
                cells.append(f"{val:.2f}" if isinstance(val, (int, float)) else "-")
            lines.append(f"| {r['round']} | " + " | ".join(cells) + " |")

        lines.append("")
        lines.append(
            f"Full OHLCV history: `GET /referee/history?station_id={station_id}&instrument=FRAG&rounds=20`"
        )
        return lines
