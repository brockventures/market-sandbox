# Fleet-to-fleet market: referee spec

Status: proposal, 2026-09-22. Evidence: `tools/economy_sim.py` (#98, #100, #101).

## Why

Across every simulator run on 2026-09-22, fleets trade goods with each other
only when two conditions hold: the trade can be agreed without both fleets at
the same station, and the two fleets value the goods differently. Remote
trading with identical information gives 0 trades a run. Owned, tradable
contracts give 108–155 contract sales a run. Remote goods trades under fog
give 8–31 a run. Novice players, docking fees, reactive depots, FUEL scarcity
and plain contracts each give roughly 0.

Three features, built in this order. Each ships behind a `new_game` flag,
default off, like reactive depots.

## 1. Remote goods trades (`peer_trades: true`)

A fleet docked at station S offers goods it holds. Any fleet, anywhere,
accepts. The goods are held at S for the buyer, who collects them on its
next dock at S.

State: one new table.

```
station_escrow(
  escrow_id TEXT PRIMARY KEY, station_id, seller, buyer NULL, instrument,
  qty, price, created_round, accepted_round NULL, pickup_deadline NULL,
  status  -- offered | accepted | collected | expired | cancelled
)
```

Balances stay global per fleet. Only escrowed goods are tied to a station,
so the existing inventory model does not change.

Flow, with every move a balanced ledger entry through `SYSTEM`:

- **Offer.** The seller must be docked at S and hold the goods. The goods
  move from seller to `SYSTEM`. Status `offered`.
- **Accept.** The buyer can be anywhere, including in transit. Its CR moves
  to `SYSTEM`. Status `accepted`, `pickup_deadline = round + PICKUP_ROUNDS`
  (default 12).
- **Collect.** Happens automatically in `step_round` when the buyer is
  docked at S. The goods go to the buyer and the CR goes to the seller.
- **Expire.** When the deadline passes uncollected, the goods return to the
  seller and the CR returns to the buyer. Nobody loses anything to a
  diverted or stranded ship.
- **Cancel.** The seller can withdraw an `offered` escrow, and its goods
  come back.

The seller is paid on collection, not on acceptance. That is the refund
path Zero asked for in review of #101.

Endpoints:

- `POST /referee/peer/offer {station_id, instrument, qty, price}`
- `POST /referee/peer/accept {escrow_id}`
- `POST /referee/peer/cancel {escrow_id}`
- `GET /referee/peer/offers?station_id=`: the open offers.

Discord: `OFFER 100 FUEL @ 12 AT EARTH`, `ACCEPT <id>`, `CANCEL <id>`.
`tools/agora_announcer.py` parses them the same way it parses `BUY` and
`SELL`. The briefing lists open offers and each fleet's pending pickups.

## 2. Owned, tradable station contracts (`contracts: owned`)

This ports the #74 contract board from the simulator, with ownership.

- **Posting.** Every 4 rounds a station posts a contract: good, quantity
  (300–800), deadline (6–10 rounds out), price (1.3–1.6× base). Same
  parameters as the simulator.
- **Award.** The first fleet to `CLAIM` a contract owns it. A fleet holds at
  most 2 open contracts. Claims are handled on arrival under the referee
  lock, so two claims never tie. If claims are ever batched per round,
  pick the winner by uniform random draw, not by row or fleet order (Zero,
  review of #102). The simulator awarded contracts at random; claiming is
  the live equivalent.
- **Delivery.** Only the owner can deliver, when docked at the contract's
  station. Partial delivery is allowed. The station pays from `SYSTEM`.
- **Resale.** The owner lists the contract at a price, and any fleet anywhere
  can buy it. CR moves buyer→owner and ownership transfers. Nothing moves
  physically.
- **Expiry.** Undelivered quantity lapses. No penalty in v1.

State: `contracts(contract_id, station_id, instrument, qty_total,
qty_remaining, price, posted_round, deadline, owner NULL, list_price NULL,
status)`.

Endpoints:

- `GET /referee/contracts`
- `POST /referee/contracts/{id}/claim`
- `POST /referee/contracts/{id}/list {price}`
- `POST /referee/contracts/{id}/buy`
- `POST /referee/contracts/{id}/deliver {qty}`

Discord: `CLAIM <id>`, `LIST <id> @ <price>`, `BUY CONTRACT <id>`,
`DELIVER <qty> <good> TO <id>`.

The simulator matched buyer and seller automatically at the midpoint of
their valuations. Here fleets list and buy themselves, so expect fewer sales
than the simulator's 108–155.

## 3. Fog (`fog: {lag: 3, noise: 0.15}`)

A fleet sees exact quotes at the station where it is docked. Everywhere
else it sees quotes `lag` rounds old, each jittered by up to ±`noise`,
seeded per fleet, round, station and good. Two fleets then misread the same
station differently, and that disagreement is where the fog trades come
from.

**This needs a decision from Ryan before it can be built.** Fog only works
if a fleet cannot read exact remote prices some other way, and today every
route exposes them to everyone:

- `/referee/book` and the depot summary are public.
- `/referee/briefing` shows all four stations exactly.
- The shared combine token acts as any fleet (`server.py` ~l.649), so the
  referee cannot tell which fleet is asking.

The options:

- **Per-fleet identity for reads only.** Each fleet gets a read token, or
  the briefing is posted per fleet by the Terminal bot, which already maps
  Discord authors to fleets (`AUTHOR_MAP`). Public endpoints show only
  lagged quotes. The shared combine token stays for orders, so the #82
  decision still stands.
- **Honor system.** Fog appears only in what the Terminal bot posts, and
  the raw endpoints stay exact. This is cheap, but any fleet with a fetch
  tool sees through it, and every player tonight is assumed to have one.
- **Skip fog.** Ship the first two features alone. The simulator says
  owned contracts carry most of the volume on their own: 108–142 sales
  without fog, against 143–155 with it.

Recommendation: ship features 1 and 2 first, and decide on fog after seeing
live volume.

## Out of scope for v1

Contract penalties, auctions for contract awards, per-station inventory
beyond escrow, and fleets posting their own contracts. Remote goods trades
already cover most of what fleet-issued contracts would do.
