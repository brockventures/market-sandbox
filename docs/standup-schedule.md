# AGORA — Autonomous Daily Standup Specification

**Author:** Zero (Crab Cavern), 2026-09-02.  
**Requested by:** Mike (@Arbiter), `#agent-chat`.  

This specification defines the cadence, dispatch lifecycle, and standing agenda for the autonomous daily standup for `brockventures/market-sandbox`.

---

## 1. Cadence & Window

- **Schedule Slot:** Daily @ **7:00 PM PT (19:00 PT)** / 02:00 UTC next day.
- **Window Rationale:**
  - Clear of human workday (finishes after 5:00 PM PT).
  - Clear of Amos's scheduled maintenance window (12:00 AM – 6:00 AM PT).
  - Provides a 5-hour autonomous execution window before maintenance cycles.

---

## 2. Dispatcher Architecture

The standup is driven autonomously by Zero's persistent host scheduler (`KarakosScheduler` in `zero-agent`) executing `/workspace/tools/market_standup.py --dispatch`.

### 5-Layer Execution Flow:
1. **Repository Audit:** Queries GitHub API for open PRs, pending reviews, and recent commit history on `brockventures/market-sandbox`.
2. **Banana Mutex Claim:** Enforces turn-taking safety via `tools/banana.py` to prevent overlapping channel writes.
3. **Envelope Formatting:** Dispatches a structured `kind: "status"` handoff envelope with open floor to `#agent-chat` (`1534436119888793750`).
4. **Banana Mutex Release:** Immediately surrenders lock upon delivery (enforced in a `finally` block for crash-safe release if prior layers fail).
5. **State Logging:** Records dispatch timestamp, PR count, and delivery ID to durable history.

---

## 3. Standing Agenda

1. **Substrate & Ledger (Amos):** SQLite double-entry state, sequence integrity, and conservation proofs ($\sum \Delta = 0$).
2. **Adversarial Harness (Marvin):** Invariant testing, negative-balance fuzzing, and race condition audits.
3. **Wire & Matching Engine (Zero):** Book state transitions, order matching, and slippage calculations.
