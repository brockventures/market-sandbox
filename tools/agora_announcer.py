#!/usr/bin/env python3
"""
agora_announcer.py - Station Agora Trade Terminal Announcer & In-Channel Order Router.

Features:
1. Rich Thematic Sector Briefings: Station rotation, Expanse lore, live depot quotes.
2. Complete Holdings Telemetry: CR, FRAG, FUEL, FOOD, ORE, and MTM Net Worth.
3. Zero-Prep Instructions: Clear chat format + 1-line curl with universal combine token.
4. Active In-Channel Trade Listener: Intercepts natural language orders in Discord,
   executes them atomically against the referee, adds reactions (🚀/✅/❌), and emits
   instant execution receipts.
"""

import os
import sys
import time
import json
import re
import uuid
import argparse
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, Any, Optional, Set, Tuple

DEFAULT_CHANNEL_ID = "1534436119888793750"  # #the-banana-stand
# Load environment variables early so AGORA_BASE_URL and tokens are populated
for env_path in ("/workspace/market-sandbox/.env", "/workspace/.env"):
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for eline in f:
                    eline = eline.strip()
                    if eline and not eline.startswith("#") and "=" in eline:
                        k, v = eline.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
        except Exception:
            pass

DEFAULT_ROBOT_ROLE_ID = "1543462881624858624"  # @Robot
DEFAULT_TEAM_ROLE_ID = "1543462881624858624"   # @Robot
DEFAULT_TARGET_TAG = f"<@&{DEFAULT_ROBOT_ROLE_ID}>"

REFEREE_BASE_URL = os.environ.get("AGORA_BASE_URL", "https://agora-banana-production.up.railway.app")

STATION_ROTATION = ["ceres", "mars", "earth", "luna"]

STATION_PROFILES = {
    "ceres": {
        "name": "PIAZZI DEPOT (Ceres Belt Gate)",
        "emoji": "🪐",
        "intel": "Aeroponic nutrient failure struck Ceres Sub-Ring 4. Station Commissariat has issued urgent food requisitions at elevated spot prices while dumping raw ORE and salvage FRAG.",
        "opp": "Piazzi Depot pays premium CR for FOOD; sells ORE and FRAG cheap."
    },
    "mars": {
        "name": "ARCADIA FOUNDRIES (Martian Orbital Yards)",
        "emoji": "🔴",
        "intel": "Martian orbital shipyards announced emergency structural procurement contracts. Unrefined hull fragments and cryogenic propellant fuel trading at premium spot valuations.",
        "opp": "Arcadia Foundries paying top CR for FUEL and ORE."
    },
    "earth": {
        "name": "KENNEDY ORBITAL ELEVATOR (High Earth Terminal)",
        "emoji": "🌍",
        "intel": "Midwest hydroponic mega-farms report massive harvest surplus. Earth orbital depots flooded with fresh food, while terrestrial clean tech mandates bid aggressively for imported raw asteroid ORE.",
        "opp": "Kennedy Elevator sells FOOD cheap; pays high prices for ORE and FRAG."
    },
    "luna": {
        "name": "SHACKLETON COLD TRAP (Lunar Polar Port)",
        "emoji": "🌕",
        "intel": "High-velocity micrometeorite shower pelted Lunar south pole, scattering salvageable composite fragments across Shackleton Crater while southern polar He-3 collectors maintain steady refuel operations.",
        "opp": "Deep resting liquidity and tight spreads across all orderbooks. Ideal for rapid market-making."
    }
}

FLEET_NAMES = {
    "amos": "Atlantean Paperclip Manufacturing [APM]",
    "marvin": "Ballistic Liquidation Co. [BLC]",
    "zero": "Apex Vector Arbitrage [AVA]",
    "aerial": "Zenith Drift Overwatch [ZDO]"
}

TRADE_PATTERN = re.compile(
    r"\b(BUY|BID|SELL|ASK)\s+(\d+)\s+(FRAG|FUEL|FOOD|ORE|BANANA)\b(?:[^\d]*?(\d+))?(?:.*?\b(?:AT|IN|STATION)\s+([A-Za-z]+))?",
    re.IGNORECASE
)

TRANSIT_PATTERN = re.compile(
    r"\b(?:MOVE|TRANSIT|FLY|WARP|GO)\s+(?:TO\s+)?([A-Za-z]+)(?:\s+(?:WITH|CARRYING|LOAD)\s+(\d+)\s+([A-Za-z]+))?",
    re.IGNORECASE
)

AUTHOR_MAP = {
    "1541205716948353074": "amos",   # Amos / Ivy
    "1542081375287640084": "zero",   # Zero
    "179407724335988736": "zero",    # Ryan Brock
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
    for key in ("AGORA_COMBINE_TOKEN", "AGORA_ADMIN_TOKEN", "REFEREE_ADMIN_TOKEN", "AGORA_TOKEN_ZERO", "AGORA_TOKEN"):
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
                            if k.strip() in ("AGORA_COMBINE_TOKEN", "AGORA_ADMIN_TOKEN", "REFEREE_ADMIN_TOKEN", "AGORA_TOKEN_ZERO", "AGORA_TOKEN"):
                                return v.strip().strip("'").strip('"')
            except Exception:
                pass
    return "agora-combine-2026"


def fetch_json(endpoint: str, retries: int = 3, backoff_sec: float = 1.0) -> dict:
    """Fetch JSON from referee API with retries for transient blips."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}{endpoint}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AgoraAnnouncer/2.0", "Accept": "application/json"}
    )
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff_sec * (attempt + 1))
    return {"status": "error", "error": str(last_err)}


def trigger_referee_burst(rounds: int = 8, interval_sec: float = 180.0) -> dict:
    """Call POST /referee/admin/burst to arm discrete server-side ticker."""
    token = get_referee_token()
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/admin/burst"
    payload = json.dumps({"rounds": rounds, "interval_sec": interval_sec}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "AgoraAnnouncer/2.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return {"status": e.code, "error": json.loads(raw)}
        except Exception:
            return {"status": e.code, "raw_error": raw}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def cancel_referee_burst() -> dict:
    """Call POST /referee/admin/burst/cancel to halt active burst cleanly."""
    token = get_referee_token()
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/admin/burst/cancel"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "AgoraAnnouncer/2.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "error": str(e)}


def pause_referee_ticker() -> dict:
    """Pause ticker engine on referee."""
    token = get_referee_token()
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/admin/ticker/pause"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "error": str(e)}


def resume_referee_ticker() -> dict:
    """Resume ticker engine on referee."""
    token = get_referee_token()
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/admin/ticker/resume"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_ticker_status() -> dict:
    """Fetch current ticker state."""
    return fetch_json("/referee/ticker/status")


def post_discord(channel_id: str, content: str, token: str) -> Optional[dict]:
    """Post message directly via Discord REST API and return response dict."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    chunks = []
    if len(content) <= 1950:
        chunks = [content]
    else:
        current = []
        curr_len = 0
        for block in content.split("\n\n"):
            if curr_len + len(block) + 2 > 1950:
                if current:
                    chunks.append("\n\n".join(current))
                    current = [block]
                    curr_len = len(block)
                else:
                    chunks.append(block[:1950])
            else:
                current.append(block)
                curr_len += len(block) + 2
        if current:
            chunks.append("\n\n".join(current))

    last_resp = None
    for chunk in chunks:
        payload = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/brockventures/market-sandbox, 2.0)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                last_resp = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            print(f"Discord API Error ({e.code}): {err_body}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"Discord Post Error: {e}", file=sys.stderr)
            return None
    return last_resp


def add_discord_reaction(channel_id: str, message_id: str, emoji: str, token: str) -> bool:
    """Add emoji reaction to a Discord message."""
    encoded_emoji = urllib.parse.quote(emoji)
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{encoded_emoji}/@me"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/brockventures/market-sandbox, 2.0)",
        },
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        return False


def fetch_discord_messages(channel_id: str, after_id: str, token: str, limit: int = 20) -> list:
    """Fetch messages in channel after after_id."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
    if after_id:
        url += f"&after={after_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/brockventures/market-sandbox, 2.0)",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []


def submit_transit_to_referee(transit: dict, ref_token: str) -> dict:
    """Submit interplanetary transit to referee /stations/transit."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}/stations/transit"
    payload = json.dumps(transit).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {ref_token}",
            "Content-Type": "application/json",
            "User-Agent": "AgoraTradeTerminal/2.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return {"status": "error", "http_code": e.code, "error": json.loads(raw)}
        except Exception:
            return {"status": "error", "http_code": e.code, "raw_error": raw}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def parse_discord_transit(content: str, author_id: str, author_name: str) -> Optional[dict]:
    """Parse natural language transit command from Discord chat."""
    if TRADE_PATTERN.search(content) and not re.search(r"\b(?:MOVE|TRANSIT)\b", content, re.I):
        return None
    m = TRANSIT_PATTERN.search(content)
    if not m:
        return None
    dest_raw, qty_raw, comm_raw = m.groups()
    dest = dest_raw.lower().strip()
    if dest not in STATION_PROFILES and dest not in ("earth", "luna", "mars", "ceres"):
        return None

    qty = int(qty_raw) if qty_raw else 0
    comm = comm_raw.upper().strip() if comm_raw else "FRAG"
    if comm == "BANANA":
        comm = "FRAG"

    agent = None
    agent_override = re.search(r"\b(?:as|agent:?)\s+(amos|marvin|zero|aerial)\b", content, re.I)
    if agent_override:
        agent = agent_override.group(1).lower()
    elif author_id in AUTHOR_MAP:
        agent = AUTHOR_MAP[author_id]
    else:
        name_lower = author_name.lower()
        if "amos" in name_lower or "carmody" in name_lower or "mike" in name_lower:
            agent = "amos"
        elif "marvin" in name_lower or "alex" in name_lower:
            agent = "marvin"
        elif "zero" in name_lower or "brock" in name_lower or "ryan" in name_lower:
            agent = "zero"
        elif "aerial" in name_lower:
            agent = "aerial"

    if not agent:
        agent = "zero"

    return {
        "agent_id": agent,
        "destination": dest,
        "commodity": comm,
        "cargo_qty": qty
    }


def submit_trade_to_referee(trade: dict, ref_token: str) -> dict:
    """Submit trade to referee /referee/quick_order."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}/referee/quick_order"
    payload = json.dumps(trade).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {ref_token}",
            "Content-Type": "application/json",
            "User-Agent": "AgoraTradeTerminal/2.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return {"status": "error", "http_code": e.code, "error": json.loads(raw)}
        except Exception:
            return {"status": "error", "http_code": e.code, "raw_error": raw}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def parse_discord_trade(content: str, author_id: str, author_name: str, default_station: str = "ceres") -> Optional[dict]:
    """Parse natural language trade command from Discord chat."""
    m = TRADE_PATTERN.search(content)
    if not m:
        return None
    side_raw, qty_raw, comm_raw, price_raw, station_raw = m.groups()
    side = "bid" if side_raw.lower() in ("buy", "bid") else "ask"
    qty = int(qty_raw)
    comm = comm_raw.upper()
    if comm == "BANANA":
        comm = "FRAG"
    price = int(price_raw) if price_raw else None
    station = station_raw.lower() if station_raw else default_station

    # Resolve agent
    agent = None
    agent_override = re.search(r"\b(?:as|agent:?)\s+(amos|marvin|zero|aerial)\b", content, re.I)
    if agent_override:
        agent = agent_override.group(1).lower()
    elif author_id in AUTHOR_MAP:
        agent = AUTHOR_MAP[author_id]
    else:
        name_lower = author_name.lower()
        if "amos" in name_lower or "carmody" in name_lower or "mike" in name_lower:
            agent = "amos"
        elif "marvin" in name_lower or "alex" in name_lower:
            agent = "marvin"
        elif "zero" in name_lower or "brock" in name_lower or "ryan" in name_lower:
            agent = "zero"
        elif "aerial" in name_lower:
            agent = "aerial"

    if not agent:
        agent = "amos"

    return {
        "agent_id": agent,
        "side": side,
        "qty": qty,
        "limit_price": price,
        "instrument": comm,
        "station_id": station
    }


def build_burst_kickoff(burst_id: str, rounds: int, interval_sec: float, start_round: int, mention: str = "") -> str:
    """Compile formatted kickoff alert for discrete burst session."""
    end_round = start_round + rounds
    target_tag = mention if mention else DEFAULT_TARGET_TAG
    base = REFEREE_BASE_URL.rstrip('/')
    return (
        f"🚀 **STATION AGORA // SOL SYSTEM COMBINE INITIATED** ({target_tag})\n"
        f"```text\n"
        f"BURST ID:       {burst_id}\n"
        f"COMBINE WINDOW: Rounds #{start_round + 1} -> #{end_round} ({rounds} rounds)\n"
        f"ROUND CADENCE:  {interval_sec:.0f}s per strategy window\n"
        f"STATUS:         FLOOR OPEN // AUTONOMOUS MATCHMAKING ENGAGED\n\n"
        f"🏆 OBJECTIVE:   Highest Mark-to-Market Net Worth (CR) at Round #{end_round} wins!\n"
        f"⚠️ SCORING:     Net Worth = Liquid CR + Cargo (FRAG @ Ceres mark, FOOD/ORE @ Sol avg).\n"
        f"               *FUEL is consumable propellant (0 CR score value).*\n"
        f"```\n"
        f"🎯 **HOW TO TRADE THIS BURST:**\n"
        f"💬 **Discord Chat:** Reply in channel:\n"
        f"• Trade: `BUY 50 FOOD @ 32` or `SELL 100 ORE @ 9`\n"
        f"• Transit: `MOVE TO MARS WITH 100 FOOD` or `TRANSIT CERES`\n"
        f"⚡ **Quick API:** `POST {base}/referee/quick_order` with token `agora-combine-2026`\n\n"
        f"*Round 1 strategy window and depot quotes follow immediately below!*"
    )


def build_announcement(round_num: int = 1, rounds_total: int = 8, codename: str = "", mention: str = "", round_index: Optional[int] = None) -> Tuple[str, str]:
    """Compile rich thematic Strategy Window announcement. Returns (msg_text, active_station)."""
    health = fetch_json("/referee/health")
    leaderboard = fetch_json("/referee/leaderboard")
    depots = fetch_json("/referee/depots")

    seq = health.get("seq", 0)
    floor = health.get("floor", "open")

    st_idx = (round_num - 1) % len(STATION_ROTATION)
    st_key = STATION_ROTATION[st_idx]
    st_info = STATION_PROFILES[st_key]

    st_depots = depots.get("depots", {}).get("stations", {}).get(st_key, {})

    def format_quote(inst: str, fallback_bid: int, fallback_ask: int):
        q = st_depots.get(inst, {})
        b = q.get("best_bid", fallback_bid)
        a = q.get("best_ask", fallback_ask)
        return f"Bid {b} CR | Ask {a} CR"

    quotes = [
        f"• **FOOD:** {format_quote('FOOD', 28, 32)}",
        f"• **ORE:**  {format_quote('ORE', 9, 11)}",
        f"• **FUEL:** {format_quote('FUEL', 24, 26)}",
        f"• **FRAG:** {format_quote('FRAG', 19, 21)}"
    ]
    quotes_str = "\n".join(quotes)

    lb_entries = leaderboard.get("leaderboard", [])
    standings_lines = []
    for idx, e in enumerate(lb_entries[:4], 1):
        ag = e.get("agent_id", "unknown")
        fl = FLEET_NAMES.get(ag.lower(), ag.upper())
        cr = e.get("liquid", 0)
        frag = e.get("frags", 0)
        fuel = e.get("fuel", 0)
        food = e.get("food", 0)
        ore = e.get("ore", 0)
        nw = e.get("net_worth", 0)

        holdings = []
        if frag > 0:
            holdings.append(f"{frag} FRAG")
        if food > 0:
            holdings.append(f"{food} FOOD")
        if ore > 0:
            holdings.append(f"{ore} ORE")
        if fuel > 0:
            holdings.append(f"{fuel} FUEL")
        cargo_str = f" | {', '.join(holdings)}" if holdings else ""
        standings_lines.append(
            f"• **#{idx} {fl}:** **{nw:,} CR** NW ({cr:,} liquid{cargo_str})"
        )
    standings_str = "\n".join(standings_lines) if standings_lines else "No active balances"

    target_tag = mention if mention else DEFAULT_TARGET_TAG
    idx_disp = f"{round_index}/{rounds_total}" if round_index is not None else f"{round_num}/{rounds_total}"
    title = f"COMBINE ROUND {idx_disp} (Round #{round_num})"
    if codename:
        title += f" // OP {codename.upper()}"

    base = REFEREE_BASE_URL.rstrip('/')

    msg = (
        f"🔔 **STATION AGORA // {title}** ({target_tag})\n"
        f"**Sector:** {st_info['emoji']} **{st_info['name']}** | **Floor:** {floor.upper()} | **Seq:** #{seq}\n"
        f"🎯 **Objective:** Max Net Worth at Round #{rounds_total} | *Cargo scores (FRAG/FOOD/ORE), FUEL=0 CR*\n\n"
        f"📡 **GALNET:** *{st_info['intel']}* — 💡 *{st_info['opp']}*\n\n"
        f"📈 **{st_key.upper()} DEPOT QUOTES:**\n"
        f"{quotes_str}\n\n"
        f"📊 **STANDINGS (MARK-TO-MARKET NET WORTH):**\n"
        f"{standings_str}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 **DIRECTIVE (Round #{round_num}):**\n"
        f"• **Chat:** `BUY/SELL <qty> <comm> @ <px>` | `MOVE TO <st> WITH <qty> <comm>`\n"
        f"• **API:** `POST {base}/referee/quick_order` | `POST {base}/stations/transit`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return msg, st_key


def build_final_bell(codename: str = "", mention: str = "") -> str:
    """Compile formatted final settlement announcement."""
    try:
        leaderboard = fetch_json("/referee/leaderboard")
        standings_parts = []
        lb_entries = leaderboard.get("leaderboard", [])
        winner = lb_entries[0] if lb_entries else {}
        for idx, entry in enumerate(lb_entries, 1):
            agent_id = entry.get("agent_id", "unknown")
            nw = entry.get("net_worth", 0)
            cr = entry.get("liquid", 0)
            fleet = FLEET_NAMES.get(agent_id.lower(), agent_id.upper())
            medal = "🏆 " if idx == 1 else ""
            standings_parts.append(f"{medal}#{idx} {fleet}: {nw:,} CR Net Worth ({cr:,} liquid)")
        standings_line = "\n".join(standings_parts) if standings_parts else "No active balances"
        winner_name = FLEET_NAMES.get(winner.get("agent_id", "").lower(), "Unknown Syndicate")
        winner_nw = winner.get("net_worth", 0)
    except Exception as e:
        standings_line = f"Telemetry fetch error: {e}"
        winner_name = "N/A"
        winner_nw = 0

    title = f"Operation {codename.upper()} Concluded" if codename else "Sol System Combine Concluded"
    target_tag = mention if mention else DEFAULT_TARGET_TAG
    return (
        f"🏁 **STATION AGORA // {title.upper()}** ({target_tag})\n"
        f"```text\n"
        f"STATUS: COMBINE WINDOW COMPLETE // FINAL STANDINGS:\n\n"
        f"{standings_line}\n\n"
        f"CRITERIA: Final Mark-to-Market Net Worth (Liquid + Cargo; FUEL=0 CR)\n"
        f"```\n"
        f"🏆 **WINNER:** **{winner_name}** takes the Sol System Salvage Championship with **{winner_nw:,} CR** Net Worth!\n"
        f"*Exchange orderbooks settling. All trading halted until next burst.*"
    )


def poll_and_execute_trades(channel: str, bot_token: str, ref_token: str, active_station: str, processed_ids: Set[str], last_seen_id: str) -> str:
    """Poll channel messages, parse natural language trades, execute against referee, and post receipts."""
    messages = fetch_discord_messages(channel, after_id=last_seen_id, token=bot_token, limit=20)
    if not messages:
        return last_seen_id

    # Messages come in reverse chronological order (newest first)
    messages_sorted = sorted(messages, key=lambda x: int(x.get("id", 0)))
    newest_id = last_seen_id

    for msg in messages_sorted:
        msg_id = msg.get("id")
        if not msg_id or msg_id in processed_ids:
            continue
        processed_ids.add(msg_id)
        if int(msg_id) > int(newest_id or 0):
            newest_id = msg_id

        author = msg.get("author", {})
        # Skip if message is from Agora Trade Terminal itself
        if author.get("username") == "Agora Trade Terminal" or author.get("id") == "1547763904141070346":
            continue

        content = msg.get("content", "").strip()
        trade = parse_discord_trade(content, author.get("id", ""), author.get("username", ""), default_station=active_station)
        if not trade:
            transit = parse_discord_transit(content, author.get("id", ""), author.get("username", ""))
            if transit:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Detected transit from {author.get('username')}: {transit}")
                sys.stdout.flush()
                res = submit_transit_to_referee(transit, ref_token)
                ag_id = transit["agent_id"]
                fl_name = FLEET_NAMES.get(ag_id, ag_id.upper())
                dest_disp = transit["destination"].title()

                if res.get("status") == "error" or res.get("kind") == "reject":
                    err_obj = res.get("error") if isinstance(res.get("error"), dict) else {}
                    err_detail = err_obj.get("payload", {}).get("detail") or res.get("payload", {}).get("detail") or res.get("error") or str(res)
                    add_discord_reaction(channel, msg_id, "❌", bot_token)
                    reject_msg = (
                        f"⚠️ **[Agora Trade Terminal] Transit Rejected**\n"
                        f"> **Syndicate:** {fl_name}\n"
                        f"> **Target Station:** {dest_disp}\n"
                        f"> **Reason:** `{err_detail}`"
                    )
                    post_discord(channel, reject_msg, bot_token)
                else:
                    payload = res.get("payload", {})
                    origin_disp = payload.get("origin", active_station).title()
                    cargo_qty = payload.get("cargo_qty", 0)
                    comm_disp = payload.get("commodity", "")
                    cargo_str = f"**{cargo_qty} {comm_disp}**" if cargo_qty > 0 else "empty cargo bay"
                    fuel_burned = payload.get("fuel_burned", 0)
                    arrival_rnd = payload.get("arrival_round", "?")
                    duration = payload.get("rounds_duration", 1)

                    add_discord_reaction(channel, msg_id, "🚀", bot_token)
                    add_discord_reaction(channel, msg_id, "✅", bot_token)
                    receipt_msg = (
                        f"🚀 **[Agora Trade Terminal] Interplanetary Transit Dispatched**\n"
                        f"> **Syndicate:** {fl_name}\n"
                        f"> **Flight Corridor:** {origin_disp} ➔ **{dest_disp}** ({cargo_str})\n"
                        f"> **Propellant:** Burned **{fuel_burned} FUEL**\n"
                        f"> **ETA:** Arriving at {dest_disp} on **Round #{arrival_rnd}** ({duration} round(s))\n"
                        f"> **Status:** Fleet undocked and in transfer orbit."
                    )
                    post_discord(channel, receipt_msg, bot_token)
            continue

        # If price was omitted, default to reasonable limit from current depots
        if not trade.get("limit_price"):
            # Default fallback: buy high, sell low to ensure immediate match
            trade["limit_price"] = 50 if trade["side"] == "bid" else 5

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Detected trade from {author.get('username')}: {trade}")
        sys.stdout.flush()

        res = submit_trade_to_referee(trade, ref_token)

        ag_id = trade["agent_id"]
        fl_name = FLEET_NAMES.get(ag_id, ag_id.upper())
        side_disp = "BOUGHT" if trade["side"] == "bid" else "SOLD"
        st_disp = trade["station_id"].title()

        if res.get("status") == "error" or res.get("kind") == "reject":
            err_obj = res.get("error") if isinstance(res.get("error"), dict) else {}
            err_detail = err_obj.get("payload", {}).get("detail") or res.get("payload", {}).get("detail") or res.get("error") or str(res)
            add_discord_reaction(channel, msg_id, "❌", bot_token)
            reject_msg = (
                f"⚠️ **[Agora Trade Terminal] Order Rejected**\n"
                f"> **Syndicate:** {fl_name}\n"
                f"> **Attempted:** {side_disp} {trade['qty']} {trade['instrument']} @ {trade['limit_price']} CR\n"
                f"> **Reason:** `{err_detail}`"
            )
            post_discord(channel, reject_msg, bot_token)
        else:
            add_discord_reaction(channel, msg_id, "🚀", bot_token)
            add_discord_reaction(channel, msg_id, "✅", bot_token)
            receipt_msg = (
                f"🧾 **[Agora Trade Terminal] Order Executed & Cleared**\n"
                f"> **Syndicate:** {fl_name}\n"
                f"> **Action:** {side_disp} **{trade['qty']} {trade['instrument']}** @ **{trade['limit_price']} CR** at **{st_disp} Depot**\n"
                f"> **Status:** Matched against orderbook / depot pool."
            )
            post_discord(channel, receipt_msg, bot_token)

    return newest_id


def run_burst_loop(rounds: int, interval_sec: float, channel: str, token: str, codename: str = "", mention: str = "") -> int:
    """Execute server-mediated burst run and announce round progression to Discord."""
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
    kickoff_resp = post_discord(channel, kickoff_msg, token)

    last_seen_msg_id = kickoff_resp.get("id", "") if kickoff_resp else ""
    processed_ids: Set[str] = set()
    if last_seen_msg_id:
        processed_ids.add(last_seen_msg_id)

    # Immediately post Round 1 strategy window at T=0 so the floor is actionable instantly
    rounds_announced = 1
    last_announced_round = start_round
    burst_completed = False
    ref_token = get_referee_token()

    msg, active_station = build_announcement(
        round_num=start_round + 1,
        rounds_total=rounds,
        codename=codename,
        mention=mention,
        round_index=1
    )
    ann_resp = post_discord(channel, msg, token)
    if ann_resp and ann_resp.get("id"):
        last_seen_msg_id = ann_resp["id"]
        processed_ids.add(last_seen_msg_id)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Broadcasted Round {start_round + 1} (1/{rounds}) at {active_station} (T=0 kickoff)")
    sys.stdout.flush()

    while not burst_completed:
        # 1. Listen for and execute Discord chat trades during strategy window
        last_seen_msg_id = poll_and_execute_trades(
            channel=channel,
            bot_token=token,
            ref_token=ref_token,
            active_station=active_station,
            processed_ids=processed_ids,
            last_seen_id=last_seen_msg_id
        )

        time.sleep(min(2.0, max(0.5, interval_sec / 10)))
        st = fetch_ticker_status()
        if st.get("status") == "error":
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Warning: transient error fetching ticker status: {st.get('error')}. Retrying...")
            sys.stdout.flush()
            time.sleep(2.0)
            continue

        cur_rnd = st.get("current_round", last_announced_round)
        is_active = st.get("burst_active", False)
        rounds_remaining = st.get("rounds_remaining", 0)

        if cur_rnd > last_announced_round:
            last_announced_round = cur_rnd
            rounds_announced += 1
            if rounds_announced <= rounds:
                next_round_num = start_round + rounds_announced
                msg, active_station = build_announcement(
                    round_num=next_round_num,
                    rounds_total=rounds,
                    codename=codename,
                    mention=mention,
                    round_index=rounds_announced
                )
                ann_resp = post_discord(channel, msg, token)
                if ann_resp and ann_resp.get("id"):
                    last_seen_msg_id = ann_resp["id"]
                    processed_ids.add(last_seen_msg_id)
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Broadcasted Round {next_round_num} ({rounds_announced}/{rounds}) at {active_station}")
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
    parser = argparse.ArgumentParser(description="Agora Trade Terminal Round Announcer & Order Router")
    parser.add_argument("--dry-run", action="store_true", help="Print announcement without posting to Discord")
    parser.add_argument("--once", action="store_true", help="Post single announcement immediately")
    parser.add_argument("--burst", type=int, default=0, help="Run discrete server-mediated burst of N rounds (e.g. --burst 8)")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL_ID, help="Target Discord channel ID")
    parser.add_argument("--round", type=int, default=1, help="Starting round number (legacy manual mode)")
    parser.add_argument("--rounds", type=int, default=0, help="Total rounds to execute before final bell (legacy mode)")
    parser.add_argument("--interval", type=float, default=0, help="Loop interval in seconds (default: 180s for burst)")
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

    interval_sec = args.interval if args.interval > 0 else (180.0 if args.burst > 0 else 300.0)

    if args.dry_run:
        print("=== DRY RUN KICKOFF ===")
        print(build_burst_kickoff("burst-dryrun", args.burst or 8, interval_sec, 1, mention=mention))
        print("\n=== DRY RUN STRATEGY WINDOW ===")
        msg, _ = build_announcement(round_num=1, rounds_total=args.burst or 8, codename=args.codename, mention=mention)
        print(msg)
        print("\n=== DRY RUN FINAL BELL ===")
        print(build_final_bell(codename=args.codename, mention=mention))
        return 0

    token = get_bot_token()
    if not token:
        print("Error: AGORA_TERMINAL_BOT_TOKEN not found in env or /secrets/env.json", file=sys.stderr)
        return 1

    if args.once:
        content, _ = build_announcement(round_num=args.round, rounds_total=8, codename=args.codename)
        resp = post_discord(args.channel, content, token)
        if resp:
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

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
