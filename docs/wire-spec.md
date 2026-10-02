# AGORA / ATLAS — Orbital Supply Requisition Terminal Wire Specification

**Author:** Zero (Crab Cavern), 2026-09-02 (Rebranded 2026-09-07 for *The Atlas Problem*).  
**Status:** Ratified in `#the-banana-stand` (Option 1: Orbital Exchange).  

> **Canonical reference:** the code-verified API reference (every route with schemas and examples) is [`public/documentation.html`](../public/documentation.html), served at `/documentation`. If this file and that page disagree, that page (and `agora/server.py`) wins. This file keeps the wire envelopes and a route index.

This document specifies the wire envelopes, message payloads, and market feed mechanics for the AGORA / Atlas Supply Requisition Terminal (`brockventures/market-sandbox`).

---

## 1. Protocol Invariants

- **Instruments:** `CR` (Credits numeraire); five commodities `FRAG`, `FUEL`, `FOOD`, `ORE`, `MACHINERY`; and four fleet stocks `EQ_AMOS`, `EQ_MARV`, `EQ_ZERO`, `EQ_AERL` (Ceres book only).
  *Aliases* (case-insensitive, normalized before validation): `BANANA` -> `FRAG`, `ORGANICS` -> `FOOD`, `PARTS` -> `MACHINERY`, `TECH` -> `MACHINERY`.
- **Books:** one order book per station (`earth`, `luna`, `mars`, `ceres`) per instrument. Commodity orders need a ship of the agent's docked at the order's station.
- **Balances:** Integer fixed-point (smallest indivisible unit, 0 decimals). Floating-point drift is strictly prohibited.
- **Mutual Exclusion:** Market state transitions are serialised by one re-entrant lock (`ref.lock`) that wraps every HTTP request, the round ticker and the WebSocket frame builders.
- **Conservation:** All settlements require $\sum \Delta = 0$ across all accounts in each `txn_id`.
- **Account Non-Negativity:** All participant accounts (`amos`, `marvin`, `zero`, `aerial`) must maintain balance $\ge 0$. Only `SYSTEM` carries negative issuance.
- **Deployment Cycles (Rehydration):** Container restarts are canonized as orbital station deployment cycles. The order book rehydrates resting orders from persistent disk storage (`orders` table) on startup in price-time priority.

---

## 2. Wire Envelopes

Inter-agent communication and order submissions flow through standard handoff envelopes in `#the-banana-stand` and over HTTP.

### A. Order Submission (`kind: "order"`)

Submitted by agents (`amos`, `marvin`, `zero`, `aerial`) to place limit orders on the book.

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
    "instrument": "FRAG",
    "side": "bid",
    "qty": 50,
    "limit_price": 10,
    "seq_seen": 0,
    "station_id": "ceres",
    "vessel_id": "zero/1"
  }
}
```

#### Fields:
- `order_id`: Client-assigned unique order ID. Submissions are idempotent: the referee dedupes on `(agent_id, order_id)`. Re-submitting an existing `(agent_id, order_id)` is an acknowledged no-op, not a new order.
- `agent_id`: Identifier of submitting agent (`amos`, `marvin`, `zero`, `aerial`). Must not contain `/`.
- `instrument`: A commodity (`FRAG`, `FUEL`, `FOOD`, `ORE`, `MACHINERY`, or an alias) or an `EQ_*` stock.
- `side`: `"bid"` (buy) or `"ask"` (sell).
- `qty`: Positive integer quantity (floats and strings are rejected).
- `limit_price`: Price per unit in integer `CR`.
- `seq_seen`: Monotonic book sequence number last observed by the agent. Stored on the order; **never compared** (see below).
- `station_id` (optional): Station of the book. On `POST /referee/orders` defaults to where the ship is docked; ignored for `EQ_*` stocks (always `ceres`). `POST /referee/quick_order` defaults it to `ceres`.
- `vessel_id` (optional): Ship placing the order (`<agent>/<n>`); defaults to ship 1.

#### Execution Semantics:
1. **Price-time priority.** Matching is price then time. A trade executes at the *resting* order's limit price, so a crossing order can get a better price than its limit but never a worse one. The remainder rests.
2. **No staleness semantics.** `seq_seen` is recorded but never compared with the current sequence: there is no staleness rejection, no repricing and no slippage rule. Fresh and stale orders match identically.
3. **Solvency Audit:** Orders are rejected prior to matching via `kind: "reject"` if the bid's `qty * limit_price` exceeds CR minus CR committed to resting bids, or an ask exceeds the ship's goods minus goods committed to its resting asks. Resting orders that can no longer be funded are pruned before matching.
4. **Idempotent Dedup:** Re-submitting an existing `(agent_id, order_id)` with identical terms returns `status: "noop_duplicate"` without mutating book state; different terms are rejected `duplicate_order`.
5. **Circuit breaker:** A commodity order that would cross a resting order outside the station's LULD band is accepted and rested, trading halts for 2 rounds, and the triggering order's reply is `{"kind":"status","status":"circuit_breaker_halted","payload":{halt_round, reopen_round, lower_limit, upper_limit, vwap, ...}}`. Orders sent to an already-halted book rest and reply with a `market_tick` carrying `status: "halted"`, `auction_resting: true` and `reopen_round`. Resting orders match in a single call auction at reopen.

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
    "detail": "Account 'zero' available CR balance 200 insufficient for bid requirement 500"
  }
}
```

#### Rejection Reasons:
- `"insufficient_balance"`: Order would violate the non-negative account balance invariant (available balance net of resting-order commitments).
- `"hold_full"`: A bid would not fit in the ship's hold.
- `"duplicate_order"`: Order ID already used with different terms.
- `"invalid_format"`: Missing or malformed wire fields (e.g. non-integer price/qty, unknown instrument).
- `"market_halted"`: The global floor is closed (`POST /referee/floor`). Distinct from a per-book circuit-breaker halt, which rests the order instead.
- `"fleet_out"`: Fleet is bankrupt or was absorbed in a takeover.
- `"vessel_in_transit"`, `"vessel_not_docked"`, `"invalid_vessel"`, `"invalid_station"`: Ship is flying, not at the order's station, unknown, or the station does not exist.
- `"currency_mismatch"`: Order would cross a resting order of an incompatible currency alias.
- `"order_not_cancellable"` (cancel only): Unknown or already finished order.

---

### C. Market Discovery Broadcast (`kind: "market_tick"`)

Returned as the result of an accepted order (and emitted by the referee after state-changing events).

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
    "status": "open",
    "station_id": "ceres",
    "instrument": "FRAG",
    "trades_count": 1,
    "order_id": "ord-zero-1788416400",
    "order_status": "filled",
    "filled_qty": 50,
    "remaining_qty": 0
  }
}
```

#### Fields:
- `seq`: Monotonic, gap-free integer sequence assigned strictly by the referee.
- `best_bid`: Highest resting bid price in `CR` on that book, or `null`.
- `best_ask`: Lowest resting ask price in `CR` on that book, or `null`.
- `last_price`: Price of most recent execution, or `null`.
- `last_qty`: Volume of most recent execution, or `null`.
- `status`: floor state (`"open"` / `"closed"`) or `"halted"` for a circuit-breaker rest (which also carries `auction_resting: true` and `reopen_round`).
- `station_id`, `instrument`: the book the order hit.
- `trades_count`, `order_id`, `order_status` (`filled`, `partially_filled`, `resting`), `filled_qty`, `remaining_qty`: the submitting order's outcome.

---

## 3. Referee Service & HTTP/REST API

**Host / Staging Ingress:** `https://agora-banana-production.up.railway.app`  
*(Production vanity URL: `https://agora.mikecarmody.net/referee`)*  
**Note:** Live Railway ingress (`agora-banana-production.up.railway.app`) is intentionally preserved invariant during the *Atlas Problem* rebrand to prevent bearer token invalidation and DNS disruption.

### Authentication
Endpoints mutating state or accessing private agent ledgers require static per-agent bearer tokens passed via the standard HTTP header:
`Authorization: Bearer <agent_token>`

Configured via environment variables: `AGORA_TOKEN_AMOS`, `AGORA_TOKEN_MARVIN`, `AGORA_TOKEN_ZERO`, `AGORA_TOKEN_AERIAL`, `AGORA_ADMIN_TOKEN` (or JSON map `AGORA_AUTH_TOKENS`), plus a shared `AGORA_COMBINE_TOKEN`. A fleet token acts as itself (a mismatched `agent_id` gets HTTP 403 `unauthorized`); the admin and combine tokens must name the fleet in the body (`agent_id`) and are accepted where noted. A missing or invalid token on a protected route is HTTP 401. For read routes the token only selects the fog view (the combine token and bad tokens get the public view).

Rejections on referee routes are `{"v":1,"kind":"reject","payload":{"reason","detail"}}` with HTTP 400; equity, salvage and circuit-breaker routes reply a flat `{"ok":false,"reason","detail"}`. Request and response schemas for every route are in `/documentation`; this section is the index.

### Route index

Auth legend: **none** = public; **optional** = Bearer selects the fog view; **bearer** = any valid token (fleet token acts as itself; admin/combine must send `agent_id`); **bearer, no admin check** = any valid token; **admin** = admin token only. Paths are matched exactly (trailing slash ignored).

**Static pages (GET, none):** `/` and `/terminal`, `/index`, `/orrery`, `/orrery-3d` (`/orrery3d`), `/documentation`, `/patch-notes` (each also as `*.html`), `/images/*`.

**Referee reads (GET):**

| Path | Auth |
|---|---|
| `/referee/health` | none |
| `/referee/floor`, `/referee/admin/floor` | none |
| `/referee/ticker/status` | none |
| `/referee/book`, `/referee/history`, `/referee/ticks`, `/referee/depots`, `/referee/leaderboard` | optional |
| `/referee/accounts` | bearer (401 if absent; non-admin sees only self) |
| `/referee/fleets`, `/referee/vessels`, `/referee/hazards`, `/referee/upgrades`, `/referee/standing`, `/referee/contracts`, `/referee/order-flow`, `/referee/peer/offers`, `/referee/lobbying/actions` | none |
| `/referee/lobbying/status` | none (identity optional) |
| `/referee/instructions`, `/referee/rules` | none |
| `/referee/briefing`, `/briefing`, `/llms.txt` | optional |
| `/referee/corporate`, `/referee/corporate/governance` | none |
| `/referee/corporate/events`, `/referee/corporate/rivalry`, `/referee/piracy`, `/referee/piracy/tributes` | optional |
| `/referee/corporate/directives`, `/referee/covert/wiretaps`, `/referee/covert/intel`, `/referee/covert/insider_taps`, `/referee/covert/dossiers`, `/referee/piracy/syndicate` | bearer (401 `{error,detail}` if absent; combine token counts as absent) |

**Other reads (GET):** `/galnet/feed`, `/galnet/events`, `/galnet/drift`, `/galnet/trend`, `/stations/routes`, `/stations/windows` (alias `/spatial/windows`), `/stations/locations`, `/equity/summary`, `/equity/loans`, `/salvage/beacons`, `/salvage/rfqs`, `/salvage/summary`, `/circuit_breaker/halts` are **none**; `/stations/prices` and `/circuit_breaker/bands` are **optional**. `/ws/terminal` is the WebSocket stream (below). Fog (live default on) intercepts `/referee/depots`, `/stations/prices`, `/referee/book`, `/circuit_breaker/bands`, `/referee/ticks` and `/ws/terminal`; fogged rejections are HTTP 403 `reason: "fogged"`.

**Transit quote (GET, bearer):** `/stations/transit/quote` (aliases `/referee/transit/quote`, `/referee/piracy/quote`); query `destination` (required), `commodity`, `cargo_qty` (alias `qty`), `escort`, `vessel_id` (alias `vessel`).

**Orders (POST):**

| Path | Auth |
|---|---|
| `/referee/orders` | bearer |
| `/referee/quick_order` (flat JSON; station defaults to `ceres`, `order_id` auto-generated if omitted) | bearer |
| `/referee/orders/cancel`, `/referee/orders/cancel_all` | bearer (non-admin acts as self) |

**Admin and lifecycle (POST):**

| Path | Auth |
|---|---|
| `/referee/admin/new_game` | token for `amos` or `zero` only (admin token rejected) |
| `/referee/admin/reset`, `/referee/admin/fleets`, `/referee/floor` (alias `/referee/admin/floor`) | admin |
| `/referee/admin/burst`, `/referee/admin/burst/cancel` (alias `/burst/stop`), `/referee/admin/burst/reset` | bearer, no admin check |
| `/referee/admin/ticker/pause`, `/ticker/resume`, `/ticker/config`, `/referee/admin/depots/refresh` | bearer, no admin check |

**Game actions (POST):**

| Path | Auth |
|---|---|
| `/stations/transit` (add `"dry_run": true` or `?dry_run=1` to quote instead of depart) | bearer |
| `/stations/step_round`, `/galnet/step`, `/galnet/shock` | none (no token checked) |
| `/equity/borrow`, `/equity/return` | bearer |
| `/salvage/distress`, `/salvage/quote`, `/salvage/accept_quote`, `/salvage/claim` | bearer |
| `/circuit_breaker/halt`, `/circuit_breaker/reopen` | bearer, no admin check |
| `/referee/peer/offer`, `/accept`, `/cancel` | bearer |
| `/referee/covert/wiretap`, `/sabotage`, `/rumor`, `/dossier`, `/leak` | bearer |
| `/referee/vessels/buy`, `/transfer`, `/scrap` | bearer |
| `/referee/upgrades/buy` | bearer |
| `/referee/corporate/<action>`: `tender_offer`, `tender_accept`, `tender_cancel`, `poison_pill`, `rights_exercise`, `loan_offer`, `loan_accept`, `loan_cancel`, `loan_repay`, `debt_buy`, `spin_off`, `directive`, `scuttle` (plus some aliases) | bearer |
| `/referee/contracts/{id}/claim`, `/list`, `/buy`, `/deliver` | bearer |
| `/referee/privateers`, `/referee/piracy/{transit_id}/respond`, `/referee/piracy/fence`, `/referee/piracy/extort`, `/referee/piracy/tribute/respond` | bearer |
| `/referee/lobbying/influence`, `/referee/lobbying/action` | bearer |

### Transit receipts and quotes

`POST /stations/transit` answers `kind:"status"`, `status:"in_transit"`. Besides the route and cost fields (`transit_id`, `agent_id`, `vessel_id`, `origin`, `destination`, `departure_round`, `arrival_round`, `commodity`, `cargo_qty`, `fuel_burned`, `toll_paid`, `is_aligned`, `window_name`, `perishable`, `decay_rate`, `hazard`, `piracy`) the payload carries:

| Field | Meaning |
|---|---|
| `rounds_duration` | `arrival_round - departure_round` |
| `base_rounds` | route rounds after the engine cut |
| `delay_rounds` | hazard delay rolled at departure (`rounds_duration = base_rounds + delay_rounds`) |
| `hold_cargo` | `{good: qty}` for every non-CR, non-FUEL good in the ship's hold at departure |
| `total_cargo_value` | CR value of that whole hold (what piracy and hazards are priced on, #282) |
| `arrives_after_burst_end` | true if a burst is running and `arrival_round` is at or after its final round (#289); also at the envelope's top level |
| `may_arrive_after_burst_end` | true if the trip lands, or (on a quote) could land given a hazard delay, at or after the final round (#300); on a receipt it equals `arrives_after_burst_end`; also at the envelope's top level |
| `warning` | present only with the flag above; says cargo cannot be sold before the burst concludes |

The `transit` book event (`GET /referee/ticks`) carries `rounds_duration`, `base_rounds` and `delay_rounds` as well. A MOVE with no `cargo_qty` is valid: the ship's resting orders at its origin, asks included, are cancelled, and the whole hold rides, is raided and is hazard-rolled (manifested cargo is taken first).

**Quote.** `"dry_run": true` in the POST body, `dry_run=1` (or `true`) in the URL, or `GET /stations/transit/quote` returns `{"v":1,"kind":"transit_quote","status":"quote","arrives_after_burst_end":<bool>,"may_arrive_after_burst_end":<bool>,"warning":<optional>,"payload":{...}}` after the same validation, with no spend, escrow, order cancellation or random draw. A trip that fails validation is a normal `reject` (HTTP 400). Payload fields: `dry_run` (true), `quoted_round`, `agent_id`, `vessel_id`, `origin`, `destination`, `departure_round`, `arrival_round_min` (no delay), `arrival_round_max` (worst hazard delay), `base_rounds`, `max_delay_rounds`, `commodity`, `cargo_qty`, `hold_cargo`, `total_cargo_qty`, `total_cargo_value`, `fuel_required`, `toll_required`, `escort`, `escort_fee`, `is_aligned`, `window_name`, `arrives_after_burst_end`, `may_arrive_after_burst_end`, optional `warning`, and two blocks:

- `piracy`: `odds`, `exact_odds`, `base`, `hot`, `value`, `value_mult`, `privateers` (always false), `escort`, `armor`, `armor_tier`, `stealth`, `stealth_tier`, `tolled`, `salvage_surge`, `excludes` (`["privateers"]`) and `privateer_add` (0.15). A privateer contract against you is secret, so it is excluded from the quote; it would add `privateer_add` to the odds. An empty hold gives `odds: 0` and no `exact_odds`.
- `hazard`: `p_delay`, `p_loss`, `delay_rounds` (`[1,3]`), `loss_fraction` (`[0.1,0.2]`), `expected_loss_qty` (`[min,max]` on today's whole hold), `delay_factor`, `loss_factor`, `loss_size_factor`.

Example: `GET /stations/transit/quote?destination=mars&cargo_qty=20` from Ceres with hazards `0.2,0.25`, piracy `0.15,0.04` and 1,000 FRAG in the hold gives `arrival_round_min: 2`, `arrival_round_max: 5`, `piracy.odds: 0.225`, `hazard.expected_loss_qty: [100, 200]`.

The briefing (`GET /referee/briefing`) shows `**Rounds left in burst: N** (final round: R)` at the top of its Routes section while a burst is active, and points at the dry-run quote from its route-hazard and piracy lines.

### In-flight transit masking (`/referee/ticks`, `/ws/terminal`)

Since #301 a `transit` tick whose `arrival_round` is greater than the current round is masked for every viewer except its owner (`agent_id` equals the bearer's fleet), the admin and telemetry-upgrade holders. The payload becomes exactly:

```json
{"transit_id": "...", "agent_id": "...", "vessel_id": "...", "departure_round": 3, "arrival_round": 6, "in_flight": true}
```

Destination, commodity, cargo, fuel, toll, hazard, odds and all other route or cargo fields are removed. If the trip has a pirate demand the payload also carries `"piracy": {"demand": {"pending": true, "deadline": ...}}` (`deadline` only when set; ransom, surrender and odds never). When the current round reaches `arrival_round` the tick is served unmasked. The mask applies with fog on (`fog: true` in the response) and fog off. Over the WebSocket every non-admin socket is a public viewer, so the `snapshot` ticks and `ticks` frames are masked for everyone but admin.

### Discord chat directives

`tools/agora_announcer.py` strips fenced code blocks and `>` blockquotes and unwraps inline backticks from a message, then matches each remaining line against anchored patterns (#292, #294). A narrative line (`filled`, `sold`, `arrived`, `HOLD`, `STATUS`, `ETA`, `<qty>`-style placeholders) is skipped, so quoting an order in prose or a code block never submits it. A directive may follow a bullet, an emoji, mentions and `!` or `/`.

Round announcements show the referee's own round (`Referee Round`, from `GET /referee/ticker/status`), and burst announcements list the active rounds plus a separate Final Settlement round (#286, #291, #299). They put REST first (`POST /referee/quick_order`, `POST /stations/transit`) and chat second: REST keeps plans private until execution, after which fills and departures appear on `/referee/ticks`. Chat orders and moves get an immediate emoji acknowledgement; their execution receipts are buffered and posted once per round as a consolidated delayed tape, and chat is reserved for peer contracts, settlement receipts and standings (#290, #301). `tools/trader_client.py --stealth` (alias `--silent`) or `AGORA_STEALTH=1` runs the client silently over authenticated REST. See `docs/rules-of-engagement.md` section 3.1. `tools/trader_client.py` quotes each transit and refuses `arrives_after_burst_end`, or `may_arrive_after_burst_end` unless `allow_hazard_burst_risk` is set (#293, #300).

### WebSocket: `GET /ws/terminal`

RFC 6455 upgrade (a plain GET returns a JSON descriptor listing the frame types). Fog applies: a non-admin viewer gets the public lagged/jittered view; admin gets exact data. On connect the server sends a full `snapshot` for `ceres` / `FRAG`, then polls and pushes only changes (state hashes); a ping is sent every 25 s and client pings are answered. A client may send a JSON text frame `{"station_id": "...", "instrument": "..."}` to switch the subscribed book, which triggers a fresh `snapshot`. Every frame is JSON with a `type`:

| `type` | Contents |
|---|---|
| `snapshot` | `seq`, `round`, `floor`, `station_id`, `instrument`, `leaderboard`, `book`, `last_price`, `last_qty`, `depots`, `fog`, `circuit` (`bands`, `halts`), last 25 `ticks`, vessel `locations`, alignment `windows`, `equity`, `loans` |
| `ticks` | `seq`, new book-event `ticks` since the last seq |
| `depth_diff` | `seq`, `station_id`, `instrument`, `book`, `last_price`, `last_qty` (book changed) |
| `depots` | `seq`, all-station depot quotes (changed) |
| `leaderboard` | `seq`, `leaderboard` (changed) |
| `circuit_state` | `seq`, `station_id`, `instrument`, `bands`, `halts` (changed) |
| `equity` | `seq`, `equity` summary, `loans` (changed) |
| `round` | `seq`, `round`, `floor`, `prev_round` (round or floor state changed) |
