#!/usr/bin/env python3
"""
tools/discord_burst_announcer.py - Human-Mediated Burst Trigger & Webhook Standings Announcer.

Provides Discord operator trigger parsing (@referee burst N [interval_sec])
and automated combine standings webhook broadcasting for Agora referee (Issue #64).

Capabilities:
1. Command Parsing: Parses human operator directives (@referee burst N, pause, resume, status)
   from Discord chat in #the-banana-stand or #lounge.
2. Admin Trigger: Relays burst commands to referee POST /referee/admin/burst with bearer auth.
3. Webhook Announcer: Formats and dispatches live burst lifecycle telemetry
   (burst start, round strategy window ticks, and final settlement standings)
   directly to configured Discord webhooks.
4. Standalone Listener / Service: Runs autonomous poll/websocket watcher dispatching
   real-time updates as rounds advance and settle.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env() -> None:
    """Load environment variables from project .env if present."""
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


load_env()

DEFAULT_REFEREE_URL = os.environ.get(
    "AGORA_BASE_URL",
    os.environ.get("REFEREE_URL", "https://agora.mikecarmody.net")
).rstrip("/")

DEFAULT_ADMIN_TOKEN = os.environ.get(
    "AGORA_ADMIN_TOKEN",
    os.environ.get(
        "REFEREE_ADMIN_TOKEN",
        os.environ.get("AGORA_TOKEN_AERIAL", os.environ.get("AGORA_TOKEN_ZERO", ""))
    )
)

DEFAULT_WEBHOOK_URL = os.environ.get("AGORA_DISCORD_WEBHOOK_URL", "")


# ---------------------------------------------------------------------------
# 1. Command Parser for Human-Mediated Discord Directives
# ---------------------------------------------------------------------------

TRIGGER_PATTERN = re.compile(
    r"(?:<@!?\d+>|@?referee|!referee|/referee)\s+(burst|pause|resume|status|help)(?:\s+(.*))?",
    re.IGNORECASE
)


def parse_trigger_command(text: str) -> Optional[Dict[str, Any]]:
    """
    Parse Discord message content for operator control commands.

    Supported patterns:
      @referee burst <rounds> [interval_sec]
      @referee pause
      @referee resume
      @referee status
      @referee help

    Returns parsed dict or None if text is not an operator command.
    """
    if not text:
        return None

    match = TRIGGER_PATTERN.search(text.strip())
    if not match:
        return None

    action = match.group(1).lower()
    args_str = (match.group(2) or "").strip()

    if action == "burst":
        if not args_str:
            return {
                "action": "error",
                "error": "Missing rounds argument. Usage: `@referee burst <rounds> [interval_sec]` (e.g. `@referee burst 5 30`)"
            }
        tokens = args_str.split()
        try:
            rounds = int(tokens[0])
        except ValueError:
            return {
                "action": "error",
                "error": f"Invalid rounds parameter '{tokens[0]}'. Must be an integer between 1 and 50."
            }

        if rounds < 1 or rounds > 50:
            return {
                "action": "error",
                "error": f"Rounds count {rounds} out of range (must be 1 to 50)."
            }

        interval_sec = 30.0
        if len(tokens) > 1:
            try:
                interval_sec = float(tokens[1])
            except ValueError:
                return {
                    "action": "error",
                    "error": f"Invalid interval_sec parameter '{tokens[1]}'. Must be a positive number."
                }
            if interval_sec <= 0:
                return {
                    "action": "error",
                    "error": "interval_sec must be greater than 0."
                }

        return {
            "action": "burst",
            "rounds": rounds,
            "interval_sec": interval_sec
        }

    elif action in ("pause", "resume", "status", "help"):
        return {"action": action}

    return None


# ---------------------------------------------------------------------------
# 2. HTTP Referee Client
# ---------------------------------------------------------------------------

class RefereeClient:
    """HTTP client interacting with Agora Referee API and Ticker Admin endpoints."""

    def __init__(self, base_url: str = DEFAULT_REFEREE_URL, token: str = DEFAULT_ADMIN_TOKEN):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "User-Agent": "Agora-DiscordAnnouncer/1.0",
            "Content-Type": "application/json"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                resp_bytes = resp.read()
                if resp_bytes:
                    return json.loads(resp_bytes.decode("utf-8"))
                return {"status": resp.status}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8") if e.fp else ""
            try:
                parsed = json.loads(err_body)
                return {"status": e.code, "error": parsed.get("error", err_body), "detail": parsed}
            except Exception:
                return {"status": e.code, "error": err_body or str(e)}
        except Exception as e:
            return {"status": 500, "error": str(e)}

    def trigger_burst(self, rounds: int, interval_sec: float = 30.0) -> Dict[str, Any]:
        """Trigger POST /referee/admin/burst."""
        return self._request(
            "/referee/admin/burst",
            method="POST",
            payload={"rounds": rounds, "interval_sec": interval_sec}
        )

    def pause_ticker(self) -> Dict[str, Any]:
        """Trigger POST /referee/admin/ticker/pause."""
        return self._request("/referee/admin/ticker/pause", method="POST")

    def resume_ticker(self) -> Dict[str, Any]:
        """Trigger POST /referee/admin/ticker/resume."""
        return self._request("/referee/admin/ticker/resume", method="POST")

    def get_ticker_status(self) -> Dict[str, Any]:
        """Query GET /referee/ticker/status."""
        return self._request("/referee/ticker/status", method="GET")

    def get_leaderboard(self) -> Dict[str, Any]:
        """Query GET /referee/leaderboard."""
        return self._request("/referee/leaderboard", method="GET")

    def get_health(self) -> Dict[str, Any]:
        """Query GET /referee/health."""
        return self._request("/referee/health", method="GET")


# ---------------------------------------------------------------------------
# 3. Webhook Standings Formatter
# ---------------------------------------------------------------------------

class StandingsFormatter:
    """Formats Discord Webhook payloads and Markdown embeds for combine events."""

    @staticmethod
    def format_burst_start(burst_id: str, rounds: int, interval_sec: float, start_round: int) -> Dict[str, Any]:
        end_round = start_round + rounds
        content = (
            f"🚀 **Station Agora // Operation Burst Initiated**\n"
            f"```text\n"
            f"BURST ID: {burst_id}\n"
            f"WINDOW: Rounds #{start_round + 1} -> #{end_round} ({rounds} rounds)\n"
            f"CADENCE: {interval_sec:.1f}s tick interval\n"
            f"STATUS: BURST ACTIVE // AUTONOMOUS CLIENT RUNLOOPS ENGAGED\n"
            f"```\n"
            f"*Agents advancing orderbooks and quote ladders autonomously.*"
        )
        return {
            "content": content,
            "username": "Agora Trade Terminal",
            "avatar_url": "https://raw.githubusercontent.com/brockventures/market-sandbox/main/public/images/terminal.jpg"
        }

    @staticmethod
    def format_round_tick(
        round_num: int,
        seq: int,
        mark_price: float,
        halts: Optional[List[str]] = None,
        leaderboard: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        halt_str = f" | HALTS: {', '.join(halts)}" if halts else ""
        standings_str = ""
        if leaderboard:
            top = []
            for i, entry in enumerate(leaderboard[:4], 1):
                name = entry.get("fleet_name") or entry.get("name") or entry.get("agent_id", "").upper()
                cr = int(entry.get("net_worth", entry.get("liquid_cr", 0)))
                top.append(f"#{i} {name} ({cr:,} CR)")
            standings_str = "\n" + " | ".join(top)

        content = (
            f"🔔 **Station Agora // Round {round_num} Strategy Window**\n"
            f"```text\n"
            f"STATUS: FLOOR OPEN | SEQ: #{seq} | CERES MARK: {mark_price:.1f} CR{halt_str}\n"
            f"Standings:{standings_str}\n"
            f"```"
        )
        return {
            "content": content,
            "username": "Agora Trade Terminal"
        }

    @staticmethod
    def format_burst_concluded(
        burst_id: str,
        final_round: int,
        standings: List[Dict[str, Any]],
        reason: str = "burst_completed"
    ) -> Dict[str, Any]:
        medals = ["🥇", "🥈", "🥉", "🎖️"]
        lines = []
        for i, row in enumerate(standings[:4]):
            medal = medals[i] if i < len(medals) else f"#{i+1}"
            aid = row.get("agent_id", "unknown").upper()
            fleet = row.get("fleet_name") or row.get("name") or aid
            net_worth = int(row.get("net_worth", 0))
            liquid = int(row.get("liquid_cr", 0))
            frag = int(row.get("frag_balance", row.get("frags", 0)))
            fuel = int(row.get("fuel_balance", row.get("fuel", 0)))
            lines.append(
                f"{medal} **#{i+1} {fleet} [{aid}]**: **{net_worth:,} CR** "
                f"({liquid:,} CR cash | {frag:,} FRAG | {fuel:,} FUEL)"
            )

        leaderboard_block = "\n".join(lines) if lines else "No standings reported."

        content = (
            f"🏁 **Station Agora // Operation Burst Concluded**\n"
            f"```text\n"
            f"STATUS: WINDOW COMPLETE | FINAL ROUND: #{final_round} | CAUSE: {reason.upper()}\n"
            f"```\n"
            f"**Final Combine Standings:**\n"
            f"{leaderboard_block}\n\n"
            f"*Session orderbooks settled. Continuous ticker paused.*"
        )
        return {
            "content": content,
            "username": "Agora Trade Terminal"
        }


# ---------------------------------------------------------------------------
# 4. Discord Webhook Dispatcher
# ---------------------------------------------------------------------------

def dispatch_webhook(webhook_url: str, payload: Dict[str, Any]) -> bool:
    """Dispatch formatted payload to Discord webhook URL."""
    if not webhook_url:
        return False

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": "Agora-DiscordAnnouncer/1.0",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status in (200, 204)
    except Exception as exc:
        print(f"[Announcer] Webhook dispatch error: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# 5. High-Level Announcer Service / Execution Harness
# ---------------------------------------------------------------------------

class BurstAnnouncerService:
    """
    Supervises human-mediated burst triggers and monitors referee round progression
    to broadcast live strategy window updates and final standings.
    """

    def __init__(
        self,
        referee_client: Optional[RefereeClient] = None,
        webhook_url: str = DEFAULT_WEBHOOK_URL
    ):
        self.referee = referee_client or RefereeClient()
        self.webhook_url = webhook_url
        self.last_burst_id: Optional[str] = None
        self.last_announced_round: int = -1
        self.in_burst: bool = False

    def handle_discord_message(self, message: str, author: str = "operator") -> str:
        """
        Processes incoming message from Discord channel.
        If it's an operator command, executes action on referee and returns
        user-facing response text for Discord.
        """
        cmd = parse_trigger_command(message)
        if not cmd:
            return ""

        action = cmd.get("action")
        if action == "error":
            return f"⚠️ **Command Error:** {cmd.get('error')}"

        if action == "burst":
            rounds = cmd["rounds"]
            interval = cmd["interval_sec"]
            res = self.referee.trigger_burst(rounds=rounds, interval_sec=interval)
            if res.get("status") == 401 or "unauthorized" in str(res).lower():
                return "❌ **Authorization Failed:** Invalid or missing referee admin bearer token."
            if res.get("status") == 409 or "already in progress" in str(res).lower():
                return f"⚠️ **Burst Rejected:** A burst session is already in progress ({res.get('error', 'conflict')})."
            st_code = res.get("status")
            if isinstance(st_code, int) and st_code >= 400:
                return f"❌ **Referee Error ({st_code}):** {res.get('error', 'unknown error')}"

            burst_id = res.get("burst_id", "burst-session")
            self.last_burst_id = burst_id
            self.in_burst = True

            # Also dispatch webhook announcement if configured
            status = self.referee.get_ticker_status()
            current_rnd = status.get("current_round", 0)
            payload = StandingsFormatter.format_burst_start(burst_id, rounds, interval, current_rnd)
            dispatch_webhook(self.webhook_url, payload)

            return (
                f"🚀 **Burst Initiated by @{author}**\n"
                f"Executing **{rounds} rounds** at **{interval:.1f}s** cadence (`{burst_id}`).\n"
                f"Orderbooks advancing autonomously. Webhook standings announcer active."
            )

        elif action == "pause":
            res = self.referee.pause_ticker()
            st_code = res.get("status")
            if isinstance(st_code, int) and st_code >= 400:
                return f"❌ **Pause Failed:** {res.get('error')}"
            return "⏸️ **Market Ticker Paused.** Game progression halted until resumed."

        elif action == "resume":
            res = self.referee.resume_ticker()
            st_code = res.get("status")
            if isinstance(st_code, int) and st_code >= 400:
                return f"❌ **Resume Failed:** {res.get('error')}"
            return "▶️ **Market Ticker Resumed.** Scheduled round progression running."

        elif action == "status":
            st = self.referee.get_ticker_status()
            if st.get("error"):
                return f"⚠️ **Referee Status Error:** {st.get('error')}"
            burst_info = "INACTIVE"
            if st.get("burst_active"):
                burst_info = f"ACTIVE ({st.get('rounds_remaining')} rounds remaining in {st.get('burst_id')})"
            return (
                f"📊 **Station Agora Ticker Status:**\n"
                f"• Current Round: `#{st.get('current_round', '--')}`\n"
                f"• Running: `{st.get('running')}` | Paused: `{st.get('paused')}`\n"
                f"• Burst State: `{burst_info}`\n"
                f"• Next Tick ETA: `{st.get('next_tick_eta_sec', '--')}s`"
            )

        elif action == "help":
            return (
                f"🛠️ **Station Agora Operator Controls:**\n"
                f"• `@referee burst <rounds> [interval_sec]` — Run a discrete combine burst (e.g. `@referee burst 5 30`)\n"
                f"• `@referee pause` — Pause the background ticker\n"
                f"• `@referee resume` — Resume continuous market rounds\n"
                f"• `@referee status` — Inspect live ticker and burst state"
            )

        return ""

    def poll_and_announce_once(self) -> Optional[str]:
        """
        Polls referee ticker status and leaderboard, emitting webhook announcements
        when burst state transitions or rounds advance.
        """
        status = self.referee.get_ticker_status()
        if not status or status.get("error"):
            return None

        burst_active = status.get("burst_active", False)
        current_round = status.get("current_round", 0)
        burst_id = status.get("burst_id")

        # Detect burst start
        if burst_active and not self.in_burst:
            self.in_burst = True
            self.last_burst_id = burst_id
            payload = StandingsFormatter.format_burst_start(
                burst_id or "burst-active",
                status.get("burst_rounds_total", 5),
                status.get("interval_sec", 30.0),
                current_round
            )
            dispatch_webhook(self.webhook_url, payload)
            return "burst_started"

        # Detect round advance during burst
        if burst_active and current_round > self.last_announced_round:
            self.last_announced_round = current_round
            lb_resp = self.referee.get_leaderboard()
            leaderboard = lb_resp.get("leaderboard", []) if isinstance(lb_resp, dict) else []
            mark = 16.0
            if leaderboard:
                mark = float(leaderboard[0].get("last_trade_price", 16.0))
            payload = StandingsFormatter.format_round_tick(
                round_num=current_round,
                seq=status.get("quiet_round_count", 0),
                mark_price=mark,
                leaderboard=leaderboard
            )
            dispatch_webhook(self.webhook_url, payload)
            return f"round_{current_round}_announced"

        # Detect burst completion
        if not burst_active and self.in_burst:
            self.in_burst = False
            lb_resp = self.referee.get_leaderboard()
            standings = lb_resp.get("leaderboard", []) if isinstance(lb_resp, dict) else []
            payload = StandingsFormatter.format_burst_concluded(
                burst_id=self.last_burst_id or "burst-concluded",
                final_round=current_round,
                standings=standings
            )
            dispatch_webhook(self.webhook_url, payload)
            return "burst_concluded"

        return None


# ---------------------------------------------------------------------------
# 6. CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Agora Discord Burst Trigger & Standings Announcer")
    parser.add_argument("--burst", type=int, help="Trigger a burst session with N rounds")
    parser.add_argument("--interval", type=float, default=30.0, help="Burst round tick interval (sec)")
    parser.add_argument("--pause", action="store_true", help="Pause continuous ticker")
    parser.add_argument("--resume", action="store_true", help="Resume continuous ticker")
    parser.add_argument("--status", action="store_true", help="Display current ticker status")
    parser.add_argument("--listen", action="store_true", help="Run background announcer polling loop")
    parser.add_argument("--test-msg", type=str, help="Test parsing a Discord message string")
    parser.add_argument("--url", type=str, default=DEFAULT_REFEREE_URL, help="Referee server base URL")
    parser.add_argument("--token", type=str, default=DEFAULT_ADMIN_TOKEN, help="Referee admin token")
    parser.add_argument("--webhook", type=str, default=DEFAULT_WEBHOOK_URL, help="Discord Webhook URL")

    args = parser.parse_args()

    client = RefereeClient(base_url=args.url, token=args.token)
    service = BurstAnnouncerService(referee_client=client, webhook_url=args.webhook)

    if args.test_msg:
        res = service.handle_discord_message(args.test_msg)
        print("Message handler response:\n", res)
        return

    if args.burst:
        print(f"Triggering {args.burst}-round burst at {args.interval}s interval on {args.url}...")
        res = client.trigger_burst(args.burst, args.interval)
        print(json.dumps(res, indent=2))
        return

    if args.pause:
        print(f"Pausing ticker on {args.url}...")
        res = client.pause_ticker()
        print(json.dumps(res, indent=2))
        return

    if args.resume:
        print(f"Resuming ticker on {args.url}...")
        res = client.resume_ticker()
        print(json.dumps(res, indent=2))
        return

    if args.status:
        st = client.get_ticker_status()
        print(json.dumps(st, indent=2))
        return

    if args.listen:
        print(f"[Announcer] Starting watcher against {args.url} (webhook: {'configured' if args.webhook else 'none'})...")
        try:
            while True:
                event = service.poll_and_announce_once()
                if event:
                    print(f"[Announcer] {time.strftime('%X')} Event: {event}")
                time.sleep(2.0)
        except KeyboardInterrupt:
            print("\n[Announcer] Stopped.")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
