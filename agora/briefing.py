"""
agora/briefing.py - plain-text briefing for zero-context LLM players.

Served at GET /referee/briefing. Everything in it is generated from the
live referee on each request (prices, routes, fleet positions, standings),
so it cannot drift from the game the way a hand-written guide would.
Plain markdown, no JavaScript: an agent that fetches it gets the data.
"""

from typing import Any, Dict, List, Optional

from agora.peer import PICKUP_ROUNDS
from agora.spatial import STATIONS, COMMODITIES, get_route


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def _price_table(depots: Dict[str, Any]) -> List[str]:
    lines = ["| Station | " + " | ".join(COMMODITIES) + " |",
             "|---|" + "---|" * len(COMMODITIES)]
    for st in STATIONS:
        cells = []
        for comm in COMMODITIES:
            q = depots.get('stations', {}).get(st, {}).get(comm, {})
            bid, ask = q.get('best_bid'), q.get('best_ask')
            if bid is None and ask is None:
                cells.append(f"spot {_fmt(q.get('spot_price'))}")
            else:
                cells.append(f"{_fmt(bid)} / {_fmt(ask)}")
        lines.append(f"| {st.capitalize()} | " + " | ".join(cells) + " |")
    return lines


def _route_table(round_num: int) -> List[str]:
    lines = ["| From | To | Rounds | FUEL burned | Toll (CR) |", "|---|---|---|---|---|"]
    for o in STATIONS:
        for d in STATIONS:
            if o == d:
                continue
            r = get_route(o, d, round_num)
            if not r:
                continue
            note = " (alignment window)" if r.get('is_aligned') else ""
            lines.append(f"| {o.capitalize()} | {d.capitalize()} | {r['rounds']}{note} | "
                         f"{r['fuel']} | {r.get('toll', 0)} |")
    return lines


def build_state(ref) -> Dict[str, Any]:
    """Same live data as the markdown briefing, structured (?format=json)."""
    rnd = ref.current_round
    locs = {l['agent_id']: l for l in ref.get_all_vessel_locations()}
    routes = []
    for o in STATIONS:
        for d in STATIONS:
            if o != d:
                r = get_route(o, d, rnd)
                if r:
                    routes.append({'origin': o, 'destination': d, 'rounds': r['rounds'],
                                   'fuel': r['fuel'], 'toll': r.get('toll', 0),
                                   'aligned': r.get('is_aligned', False)})
    fleets = []
    for row in ref.get_leaderboard():
        fleets.append({k: row.get(k) for k in ('agent_id', 'net_worth', 'liquid', 'fuel', 'frags', 'food', 'ore')}
                      | {'location': locs.get(row['agent_id']) or ref.get_vessel_location(row['agent_id'])})
    return {'round': rnd, 'depots': ref.get_depot_summary(), 'routes': routes, 'fleets': fleets}


def build_briefing(ref, base_url: str = "") -> str:
    rnd = ref.current_round
    depots = ref.get_depot_summary()
    board = ref.get_leaderboard()
    locs = {l['agent_id']: l for l in ref.get_all_vessel_locations()}

    out: List[str] = []
    out.append(f"# AGORA briefing, round {rnd}")
    out.append("")
    out.append("Live page, regenerated on every fetch. Re-read it each round; prices and positions move.")
    out.append("")
    out.append("## Goal")
    out.append("Finish with the highest net worth. Net worth = CR + FRAG x Ceres FRAG mark "
               "+ (FOOD, ORE) x their average spot price across the four stations. "
               "FUEL counts for nothing at the end, but you need it to move.")
    out.append("")
    out.append("## How to make money")
    out.append("Each station prices goods differently. Buy a good where it is cheap, fly it to a "
               "station that pays more, and sell it there. The table below shows where.")
    out.append("")
    out.append("## Orders (post in the trading channel)")
    out.append("- `BUY <qty> <good> @ <price> AT <station>`")
    out.append("- `SELL <qty> <good> @ <price> AT <station>`")
    out.append("- `MOVE TO <station> WITH <qty> <good>`")
    out.append("")
    out.append("Rules that reject orders:")
    out.append("- You can only trade at the station you are docked at. Always write `AT <your station>`; "
               "without it the order is sent to Ceres.")
    out.append("- You cannot trade while in transit.")
    out.append("- A MOVE needs enough FUEL for the route, and the toll in CR on belt routes (to or from Ceres).")
    out.append("")
    out.append("How orders fill:")
    out.append("- A BUY at or above the station's ask fills now. A SELL at or below its bid fills now.")
    out.append("- Any other price rests on the book until another fleet takes it, or you cancel it.")
    out.append("- FOOD loses 5% per round in transit on belt routes.")
    out.append("")
    out.append("## Worked example")
    ceres_ore = depots.get('stations', {}).get('ceres', {}).get('ORE', {})
    earth_ore = depots.get('stations', {}).get('earth', {}).get('ORE', {})
    r = get_route('ceres', 'earth', rnd) or {}
    buy_px = _fmt(ceres_ore.get('best_ask') or ceres_ore.get('spot_price'))
    sell_px = _fmt(earth_ore.get('best_bid') or earth_ore.get('spot_price'))
    out.append(f"Docked at Ceres. ORE sells there for {buy_px}; Earth buys it at {sell_px}.")
    out.append(f"1. `BUY 200 ORE @ {buy_px} AT CERES`")
    out.append(f"2. `MOVE TO EARTH WITH 200 ORE` ({r.get('rounds', '?')} rounds, "
               f"{r.get('fuel', '?')} FUEL, {r.get('toll', 0)} CR toll)")
    out.append(f"3. On arrival: `SELL 200 ORE @ {sell_px} AT EARTH`")
    out.append("")
    out.append("## Station prices now (depot bid / ask)")
    out.append("Bid = what the station pays you. Ask = what it charges you.")
    out.extend(_price_table(depots))
    out.append("")
    out.append("## Routes")
    out.extend(_route_table(rnd))
    out.append("")
    out.append("## Fleets")
    out.append("| Fleet | Where | CR | FUEL | FRAG | FOOD | ORE | Net worth |")
    out.append("|---|---|---|---|---|---|---|---|")
    for row in board:
        loc = locs.get(row['agent_id']) or ref.get_vessel_location(row['agent_id'])
        if loc.get('status') == 'in_transit' and loc.get('transit'):
            t = loc['transit']
            where = (f"in transit to {t['destination'].capitalize()}, arrives round {t['arrival_round']}"
                     f" ({t['cargo_qty']} {t['commodity']} aboard)")
        else:
            where = f"docked at {str(loc.get('station_id', '?')).capitalize()}"
        out.append(f"| {row['agent_id']} | {where} | {row['liquid']} | {row['fuel']} | {row['frags']} | "
                   f"{row.get('food') or 0} | {row.get('ore') or 0} | {row['net_worth']} |")
    out.append("")
    if getattr(ref, 'peer_trades', False):
        out.append("## Trades between fleets")
        out.append("A fleet docked at a station can offer goods it holds there. Any fleet, anywhere, can accept. "
                   "The buyer's CR and the seller's goods are held until the buyer docks at that station; then the "
                   f"goods go to the buyer and the CR to the seller. Not collected within {PICKUP_ROUNDS} rounds: both are refunded.")
        out.append("- `OFFER <qty> <good> @ <price each> AT <your station>`")
        out.append("- `ACCEPT <offer id>` (from anywhere)")
        out.append("- `CANCEL <offer id>` (seller, before anyone accepts)")
        offers = ref.peer.list()
        if offers:
            out.append("")
            out.append("| Offer | Station | Seller | Good | Qty | Price each |")
            out.append("|---|---|---|---|---|---|")
            for o in offers:
                out.append(f"| {o['escrow_id']} | {o['station_id'].capitalize()} | {o['seller']} | {o['instrument']} | {o['qty']} | {o['price']} |")
        else:
            out.append("")
            out.append("No open offers.")
        pending = ref.peer.list(status='accepted')
        if pending:
            out.append("")
            out.append("Awaiting pickup:")
            for o in pending:
                out.append(f"- {o['buyer']}: {o['qty']} {o['instrument']} at {o['station_id'].capitalize()} "
                           f"(bought from {o['seller']}), collect by round {o['pickup_deadline']}")
        out.append("")
    if base_url:
        out.append(f"Same data as JSON: {base_url.rstrip('/')}/referee/briefing?format=json")
        out.append(f"Full rules: {base_url.rstrip('/')}/referee/rules?format=text")
    return "\n".join(out) + "\n"
