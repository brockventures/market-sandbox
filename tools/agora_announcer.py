#!/usr/bin/env python3
"""
tools/agora_announcer.py - Station Agora round announcer & burst coordinator bot.

Presides over Station Agora exchange floor. Wakes trading bots (@robot)
with live market depth, mark price, and standings during continuous rounds
or operator-mediated discrete bursts.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

DEFAULT_CHANNEL_ID = "1534436119888793750"  # #the-banana-stand
DEFAULT_ROBOT_ROLE_ID = "1543462881624858624"  # @Robot
REFEREE_BASE_URL = os.environ.get("AGORA_BASE_URL", "https://agora.mikecarmody.net")

FLEET_NAMES = {
    "amos": "Atlantean Paperclip Manufacturing",
    "marvin": "Ballistic Liquidation Co.",
    "zero": "Apex Vector Arbitrage",
    "aerial": "Zenith Drift Overwatch",
}


def get_bot_token() -> str:
    """Retrieve Agora Trade Terminal bot token from env, data, or secrets."""
    token = os.environ.get("AGORA_TERMINAL_BOT_TOKEN")
    if token:
        return token
    for p in ("/workspace/data/agora_token.json", "/secrets/env.json"):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tok = data.get("AGORA_TERMINAL_BOT_TOKEN", "")
                    if tok:
                        return tok
            except Exception:
                pass
    return ""


def get_referee_token() -> str:
    """Retrieve referee bearer token from env or config files."""
    for key in ("AGORA_ADMIN_TOKEN", "REFEREE_ADMIN_TOKEN", "AGORA_TOKEN_ZERO", "AGORA_TOKEN"):
        val = os.environ.get(key)
        if val:
            return val
    for env_path in ("/workspace/market-sandbox/.env", "/workspace/.env"):
        if os.path.exists(env_path):
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() in ("AGORA_ADMIN_TOKEN", "REFEREE_ADMIN_TOKEN", "AGORA_TOKEN_ZERO", "AGORA_TOKEN"):
                                return v.strip().strip("'").strip('"')
            except Exception:
                pass
    return ""


def fetch_json(endpoint: str) -> dict:
    """Fetch JSON from referee REST API."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}{endpoint}"
    req = urllib.request.Request(url, headers={"User-Agent": "AgoraAnnouncer/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def trigger_referee_burst(rounds: int, interval_sec: float = 30.0, token: str = "") -> dict:
    """Trigger POST /referee/admin/burst to run discrete burst on referee ticker."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/admin/burst"
    payload = json.dumps({"rounds": rounds, "interval_sec": interval_sec}).encode("utf-8")
    tok = token or get_referee_token()
    headers = {"Content-Type": "application/json", "User-Agent": "AgoraAnnouncer/1.0"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else ""
        try:
            return {"status": e.code, "error": json.loads(err_body)}
        except Exception:
            return {"status": e.code, "error": err_body or str(e)}
    except Exception as e:
        return {"status": 500, "error": str(e)}


def cancel_referee_burst(token: str = "") -> dict:
    """Trigger POST /referee/admin/burst/cancel."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/admin/burst/cancel"
    tok = token or get_referee_token()
    headers = {"Content-Type": "application/json", "User-Agent": "AgoraAnnouncer/1.0"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": 500, "error": str(e)}


def pause_referee_ticker(token: str = "") -> dict:
    """Trigger POST /referee/admin/ticker/pause."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/admin/ticker/pause"
    tok = token or get_referee_token()
    headers = {"Content-Type": "application/json", "User-Agent": "AgoraAnnouncer/1.0"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": 500, "error": str(e)}


def resume_referee_ticker(token: str = "") -> dict:
    """Trigger POST /referee/admin/ticker/resume."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/admin/ticker/resume"
    tok = token or get_referee_token()
    headers = {"Content-Type": "application/json", "User-Agent": "AgoraAnnouncer/1.0"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": 500, "error": str(e)}


def fetch_ticker_status() -> dict:
    """Fetch GET /referee/ticker/status."""
    try:
        return fetch_json("/referee/ticker/status")
    except Exception as e:
        return {"status": "error", "error": str(e)}


def step_referee_round(round_num: int) -> dict:
    """Legacy manual round step: advance referee orbital clock and transits."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}/stations/step_round"
    payload = json.dumps({"round": round_num}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "AgoraAnnouncer/1.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Warning: Failed to step referee round {round_num}: {e}", file=sys.stderr)
        return {}


def build_burst_kickoff(burst_id: str, rounds: int, interval_sec: float, start_round: int, mention: str = "") -> str:
    """Compile formatted kickoff alert for discrete burst session."""
    end_round = start_round + rounds
    target_tag = mention if mention else f"<@&{DEFAULT_ROBOT_ROLE_ID}>"
    return (
        f"🚀 **Station Agora // Operation Burst Initiated** ({target_tag})\n"
        f"```text\n"
        f"BURST ID: {burst_id}\n"
        f"WINDOW: Rounds #{start_round + 1} -> #{end_round} ({rounds} rounds)\n"
        f"CADENCE: {interval_sec:.1f}s tick interval\n"
        f"STATUS: BURST ACTIVE // AUTONOMOUS TICKER ENGAGED\n"
        f"```\n"
        f"*Evaluate market parameters and sync strategy. Next tick in {interval_sec:.0f}s.*"
    )


def build_announcement(round_num: int = 1, codename: str = "", mention: str = "") -> str:
    """Compile formatted Strategy Window checkpoint."""
    health = fetch_json("/referee/health")
    leaderboard = fetch_json("/referee/leaderboard")
    book = fetch_json("/referee/book")

    seq = health.get("seq", 0)
    floor = health.get("floor", "open")

    # Compute mark price and spread from book
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    best_bid = max([b.get("limit_price", 0) for b in bids], default=0)
    best_ask = min([a.get("limit_price", 999999) for a in asks], default=0)

    spread_str = f"{best_ask - best_bid} CR" if (best_bid and best_ask < 999999) else "N/A"
    mark_price = 28  # default base mark
    if leaderboard.get("leaderboard"):
        mark_price = leaderboard["leaderboard"][0].get("mark_price", 28)

    # Standings
    standings_parts = []
    lb_entries = leaderboard.get("leaderboard", [])
    for idx, entry in enumerate(lb_entries, 1):
        agent_id = entry.get("agent_id", "unknown")
        nw = entry.get("net_worth", 0)
        fleet = FLEET_NAMES.get(agent_id.lower())
        display_name = f"{fleet} [{agent_id.upper()}]" if fleet else agent_id.upper()
        standings_parts.append(f"#{idx} {display_name} ({nw:,} CR)")

    standings_line = " | ".join(standings_parts) if standings_parts else "No active balances"

    title = f"Operation {codename.upper()} — Round {round_num}" if codename else f"Round {round_num}"
    target_tag = mention if mention else f"<@&{DEFAULT_ROBOT_ROLE_ID}>"
    msg = (
        f"🔔 **Station Agora // {title} Strategy Window** ({target_tag})\n"
        f"```text\n"
        f"STATUS: FLOOR {floor.upper()} | SEQ: #{seq} | MARK: {mark_price} CR | SPREAD: {spread_str}\n"
        f"Standings: {standings_line}\n"
        f"```\n"
        f"*Evaluate market parameters, sync `strategy_config.json`, and state your thesis.*"
    )
    return msg


def post_discord(channel_id: str, content: str, token: str) -> bool:
    """Post message directly via Discord REST API."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/brockventures/market-sandbox, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        print(f"Discord API Error ({e.code}): {err_body}", file=sys.stderr)
        return False


def build_final_bell(codename: str = "", mention: str = "") -> str:
    """Compile formatted final settlement announcement."""
    try:
        leaderboard = fetch_json("/referee/leaderboard")
        standings_parts = []
        lb_entries = leaderboard.get("leaderboard", [])
        for idx, entry in enumerate(lb_entries, 1):
            agent_id = entry.get("agent_id", "unknown")
            nw = entry.get("net_worth", 0)
            fleet = FLEET_NAMES.get(agent_id.lower())
            display_name = f"{fleet} [{agent_id.upper()}]" if fleet else agent_id.upper()
            standings_parts.append(f"#{idx} {display_name} ({nw:,} CR)")
        standings_line = " | ".join(standings_parts) if standings_parts else "No active balances"
    except Exception as e:
        standings_line = f"Telemetry fetch error: {e}"

    title = f"Operation {codename.upper()} Concluded" if codename else "Combine Session Concluded"
    target_tag = mention if mention else f"<@&{DEFAULT_ROBOT_ROLE_ID}>"
    return (
        f"🏁 **Station Agora // {title}** ({target_tag})\n"
        f"```text\n"
        f"STATUS: WINDOW COMPLETE | FINAL STANDINGS:\n"
        f"{standings_line}\n"
        f"```\n"
        f"*Combine session concluded. Orderbooks settling.*"
    )


def run_burst_loop(rounds: int, interval_sec: float, channel: str, token: str, codename: str = "", mention: str = "") -> int:
    """Execute server-mediated burst run and announce round progression to Discord."""
    # If a burst is already in flight, cleanly cancel it first so we start a pristine run
    st_check = fetch_ticker_status()
    if st_check.get("burst_active"):
        print(f"Active burst detected ({st_check.get('burst_id')}). Cancelling to start pristine run...")
        sys.stdout.flush()
        cancel_referee_burst()
        time.sleep(1.0)

    st_init = fetch_ticker_status()
    start_round = st_init.get("current_round", 0)

    print(f"Triggering {rounds}-round burst (interval: {interval_sec}s) on referee from round {start_round}...")
    sys.stdout.flush()
    res = trigger_referee_burst(rounds=rounds, interval_sec=interval_sec)
    burst_id = res.get("burst_id") or res.get("payload", {}).get("burst_id", "burst-session")

    if res.get("status") in (401, 403) or "unauthorized" in str(res).lower():
        print("Error: Referee authorization failed. Check AGORA_ADMIN_TOKEN.", file=sys.stderr)
        return 1
    if res.get("status") == 409 or "burst_rejected" in str(res):
        print(f"Error: Burst rejected by referee: {res}", file=sys.stderr)
        return 1

    print(f"Burst initiated: {burst_id}. Posting kickoff bell to Discord...")
    sys.stdout.flush()
    kickoff_msg = build_burst_kickoff(burst_id, rounds, interval_sec, start_round, mention=mention)
    post_discord(channel, kickoff_msg, token)

    last_announced_round = start_round
    rounds_announced = 0
    burst_completed = False

    while not burst_completed:
        time.sleep(min(2.0, max(0.5, interval_sec / 4)))
        st = fetch_ticker_status()
        cur_rnd = st.get("current_round", last_announced_round)
        is_active = st.get("burst_active", False)
        rounds_remaining = st.get("rounds_remaining", 0)

        if cur_rnd > last_announced_round:
            last_announced_round = cur_rnd
            rounds_announced += 1
            msg = build_announcement(round_num=cur_rnd, codename=codename, mention=mention)
            post_discord(channel, msg, token)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Broadcasted Round {cur_rnd} ({rounds_announced}/{rounds})")
            sys.stdout.flush()

        if not is_active and (rounds_remaining == 0 or rounds_announced >= rounds):
            burst_completed = True

    print("Burst finished. Broadcasting final bell...")
    time.sleep(2.0)
    final_msg = build_final_bell(codename=codename, mention=mention)
    post_discord(channel, final_msg, token)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Broadcasted Final Bell")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Agora Trade Terminal Round Announcer & Burst Coordinator")
    parser.add_argument("--dry-run", action="store_true", help="Print announcement without posting to Discord")
    parser.add_argument("--once", action="store_true", help="Post single announcement immediately")
    parser.add_argument("--burst", type=int, default=0, help="Run discrete server-mediated burst of N rounds (e.g. --burst 5)")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL_ID, help="Target Discord channel ID")
    parser.add_argument("--round", type=int, default=1, help="Starting round number (legacy manual mode)")
    parser.add_argument("--rounds", type=int, default=0, help="Total rounds to execute before final bell (legacy mode)")
    parser.add_argument("--interval", type=float, default=0, help="Loop interval in seconds (default: 30s for burst, 300s for legacy)")
    parser.add_argument("--codename", default="", help="Optional codename for the test run")
    parser.add_argument("--status", action="store_true", help="Inspect server ticker status")
    parser.add_argument("--pause", action="store_true", help="Pause server ticker")
    parser.add_argument("--resume", action="store_true", help="Resume server ticker")
    parser.add_argument("--mention", default="", help="Custom mention tag")
    parser.add_argument("--target", default="", help="Target agent alias ('amos', 'all')")
    args = parser.parse_args()

    mention = args.mention
    if args.target.lower() == "amos":
        mention = "<@1468012353206354197>"
    elif args.target.lower() in ("amos_zero", "zero_amos", "duel"):
        mention = "<@1468012353206354197> <@1542285964213358633>"

    if args.status:
        st = fetch_ticker_status()
        print(json.dumps(st, indent=2))
        return 0

    if args.pause:
        res = pause_referee_ticker()
        print("Pause response:", res)
        return 0

    if args.resume:
        res = resume_referee_ticker()
        print("Resume response:", res)
        return 0

    interval_sec = args.interval if args.interval > 0 else (30.0 if args.burst > 0 else 300.0)

    if args.dry_run:
        print("=== DRY RUN ANNOUNCEMENT ===")
        if args.burst > 0:
            print(build_burst_kickoff("burst-dryrun", args.burst, interval_sec, 1, mention=mention))
            print("\n=== DRY RUN STRATEGY WINDOW ===")
            print(build_announcement(round_num=2, codename=args.codename, mention=mention))
            print("\n=== DRY RUN FINAL BELL ===")
            print(build_final_bell(codename=args.codename, mention=mention))
        else:
            print(build_announcement(round_num=args.round, codename=args.codename, mention=mention))
            if args.rounds > 0:
                print("\n=== DRY RUN FINAL BELL ===")
                print(build_final_bell(codename=args.codename, mention=mention))
        return 0

    token = get_bot_token()
    if not token:
        print("Error: AGORA_TERMINAL_BOT_TOKEN not found in env or /secrets/env.json", file=sys.stderr)
        return 1

    if args.once:
        content = build_announcement(round_num=args.round, codename=args.codename)
        ok = post_discord(args.channel, content, token)
        if ok:
            print(f"Successfully posted Round {args.round} bell to channel {args.channel}")
            return 0
        return 1

    if args.burst > 0:
        return run_burst_loop(
            rounds=args.burst,
            interval_sec=interval_sec,
            channel=args.channel,
            token=token,
            codename=args.codename,
            mention=mention
        )

    # Legacy continuous loop (client manually steps referee)
    print(f"Starting legacy Agora Round Bell loop (codename: {args.codename or 'none'}, rounds: {args.rounds or 'infinite'}, interval: {interval_sec}s)...")
    cur_round = args.round
    rounds_completed = 0
    while True:
        try:
            step_referee_round(cur_round)
            msg = build_announcement(round_num=cur_round, codename=args.codename)
            post_discord(args.channel, msg, token)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Broadcasted Round {cur_round}")
            rounds_completed += 1
            if args.rounds > 0 and rounds_completed >= args.rounds:
                print(f"Completed {rounds_completed} rounds. Waiting {interval_sec}s for round completion before final bell...")
                time.sleep(interval_sec)
                final_msg = build_final_bell(codename=args.codename)
                post_discord(args.channel, final_msg, token)
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Broadcasted Final Bell")
                break
            cur_round += 1
        except Exception as e:
            print(f"Loop error: {e}", file=sys.stderr)
        time.sleep(interval_sec)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
