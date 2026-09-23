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


def _depots(ref, viewer):
    return ref.fog.depot_view(ref, viewer) if getattr(ref, 'fog', None) else ref.get_depot_summary()


def build_state(ref, viewer: Optional[str] = None) -> Dict[str, Any]:
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
    return {'round': rnd, 'viewer': viewer, 'depots': _depots(ref, viewer), 'routes': routes, 'fleets': fleets}


def build_briefing(ref, base_url: str = "", viewer: Optional[str] = None) -> str:
    rnd = ref.current_round
    depots = _depots(ref, viewer)
    fog = getattr(ref, 'fog', None)
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
    out.append("There is no limit on actions per round. Send as many orders as you like each round: buy and sell "
               "goods, trade stocks, make or accept offers, and start a trip. The limits are physical: one trip "
               "at a time, and goods trade only where you are docked.")
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
    if getattr(ref, 'idle_fee', 0):
        out.append(f"- Idle fee: a docked fleet that does nothing in a round (no order, cancel, move or offer) "
                   f"pays {ref.idle_fee} CR, in any round where another fleet is playing.")
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
    if fog and viewer != 'admin':
        here = (depots.get('fog') or {}).get('exact_station')
        if viewer and here:
            out.append(f"**Fog.** You are {viewer}. Prices at {here.capitalize()}, where you are docked, are exact. "
                       f"Every other station shows prices about {fog.lag} rounds old, each off by up to "
                       f"{int(fog.noise * 100)}%.")
        elif viewer:
            out.append(f"**Fog.** You are {viewer}, in transit, so every price here is about {fog.lag} rounds old "
                       f"and off by up to {int(fog.noise * 100)}%. You see exact prices again once you dock.")
        else:
            out.append(f"**Fog.** This is the public view: every price is about {fog.lag} rounds old and off by up "
                       f"to {int(fog.noise * 100)}%. Fetch this page with your fleet token "
                       "(`Authorization: Bearer <your token>`) to see exact prices where you are docked.")
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
    if getattr(ref, 'rival_shares', 0):
        base = {r['agent_id']: r['net_worth'] - r.get('stocks_value', 0) for r in board}
        marks = ref.stock_marks(base)
        out.append("## Fleet stocks")
        out.append("Every fleet has 1,000 shares. You start with some of each rival's. A share's fair value "
                   "(NAV) is its fleet's net worth before stocks / 1,000; the board price is set by trading. "
                   "Rival shares count toward your net worth at the board price. Stocks trade on one exchange, "
                   "from anywhere, even in transit, and are never fogged.")
        if getattr(ref, 'exchange_shares', 0):
            out.append("The exchange itself always quotes every stock, a few shares a side each round, around a "
                       "price that follows the fleet's recent NAV but swings on its own. You can always buy or "
                       "sell some stock; for size, trade with other fleets.")
        out.append("- `BUY <qty> EQ_<FLEET> @ <price>` / `SELL <qty> EQ_<FLEET> @ <price>` (no `AT` needed)")
        out.append("")
        out.append("| Stock | Fleet | NAV | Board price | Bid / ask |")
        out.append("|---|---|---|---|---|")
        for sym, m in marks.items():
            out.append(f"| {sym} | {m['issuer']} | {_fmt(m['nav'])} | {_fmt(m['mark'])} ({m['basis']}) | "
                       f"{_fmt(m['best_bid'])} / {_fmt(m['best_ask'])} |")
        out.append("")
        out.append("Holdings: " + "; ".join(
            f"{r['agent_id']}: " + (", ".join(f"{q} {sym}" for sym, q in sorted(r.get('stocks', {}).items())) or "none")
            for r in board))
        out.append("")
    if getattr(ref, 'corporate_enabled', False):
        from agora.corporate import AUCTION_CAP, BANKRUPT_ROUNDS, TAKEOVER_SHARES
        summ = ref.corporate.summary()
        out.append("## Corporate risk")
        out.append(f"A penalty you cannot pay becomes debt. Each round your cash pays it down first. Still in debt, your "
                   f"goods are sold to the local depot (when docked), then up to {AUCTION_CAP} of your own treasury shares "
                   f"a round are auctioned to your rivals at a discount. In debt for {BANKRUPT_ROUNDS} rounds with no "
                   f"treasury shares left, you are bankrupt and out. A rival holding {TAKEOVER_SHARES} of your shares "
                   f"(51%) takes you over: your cash, goods, holdings, contracts and debt become theirs, and you are out. "
                   f"The last corp standing wins; otherwise the leaderboard decides at the final bell. You can also sell "
                   f"your own shares to raise cash, and buying a rival's shares is how a takeover starts.")
        out.append("")
        out.append("| Corp | Status | Debt | Rounds in debt |")
        out.append("|---|---|---|---|")
        for a_id, r in sorted(summ['corps'].items()):
            st = r['status'] + (f" (by {r['absorbed_by']})" if r.get('absorbed_by') else "")
            out.append(f"| {a_id} | {st} | {r['debt']} | {r['rounds_in_debt']} |")
        if summ['winner']:
            out.append("")
            out.append(f"**{summ['winner']} has won: last corp standing.**")
        if summ['events']:
            out.append("")
            for e in summ['events'][:8]:
                out.append(f"- round {e['round']}: {e['detail'] if e['detail'].startswith(e['agent_id']) else e['agent_id'] + ': ' + e['detail']}")
        out.append("")
    odds = getattr(getattr(ref, 'hazards', None), 'odds', None)
    if odds:
        out.append("## Hazards in flight")
        out.append(f"Every trip risks bad luck, rolled when you leave and reported at once in your move's response: "
                   f"a {int(round(odds[0] * 100))}% chance of a storm that adds 1-3 rounds to the trip (a late ship can miss "
                   f"a contract deadline), and a {int(round(odds[1] * 100))}% chance of losing 30-70% of the cargo. "
                   f"When it happens, work around it: sell a contract you will now miss, raise cash, change plans.")
        recent = ref.hazards.recent(max(0, ref.current_round - 10))
        if recent:
            out.append("")
            for h in recent[:8]:
                out.append(f"- round {h['round']}: {h['agent_id']}: {h['note']}")
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
    if getattr(ref, 'contracts_enabled', False):
        from agora.contracts import MAX_OPEN, BOND_PCT, PENALTY
        out.append("## Station contracts")
        out.append(f"Stations post contracts to buy a good at a premium by a deadline round. The first fleet to claim one "
                   f"owns it, at most {MAX_OPEN} at a time. Claiming locks a deposit of {int(BOND_PCT * 100)}% of the "
                   f"undelivered value. Only the owner delivers, docked at that station, in part or in full; each "
                   f"delivery is paid at the contract price and returns its share of the deposit. Miss the deadline and "
                   f"you lose the rest of the deposit and pay a penalty of {int(PENALTY * 100)}% of the undelivered value. "
                   f"You can list a contract you own for sale; the buyer pays your price plus the deposit it carries.")
        out.append("- `POST /referee/contracts/<id>/claim`")
        out.append("- `POST /referee/contracts/<id>/deliver {\"qty\": n}` (docked at the contract's station; omit qty to deliver all you can)")
        out.append("- `POST /referee/contracts/<id>/list {\"price\": p}` (0 unlists) / `POST /referee/contracts/<id>/buy`")
        rows = ref.contract_desk.list()
        out.append("")
        if rows:
            out.append("| Contract | Station | Good | Left / total | Price each | Deadline round | Owner | For sale |")
            out.append("|---|---|---|---|---|---|---|---|")
            for c in rows:
                sale = f"{c['list_price']} + {c['bond']} deposit" if c['list_price'] else "-"
                out.append(f"| {c['contract_id']} | {c['station_id'].capitalize()} | {c['instrument']} | "
                           f"{c['qty_remaining']} / {c['qty_total']} | {c['price']} | {c['deadline']} | "
                           f"{c['owner'] or 'unclaimed'} | {sale} |")
        else:
            out.append("No open contracts.")
        out.append("")
    if base_url:
        out.append(f"Same data as JSON: {base_url.rstrip('/')}/referee/briefing?format=json")
        out.append(f"Full rules: {base_url.rstrip('/')}/referee/rules?format=text")
    return "\n".join(out) + "\n"
