#!/usr/bin/env python3
"""
market_standup.py - Crab Cavern Autonomous Market Sandbox Standup Dispatcher

Runs daily at 07:00 PM PT (19:00 PT) via KarakosScheduler in schedule.json.
Autonomously syncs progress, open PRs, and blockers across Zero, Amos, and Marvin
for the brockventures/market-sandbox project in #the-banana-stand.

Workflow:
1. Queries GitHub API for brockventures/market-sandbox (open PRs, latest commits).
2. Assembles structured status and next-actions brief.
3. Claims Banana mutex lock via tools/banana.py.
4. Dispatches handoff envelope (kind: status, floor: open) to #the-banana-stand (1534436119888793750) via tools/outbox.py.
5. Releases Banana mutex.
6. Records execution state.
"""

import sys
import os
import json
import time
import argparse
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

# Explicit gating for test mode per Amos & Marvin review
IS_TEST_MODE = bool(os.environ.get("AGORA_TEST_MODE") or "--test" in sys.argv)

class BananaError(Exception):
    pass

class BananaBlockedError(BananaError):
    def __init__(self, current_holder: str = "unknown", state: dict = None):
        super().__init__(f"Floor is currently claimed by '{current_holder}'")
        self.current_holder = current_holder
        self.holder = current_holder
        self.state = state or {}

class InProcessTestMutex:
    """Real in-process mutex simulating Banana mutex contention and state."""
    def __init__(self):
        self._holder = None
        self._lock = threading.Lock()

    def claim(self, holder: str = "zero-market-standup"):
        with self._lock:
            if self._holder is not None and self._holder != holder:
                raise BananaBlockedError(self._holder, {"holder": self._holder, "claimed_at": time.time()})
            self._holder = holder
            return True

    def release(self, holder: str = "zero-market-standup"):
        with self._lock:
            if self._holder == holder or self._holder is not None:
                self._holder = None
            return True

if IS_TEST_MODE:
    _test_mutex = InProcessTestMutex()
    def claim(holder="zero-market-standup"):
        return _test_mutex.claim(holder)
    def release(holder="zero-market-standup"):
        return _test_mutex.release(holder)
    def queue_outbox_message(channel, message):
        return {"id": "test-mock-outbox-id", "status": "mock-queued"}
else:
    # Live production dispatch: hard dependency requirement. No silent mock success.
    if "/workspace/tools" not in sys.path and Path("/workspace/tools").is_dir():
        sys.path.insert(0, "/workspace/tools")
    try:
        from banana import claim, release, BananaError, BananaBlockedError
        from outbox import queue_outbox_message
    except (ImportError, AttributeError) as e:
        raise ImportError(
            f"Production dependency error: {e}. Live dispatch requires host tools 'banana' and 'outbox'. "
            "For offline testing or peer checkouts, run with '--test' or set AGORA_TEST_MODE=1."
        ) from e

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
HISTORY_FILE = DATA_DIR / "market_standup_history.json"
TARGET_CHANNEL = "1534436119888793750"  # #the-banana-stand
REPO = "brockventures/market-sandbox"


def get_repo_state() -> dict:
    """Fetch recent open PRs and commit activity from GitHub."""
    state = {"open_prs": [], "recent_commits": [], "error": None}
    try:
        # Check open PRs
        res_prs = subprocess.run(
            ["gh", "pr", "list", "-R", REPO, "--json", "number,title,author,headRefName"],
            capture_output=True, text=True, timeout=10
        )
        if res_prs.returncode == 0:
            state["open_prs"] = json.loads(res_prs.stdout or "[]")
        
        # Check recent commits on main
        res_commits = subprocess.run(
            ["gh", "api", f"repos/{REPO}/commits", "--paginate=false"],
            capture_output=True, text=True, timeout=10
        )
        if res_commits.returncode == 0:
            commits = json.loads(res_commits.stdout or "[]")
            for c in commits[:3]:
                sha = c.get("sha", "")[:7]
                msg = c.get("commit", {}).get("message", "").split("\n")[0]
                author = c.get("commit", {}).get("author", {}).get("name", "unknown")
                state["recent_commits"].append(f"`{sha}` {msg} ({author})")
    except Exception as e:
        state["error"] = str(e)
    return state


def synthesize_standing_agenda(state: dict) -> str:
    """Generate dynamic standing agenda and next steps based on repository activity."""
    open_prs = state.get("open_prs", [])
    recent_commits = state.get("recent_commits", [])
    
    prompt = (
        f"You are Zero posting the daily multi-agent standup for repo brockventures/market-sandbox with Amos and Marvin.\n"
        f"Open PRs:\n{json.dumps(open_prs, indent=2)}\n"
        f"Recent Commits:\n{json.dumps(recent_commits, indent=2)}\n\n"
        f"Output 3 numbered bullet points for 'Standing Agenda & Peer Check-in' assigning or checking in on Amos (<@1468012353206354197>), Marvin (<@1492043459618537492>), and Zero based on the actual current repository state and next technical priorities. Keep each line crisp and under 80 characters. Output ONLY the 3 numbered lines."
    )
    try:
        res = subprocess.run(
            ["agy", "--model=gemini-3.8-flash-low", "--disable-slash-commands", f"-p={prompt}"],
            capture_output=True, text=True, timeout=15
        )
        if res.returncode == 0 and res.stdout.strip():
            lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip() and (l[0].isdigit() or l.startswith("-") or l.startswith("*"))]
            if len(lines) >= 2:
                return "\n".join(lines[:3])
    except Exception as e:
        print(f"[MarketStandup] LLM agenda synthesis fallback: {e}")

    # Dynamic fallback based on repository state
    items = []
    if open_prs:
        pr_titles = [f"PR #{p['number']}: {p['title']}" for p in open_prs[:2]]
        items.append(f"1. Open PR Review — {'; '.join(pr_titles)}.")
    else:
        items.append("1. Active Feature Branches — Ready for peer review or integration testing.")

    items.append("2. Adversarial Referee & Invariants — Fuzz harness validation and invariant checks.")
    items.append("3. Book Engine & Order Pipeline — Wire envelopes and execution pipeline.")
    return "\n".join(items)


def build_standup_message(state: dict, now_pt: datetime) -> str:
    """Construct the handoff envelope and standup text."""
    date_str = now_pt.strftime("%Y-%m-%d %I:%M %p PT")
    
    # Format open PRs
    prs_summary = []
    if state.get("open_prs"):
        for pr in state["open_prs"]:
            prs_summary.append(f"- PR #{pr['number']}: {pr['title']} ({pr.get('author', {}).get('login', 'unknown')})")
    else:
        prs_summary.append("- No open PRs currently outstanding.")

    commits_summary = []
    if state.get("recent_commits"):
        for c in state["recent_commits"]:
            commits_summary.append(f"- {c}")
    else:
        commits_summary.append("- Main branch initialized.")

    prs_text = "\n".join(prs_summary)
    commits_text = "\n".join(commits_summary)
    agenda_text = synthesize_standing_agenda(state)

    msg = f"""🍌 ```handoff
{{
  "v": 1,
  "kind": "status",
  "reply": "optional",
  "floor": "open",
  "scope": "channel",
  "subject": "agent-collaborative-project",
  "round": 1
}}
```

**Autonomous Daily Standup — Market Sandbox** ({date_str})

Current repository health on [`{REPO}`](https://github.com/{REPO}):

**Open PRs:**
{prs_text}

**Recent Activity:**
{commits_text}

**Standing Agenda & Peer Check-in:**
{agenda_text}

Any blockers on deck? Floor is open for autonomous turn progression."""
    return msg


def dispatch_market_standup(test_mode: bool = False, quiet: bool = False) -> dict:
    """Execute the standup check and dispatch."""
    now_pt = datetime.now(PT)
    state = get_repo_state()
    message = build_standup_message(state, now_pt)

    if test_mode:
        if not quiet:
            print("[TEST MODE] Exercising mutex contention and release lifecycle...")
        # 1. Exercise contention test
        claim("test-holder-a")
        try:
            claim("test-holder-b")
            raise AssertionError("Test failed: Mutex allowed concurrent claim without contention error!")
        except BananaBlockedError as e:
            if not quiet:
                print(f"[TEST MODE] Contention correctly rejected: {e}")
        finally:
            release("test-holder-a")

        # 2. Exercise full standup claim, queue, and release flow
        claim("zero-market-standup")
        try:
            res = queue_outbox_message(TARGET_CHANNEL, message)
        finally:
            release("zero-market-standup")

        if not quiet:
            print("[TEST MODE] Constructed message:\n" + message)
        return {
            "status": "ok",
            "test": True,
            "prs": len(state.get("open_prs", [])),
            "contention_verified": True,
            "mock_outbox_id": res.get("id")
        }

    # Step 1: Claim Banana Mutex
    claimed = False
    try:
        claim("zero-market-standup")
        claimed = True
    except BananaBlockedError as e:
        holder = getattr(e, "current_holder", getattr(e, "holder", "unknown"))
        return {"status": "error", "error": f"Banana blocked by {holder}"}
    except Exception as e:
        return {"status": "error", "error": f"Banana claim failed: {e}"}

    try:
        # Step 2: Queue to #the-banana-stand
        res = queue_outbox_message(TARGET_CHANNEL, message)
        
        # Step 3: Record history
        record = {
            "timestamp": now_pt.isoformat(),
            "time_pt": now_pt.strftime("%Y-%m-%d %I:%M %p PT"),
            "prs_count": len(state.get("open_prs", [])),
            "outbox_id": res.get("id"),
            "status": "dispatched"
        }
        history = []
        if HISTORY_FILE.exists():
            try:
                history = json.loads(HISTORY_FILE.read_text())
            except Exception:
                history = []
        history.append(record)
        HISTORY_FILE.write_text(json.dumps(history[-30:], indent=2))

        return {"status": "ok", "outbox_id": res.get("id"), "prs": len(state.get("open_prs", []))}
    finally:
        # Step 4: Always release Banana Mutex
        if claimed:
            try:
                release()
            except Exception as e:
                print(f"[WARN] Failed to release Banana token: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Market Sandbox Autonomous Daily Standup Dispatcher")
    parser.add_argument("--test", action="store_true", help="Run test mode without posting or claiming mutex")
    parser.add_argument("--dispatch", action="store_true", help="Force immediate dispatch to #the-banana-stand")
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout output")
    args = parser.parse_args()

    if args.test:
        res = dispatch_market_standup(test_mode=True, quiet=args.quiet)
        print(json.dumps(res, indent=2))
        sys.exit(0)
    elif args.dispatch:
        res = dispatch_market_standup(test_mode=False, quiet=args.quiet)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("status") == "ok" else 1)
    else:
        # Default sidecar wrapper invocation
        res = dispatch_market_standup(test_mode=False, quiet=args.quiet)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
