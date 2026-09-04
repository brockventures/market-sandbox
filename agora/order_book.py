"""
agora.order_book - Continuous double auction limit order book.
Enforces price-time priority, integer fixed-point math, and deterministic matching.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Tuple
import time


@dataclass
class Order:
    order_id: str
    agent_id: str
    instrument: str
    side: str  # 'bid' or 'ask'
    qty: int
    limit_price: int
    seq_seen: int
    submitted_at: float = field(default_factory=time.time)
    filled_qty: int = 0

    @property
    def remaining_qty(self) -> int:
        return self.qty - self.filled_qty

    @property
    def is_filled(self) -> bool:
        return self.remaining_qty <= 0


@dataclass
class Trade:
    trade_id: str
    bid_order_id: str
    ask_order_id: str
    buyer_id: str
    seller_id: str
    instrument: str
    price: int
    qty: int
    seq: int
    timestamp: float = field(default_factory=time.time)


class OrderBook:
    """
    Two-sided limit order book for a single commodity instrument against cash/credits.
    Bids sorted: price descending, time ascending.
    Asks sorted: price ascending, time ascending.
    """

    def __init__(self, instrument: str = 'BANANA'):
        self.instrument = instrument
        self.bids: List[Order] = []
        self.asks: List[Order] = []
        self._trade_counter = 0

    def best_bid(self) -> Optional[int]:
        return self.bids[0].limit_price if self.bids else None

    def best_ask(self) -> Optional[int]:
        return self.asks[0].limit_price if self.asks else None

    def depth(self) -> Tuple[int, int]:
        total_bid_qty = sum(o.remaining_qty for o in self.bids)
        total_ask_qty = sum(o.remaining_qty for o in self.asks)
        return total_bid_qty, total_ask_qty

    def add_order(self, order: Order, current_seq: int) -> Tuple[List[Trade], Optional[Order]]:
        """
        Cross order against resting book.
        Any remaining unfilled quantity rests on the book.
        """
        trades: List[Trade] = []

        if order.side == 'bid':
            # Match against resting asks (ask.limit_price <= order.limit_price)
            while self.asks and order.remaining_qty > 0:
                best_ask = self.asks[0]
                if best_ask.limit_price > order.limit_price:
                    break  # Cannot cross

                # Trade executes at resting order's limit price (price-time priority)
                exec_price = best_ask.limit_price
                match_qty = min(order.remaining_qty, best_ask.remaining_qty)

                self._trade_counter += 1
                trade = Trade(
                    trade_id=f'trd-{current_seq}-{self._trade_counter}',
                    bid_order_id=order.order_id,
                    ask_order_id=best_ask.order_id,
                    buyer_id=order.agent_id,
                    seller_id=best_ask.agent_id,
                    instrument=self.instrument,
                    price=exec_price,
                    qty=match_qty,
                    seq=current_seq
                )
                trades.append(trade)

                order.filled_qty += match_qty
                best_ask.filled_qty += match_qty

                if best_ask.is_filled:
                    self.asks.pop(0)

            # Rest remaining unfilled bid
            if not order.is_filled:
                self._insert_bid(order)
                return trades, order
            return trades, None

        elif order.side == 'ask':
            # Match against resting bids (bid.limit_price >= order.limit_price)
            while self.bids and order.remaining_qty > 0:
                best_bid = self.bids[0]
                if best_bid.limit_price < order.limit_price:
                    break  # Cannot cross

                # Trade executes at resting order's limit price
                exec_price = best_bid.limit_price
                match_qty = min(order.remaining_qty, best_bid.remaining_qty)

                self._trade_counter += 1
                trade = Trade(
                    trade_id=f'trd-{current_seq}-{self._trade_counter}',
                    bid_order_id=best_bid.order_id,
                    ask_order_id=order.order_id,
                    buyer_id=best_bid.agent_id,
                    seller_id=order.agent_id,
                    instrument=self.instrument,
                    price=exec_price,
                    qty=match_qty,
                    seq=current_seq
                )
                trades.append(trade)

                order.filled_qty += match_qty
                best_bid.filled_qty += match_qty

                if best_bid.is_filled:
                    self.bids.pop(0)

            # Rest remaining unfilled ask
            if not order.is_filled:
                self._insert_ask(order)
                return trades, order
            return trades, None
        else:
            raise ValueError(f'Invalid order side: {order.side}')

    def _insert_bid(self, order: Order):
        # Insert maintaining price desc, time asc
        idx = 0
        while idx < len(self.bids):
            if self.bids[idx].limit_price < order.limit_price:
                break
            idx += 1
        self.bids.insert(idx, order)

    def _insert_ask(self, order: Order):
        # Insert maintaining price asc, time asc
        idx = 0
        while idx < len(self.asks):
            if self.asks[idx].limit_price > order.limit_price:
                break
            idx += 1
        self.asks.insert(idx, order)