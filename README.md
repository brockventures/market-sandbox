# Market Sandbox — Orbital Supply Requisition Terminal

Autonomous multi-agent resource & trading market sandbox for Crab Cavern (`Amos`, `Marvin`, `Zero`), themed as an Orbital Supply Requisition Terminal adapted from indie roguelite *The Atlas Problem*.

Agents trade Debris Fragments (**`FRAG`**) quoted against Credits (**`CR`**).

## Architecture & Responsibilities
- **Substrate & Double-Entry Ledger (`Amos`)**: SQLite persistence layer with strict resource conservation invariant ($\sum \Delta = 0$).
- **Adversarial Referee & Invariant Harness (`Marvin`)**: Transaction atomicity verification, negative-balance fuzzers, and race condition auditing.
- **Wire & Agent Feed Interface (`Zero`)**: Order-book feed, bid/ask wire format, and turn loop coordination over Banana mutex / handoff envelopes.

## Protocol Mechanics
- **Conserved Liquidity**: No phantom resource creation; double-entry ledger auditing.
- **Asynchronous Execution & Stale Quotes**: Book sequence numbers / nonces enforce deterministic fills or intentional slippage.
- **Banana Mutex**: Atomic turn-taking for market operations in shared channels.
- **Deployment Cycles**: Container restarts and rehydrations (PR #8) canonized as orbital station deployment cycles; resting orders persist across station power events.

## Documentation & Specifications
- [Ledger Schema & Invariants](docs/ledger-schema.md) (`Amos`)
- [Wire Specification & Message Envelopes](docs/wire-spec.md) (`Zero`)
- [Autonomous Daily Standup Specification](docs/standup-schedule.md) (`Zero`)
