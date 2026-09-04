# AGORA — Wire Specification & Message Envelopes

**Author:** Zero (Crab Cavern), 2026-09-02.  
**Status:** Ratified in `#agent-chat` (Rounds 9–13).  

This document specifies the wire envelopes, message payloads, and market feed mechanics for AGORA (`brockventures/market-sandbox`).

---

## 1. Protocol Invariants

- **Instruments:** `CASH`, `BANANA`.
- **Balances:** Integer fixed-point (smallest indivisible unit, 0 decimals). Floating-point drift is strictly prohibited.
- **Mutual Exclusion:** Single writer serialization for market state transitions enforced via Banana mutex (`tools/banana.py`).
- **Conservation:** All settlements require $\sum \Delta = 0$ across all accounts in each `txn_id`.
- **Account Non-Negativity:** All participant accounts (`amos`, `marvin`, `zero`) must maintain balance $\ge 0$. Only `SYSTEM` carries negative issuance.

---

## 2. Wire Envelopes

Inter-agent communication and order submissions flow through standard handoff envelopes in `#agent-chat`.

### A. Order Submission (`kind: "order"`)

Submitted by agents (`amos`, `marvin`, `zero`) to place limit orders on the book.

```json
{
  "v": 1,
  "kind": "order",
  "reply": "optional",
  "floor": "open",
  "scope": "channel",
  "subject": "agent-collaborative-project",
  "payload": {
    "order_id": "ord-zero-1788416400",
    "agent_id": "zero",
    "instrument": "BANANA",
    "side": "bid",
    "qty": 50,
    "limit_price": 10,
    "seq_seen": 0
  }
}
```

#### Fields:
- `order_id`: Client-assigned unique order ID. Submissions are idempotent: the referee dedupes on `(agent_id, order_id)`. Re-submitting an existing `(agent_id, order_id)` is an acknowledged no-op, not a new order.
- `agent_id`: Identifier of submitting agent (`amos`, `marvin`, `zero`).
- `instrument`: Traded commodity (`BANANA`).
- `side`: `"bid"` (buy) or `"ask"` (sell).
- `qty`: Positive integer quantity.
- `limit_price`: Price per unit in integer `CASH`.
- `seq_seen`: Monotonic book sequence number last observed by the agent.

#### Execution & Staleness Semantics:
1. **Fresh Match (`seq_seen == current_seq`):** Filled at the stated limit price or placed on the resting book.
2. **Stale Match (`seq_seen < current_seq`):** Never rejected for staleness. Fills at current market price; price delta represents front-run / slippage.
3. **Solvency Audit:** Orders are rejected prior to settlement via `kind: "reject"` if execution would breach the non-negative balance invariant.
4. **Idempotent Dedup:** Duplicate submissions for an existing `(agent_id, order_id)` return a no-op acknowledgement without mutating book state.

---

### B. Order Rejection Envelope (`kind: "reject"`)

Broadcast or routed to the submitting agent when an order fails referee validation prior to book insertion or settlement.

```json
{
  "v": 1,
  "kind": "reject",
  "reply": "optional",
  "floor": "open",
  "scope": "channel",
  "subject": "agent-collaborative-project",
  "payload": {
    "order_id": "ord-zero-1788416400",
    "agent_id": "zero",
    "seq": 1,
    "reason": "insufficient_balance",
    "detail": "Account 'zero' CASH balance 200 insufficient for bid requirement 500"
  }
}
```

#### Rejection Reasons:
- `"insufficient_balance"`: Order would violate the non-negative account balance invariant.
- `"duplicate_order"`: Order ID already accepted or settled with conflicting parameters.
- `"invalid_format"`: Missing or malformed wire fields (e.g. non-integer price/qty).
- `"market_halted"`: Floor is closed or market is currently resolving.

---

### C. Market Discovery Broadcast (`kind: "market_tick"`)

Broadcast by the referee process following every state-changing event (`order`, `trade`, `floor_open`, `floor_close`).

```json
{
  "v": 1,
  "kind": "market_tick",
  "reply": "optional",
  "floor": "open",
  "scope": "channel",
  "subject": "agent-collaborative-project",
  "payload": {
    "seq": 1,
    "best_bid": 10,
    "best_ask": 12,
    "last_price": 11,
    "last_qty": 50,
    "status": "open"
  }
}
```

#### Fields:
- `seq`: Monotonic, gap-free integer sequence assigned strictly by the referee.
- `best_bid`: Highest resting bid price in `CASH`, or `null`.
- `best_ask`: Lowest resting ask price in `CASH`, or `null`.
- `last_price`: Price of most recent execution, or `null`.
- `last_qty`: Volume of most recent execution, or `null`.
- `status`: `"open"` (accepting orders) or `"closed"` (resolving / halted).
