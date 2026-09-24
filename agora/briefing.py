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


def _where(loc: Dict[str, Any]) -> str:
    if loc.get('status') == 'in_transit' and loc.get('transit'):
        t = loc['transit']
        return (f"in transit to {t['destination'].capitalize()}, arrives round {t['arrival_round']}"
                f" ({t['cargo_qty']} {t['commodity']} aboard)")
    return f"docked at {str(loc.get('station_id', '?')).capitalize()}"


def _ships(ref, agent: str) -> List[Dict[str, Any]]:
    """Every ship of a fleet: where it is and what its hold carries (#175)."""
    fleet = getattr(ref, 'fleet', None)
    if fleet is None or not fleet.is_corp(agent):
        return []
    out = []
    for loc in ref.fleet_locations(agent):
        vid = loc['vessel_id']
        h = fleet.hold_status(vid)
        out.append({'vessel_id': vid, 'where': _where(loc), 'location': loc,
                    'hold': {c: ref.get_balance(vid, c) for c in ('FUEL', 'FRAG', 'FOOD', 'ORE')},
                    'hold_used': h['hold_used'], 'hold_capacity': h['hold_capacity']})
    return out


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
                      | {'location': locs.get(row['agent_id']) or ref.get_vessel_location(row['agent_id']),
                         'ships': _ships(ref, row['agent_id'])})
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
    out.append("Finish with the highest net worth. Net worth = CR + (FRAG, FOOD, ORE) "
               "x their local station spot price. "
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
    if getattr(ref, 'ship_hold', 0):
        out.append(f"Your ship starts with its hold full ({ref.ship_hold} FRAG aboard; the rest of your FRAG waits "
                   "in your station hold `@<home>`), so sell FRAG, or move some to the station hold with "
                   "`POST /referee/vessels/transfer`, before you buy. See Ships, Hold size.")
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
    history_engine = getattr(ref, 'history_engine', None)
    if history_engine:
        v_station = 'earth'
        if viewer and viewer in locs and locs[viewer].get('station_id'):
            v_station = locs[viewer]['station_id']
        hist_table = history_engine.briefing_table(ref, v_station, rounds=5, viewer=viewer)
        if hist_table:
            out.extend(hist_table)
            out.append("")
    out.append("## Routes")
    out.extend(_route_table(rnd))
    out.append("")
    out.append("## Fleets")
    out.append("| Fleet | Where | CR | FUEL | FRAG | FOOD | ORE | Net worth |")
    out.append("|---|---|---|---|---|---|---|---|")
    for row in board:
        loc = locs.get(row['agent_id']) or ref.get_vessel_location(row['agent_id'])
        where = _where(loc)
        n = row.get('ships') or 1
        if n > 1:
            where += f" (ship 1 of {n})"
        out.append(f"| {row['agent_id']} | {where} | {row['liquid']} | {row['fuel']} | {row['frags']} | "
                   f"{row.get('food') or 0} | {row.get('ore') or 0} | {row['net_worth']} |")
    out.append("")
    out.extend(_ships_section(ref, board, viewer))
    if getattr(ref, 'rival_shares', 0):
        base = {r['agent_id']: r['net_worth'] - r.get('stocks_value', 0) for r in board}
        marks = ref.stock_marks(base)
        out.append("## Fleet stocks")
        out.append("Every fleet has 1,000 shares. You start with some of each rival's. A share's fair value "
                   "(NAV) is its fleet's net worth before stocks / 1,000; the board price is set by trading. "
                   "Rival shares count toward your net worth at the board price. Stocks trade on one exchange, "
                   "from anywhere, even in transit, and are never fogged.")
        if getattr(ref, 'exchange_shares', 0):
            from agora import exchange as X
            out.append(f"The exchange itself always quotes every stock, {X.MIN_DEPTH} to {X.MAX_DEPTH} shares a side "
                       f"each round (deeper the more that stock has traded over the last {X.VOLUME_ROUNDS} rounds), "
                       "around a price that follows the fleet's recent NAV but swings on its own. It never holds "
                       f"more than {X.MAX_SHARES} shares of any fleet. You can always buy or sell some stock; for "
                       "size, trade with other fleets.")
            if getattr(ref, 'events_enabled', False):
                pc = lambda v: f"{v * 100:+g}%"
                out.append("News moves that price once, then it drifts back toward NAV: an upgrade "
                           f"{pc(X.SHOCKS['upgrade'][1])}, an escort or a raid fought off {pc(X.SHOCKS['escort'][1])}, "
                           f"cargo lost to a hazard or pirates {pc(X.LOSS_PER)} per {X.LOSS_UNIT:,} CR lost (at most "
                           f"{pc(X.LOSS_CAP)}), a missed contract {pc(X.SHOCKS['contract_lapse'][1])}, a covert move "
                           f"exposed {pc(X.SHOCKS['privateer_contract'][1])} for whoever paid for it, a rival reaching "
                           f"20% of the fleet {pc(X.SHOCKS['stake_20'][1])}. Secrets move nothing until exposed.")
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
                if e.get('exposed'):
                    out.append(f"- round {e['round']}: exposed, {e['actor']}: {e['detail']}")
                    continue
                out.append(f"- round {e['round']}: {e['detail'] if e['detail'].startswith(e['agent_id']) else e['agent_id'] + ': ' + e['detail']}")
    if getattr(ref, 'events_enabled', False):
        from agora import events as E
        out.append("## Secrets and scandals")
        out.append(f"Covert moves are secret: only you see a privateer contract you pay for. Its victim sees each raid "
                   f"it causes but not who paid. Each round a secret has a {E.LEAK_CHANCE:.0%} chance to leak for "
                   f"{E.LEAK_ROUNDS} rounds, and a traced raid exposes it at once; an exposed secret is public, named "
                   f"on GalNet as a scandal. A fleet's stake in a rival reaching {E.STAKE_PCT:.0%} is disclosed "
                   f"publicly. What your token can see: `GET /referee/corporate/events`.")
        from agora import covert as C
        out.append(f'Espionage and sabotage: `POST /referee/covert/wiretap {{"target": "<corp>"}}` ({C.WIRETAP_COST:,} CR, '
                   f'reveals secret actions and cargo for {C.WIRETAP_ROUNDS} rounds), '
                   f'`POST /referee/covert/sabotage {{"target": "<corp>"}}` ({C.SABOTAGE_COST:,} CR: siphons '
                   f'{C.TRANSIT_SIPHON:.0%} of cargo in flight and delays it a round, or takes {C.DOCK_STEAL:.0%} of a '
                   f'docked hold; you keep {C.SABOTAGE_LOOT_SHARE:.0%} of what is taken; a target cannot be hit again '
                   f'for {C.SABOTAGE_COOLDOWN} rounds; {C.SABOTAGE_TRACE:.0%} trace chance triggers a '
                   f'{C.SABOTAGE_FINE:,} CR restitution fine to the victim). '
                   'Bilateral grievances and Bad Blood: `GET /referee/corporate/rivalry`.')
        mine = [e for e in ref.events.visible_to(viewer, max(0, rnd - 20), 20)
                if e['visibility'] != 'public' or e['kind'] == 'stake_20']
        if mine:
            out.append("")
            for e in mine[:8]:
                who = e['actor'] or 'unknown'
                tag = 'exposed' if e['exposed'] else e['visibility']
                out.append(f"- round {e['round']} ({tag}): {who} -> {e['victim'] or '-'}: {e['detail']}")
        out.append("")
    standing = getattr(ref, 'standing', None)
    if standing is not None and standing.enabled:
        out.extend(_standing_section(standing, viewer))
    if getattr(ref, 'upgrades_enabled', False):
        out.append("## Ship upgrades")
        out.append("Fitted while docked at any station, paid in CR, permanent for the game, one tier at a time. "
                   "Net worth (and your stock's NAV) counts a fitted upgrade at half its price, so buying one "
                   "is still a bet that protection pays for itself; the purchase itself lifts your stock about 2%. "
                   "`POST /referee/upgrades/buy {\"kind\": \"<kind>\"}`")
        out.append("")
        out.append("Tiers are fitted in order and go on sale on the shipyard schedule below; GalNet announces "
                   "each one. A tier's odds factor replaces the one before it.")
        out.append("")
        out.append("| Upgrade | Effect | Tiers: price, odds factor, on sale from |")
        out.append("|---|---|---|")
        for c in ref.upgrades.catalog():
            tiers = []
            for t in c['tier_detail']:
                odds = f" x{t['factor']:g}" if c['kind'] != 'engines' else ""
                when = (f"locked until round {t['unlock_round']}" if t['locked']
                        else "on sale" if t['unlock_round'] == 0 else f"on sale since round {t['unlock_round']}")
                tiers.append(f"t{t['tier']} {t['price']:,}{odds} ({when})")
            out.append(f"| {c['kind']} | {c['what']} | {'; '.join(tiers)} |")
        rows = ref.conn.execute("SELECT agent_id, kind, tier FROM fleet_upgrades ORDER BY agent_id, kind").fetchall()
        if rows:
            out.append("")
            out.append("Fitted: " + "; ".join(f"{r['agent_id']} {r['kind']} {r['tier']}" for r in rows))
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
    flow = getattr(ref, 'order_flow', None)
    if flow is not None and flow.enabled and getattr(ref, 'depots_enabled', False):
        from agora import order_flow as OF
        from agora.referee import REACTIVE_SIDE_DRIP
        out.append("## Station order flow")
        out.append("Every round each station's own buyers and sellers come to its market. They pay up to the depot's "
                   "ask and sell down to the depot's bid, and they fill resting fleet orders first, best price first: "
                   "rest an ask at or below the depot ask, or a bid at or above the depot bid, and the station's "
                   "traders trade with you before the depot (a fleet order beats the depot at the same price). "
                   + ("Where a good is dearest its buyers are the depot's, and where it is cheapest its sellers "
                      "are: no NPC traders on those sides. "
                      if OF.FLOW_MAIN_SCALE == 0 else
                      "Buyers come mostly where a good is dearest, sellers where it is cheapest. ")
                   + f"About {OF.FLOW_SCALE * REACTIVE_SIDE_DRIP:g} units a side a round everywhere else. "
                   "Sizes and last round's fills: `GET /referee/order-flow`.")
        out.append("")
    pir = getattr(ref, 'piracy', None)
    if pir is not None and pir.enabled:
        from agora import piracy as P
        belt, inner = pir.odds
        hot, until = pir.hot_station(rnd), pir.hot_until(rnd)
        pct = lambda x: f"{x * 100:g}%"
        out.append("## Piracy")
        out.append(f"**Hot station now: {hot.capitalize()}**, until round {until}. Trips to or from it carry "
                   f"{P.HOT_MULT:g}x the raid risk. A new hot station is named every {P.HOT_EVERY} rounds.")
        out.append("")
        out.append(f"Every trip with cargo can be raided, rolled once when you leave. Base chance: {pct(belt)} on belt "
                   f"routes (the tolled ones, to or from Ceres), {pct(inner)} on inner routes. Raiders follow value: "
                   f"the chance is scaled x{P.VALUE_MULT[0]:g} to x{P.VALUE_MULT[1]:g} by cargo value against "
                   f"{P.VALUE_REF:,} CR (value = qty x the good's average base price: "
                   + ", ".join(f"{c} {v:g}" for c, v in P.REF_PRICE.items()) + ").")
        out.append(f"- Escort: add `\"escort\": true` to your move (`POST /stations/transit`). It costs "
                   f"{pct(P.ESCORT_PCT)} of the cargo's value in CR and cuts the raid chance by {pct(P.ESCORT_CUT)}.")
        out.append(f"- If raiders stop you, your move's response has a `piracy.demand`: pay a ransom of "
                   f"{pct(P.RANSOM_PCT)} of the cargo's value in CR, or surrender {pct(P.SURRENDER_PCT)} of the cargo. "
                   f"Answer before the next round: `POST /referee/piracy/<transit_id>/respond "
                   f"{{\"choice\": \"pay|surrender|fight\"}}`. No answer counts as fight.")
        out.append(f"- Fight: {pct(P.FIGHT_ESCAPE)} chance you escape with nothing lost; otherwise you lose "
                   f"{pct(P.FIGHT_LOSS)} of the cargo and arrive {P.FIGHT_DELAY[0]}-{P.FIGHT_DELAY[1]} rounds late.")
        out.append(f"- Black market: stolen goods are fenced at {P.FENCE_STATION.capitalize()}'s depot, "
                   f"so its supply rises and its price dips for a while.")
        out.append(f"- Privateers: `POST /referee/privateers {{\"target\": \"<fleet>\"}}` costs {P.PRIV_COST:,} CR and "
                   f"adds {pct(P.PRIV_ADD)} to that fleet's raid chance for {P.PRIV_ROUNDS} rounds. You get "
                   f"{pct(P.PRIV_SHARE)} of whatever is taken from it. Each raid you sponsor has a {pct(P.PRIV_TRACE)} "
                   f"chance to be traced: a fine of {P.PRIV_FINE}x your fee and your name in this briefing"
                   + (" and on GalNet" if getattr(ref, 'events_enabled', False) else "") + ". One "
                   f"contract at a time; not against yourself.")
        out.append("- Odds, recent raids and contracts: `GET /referee/piracy`.")
        pending = [r for r in pir.recent_raids(max(0, rnd - 2)) if r['status'] == 'pending']
        if pending:
            out.append("")
            out.append("Demands awaiting an answer:")
            for r in pending:
                out.append(f"- {r['agent_id']} ({r['transit_id']}): pay {r['ransom']} CR or surrender "
                           f"{r['surrender_qty']} {r['commodity']}, answer before round {r['round'] + 1}")
        recent = [r for r in pir.recent_raids(max(0, rnd - 10), viewer=viewer) if r['status'] != 'pending']
        if recent:
            out.append("")
            out.append("Recent raids:")
            for r in recent[:8]:
                how = {'paid': f"paid {r['cr_taken']} CR", 'surrendered': f"gave up {r['qty_taken']} {r['commodity']}",
                       'escaped': "fought and escaped", 'lost': f"fought and lost {r['qty_taken']} {r['commodity']}, "
                                                                f"{r['delay']} round(s) late",
                       'void': "trip ended before it was settled"}.get(r['status'], r['status'])
                tag = " (timed out)" if r['timed_out'] else ""
                out.append(f"- round {r['round']}: {r['agent_id']}, {r['origin'].capitalize()} to "
                           f"{r['destination'].capitalize()}: {how}{tag}")
        contracts = pir.active_contracts(viewer)
        if contracts:
            out.append("")
            out.append("Privateer contracts in force: " + "; ".join(
                f"against {c['target']}, {c['rounds_left']} rounds left"
                + (f" (sponsor: {c['sponsor']})" if c['sponsor'] else "") for c in contracts))
        traced = pir.traced(max(0, rnd - P.PRIV_ROUNDS))
        if traced:
            out.append("")
            for t in traced:
                out.append(f"- **Traced, round {t['round']}:** {t['sponsor']} sponsored the raid on {t['agent_id']} "
                           f"and was fined {t['fine']} CR.")
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
        from agora.contracts import MAX_OPEN, BOND_PCT, PENALTY, FIRST_PENALTY
        out.append("## Station contracts")
        out.append(f"Stations post contracts to buy a good at a premium by a deadline round. The first fleet to claim one "
                   f"owns it, at most {MAX_OPEN} at a time. Claiming locks a deposit of {int(BOND_PCT * 100)}% of the "
                   f"undelivered value. Only the owner delivers, docked at that station, in part or in full; each "
                   f"delivery is paid at the contract price and returns its share of the deposit. Miss the deadline and "
                   f"you lose the rest of the deposit and pay a penalty of {int(PENALTY * 100)}% of the undelivered value"
                   + (f" ({int(FIRST_PENALTY * 100)}% for your first lapse of the game)" if FIRST_PENALTY != PENALTY else "")
                   + ". "
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


def _standing_line(lane: str, v: dict) -> str:
    """One institution's line: the counter, the goal, the percentage, and what
    the next tier opens. `Sol Freight Guild (freight): 31,200 / 40,000 CR (78%)
    - next: Guild Member (opens 4th ship berth, Guild refit yards)`."""
    from agora import standing as S
    inst = S.INSTITUTIONS[lane]
    head = f"{inst['name']} ({inst['lane_label'].lower()}): "
    held = f"{inst['titles'][v['tier'] - 1]}. " if v['tier'] else ""
    if v['next_tier'] is None:
        return f"{head}{held}{v['lane_profit_cr']:,} CR - top tier reached"
    return (f"{head}{held}{v['lane_profit_cr']:,} / {v['next_threshold_cr']:,} CR ({v['progress_pct']}%) - next: "
            f"{v['next_title']} (opens {inst['opens'][v['next_tier'] - 1]})")


def _standing_section(standing, viewer: Optional[str] = None) -> List[str]:
    """## Institutional standing (#187): the rule in one sentence, then each
    corp's progress list, the viewer's own corp first."""
    from agora import standing as S
    rep = standing.report()
    out = ["## Institutional standing",
           f"Each institution tracks one number, your lifetime net profit in its lane: reach {S.T1_FLOOR:,} CR for "
           f"tier 1 and {S.T2_FLOOR:,} CR for tier 2 ({S.t2_floor('market_making'):,} for market making), and "
           f"the standing is yours for the rest of the game, even if the lane later loses money.",
           f"Lanes: freight (goods sold away from where you bought them, contracts, tolls, fuel), trading (stock "
           f"round trips and borrow fees), market making (goods bought and sold at the same station), covert "
           f"(privateer loot and ransoms, sabotage and wiretaps). Standing opens the institution's lane tech for "
           f"purchase. GalNet reports each admission and the round a corp is halfway to its next tier. "
           f"`GET /referee/standing`",
           ""]
    corps = sorted(rep['corps'])
    if viewer in rep['corps']:
        corps.remove(viewer)
        corps.insert(0, viewer)
    for a in corps:
        out.append(f"**{a}**" + (" (you)" if a == viewer else ""))
        for lane, v in rep['corps'][a]['lanes'].items():
            out.append(f"- {_standing_line(lane, v)}")
        out.append("")
    return out


def _ships_section(ref, board, viewer: Optional[str]) -> List[str]:
    """## Ships: the rules for more than one ship, and every fleet's ships (#175)."""
    from agora import fleet as F
    if getattr(ref, 'fleet', None) is None:
        return []
    prices = ", ".join(f"ship {n}: {p:,} CR" for n, p in sorted(F.SHIP_PRICES.items()))
    out = ["## Ships",
           f"Every fleet starts with one ship and may own up to {F.BASE_SHIP_CAP}; a 4th and 5th need Sol Freight "
           f"Guild standing (Guild Member, then Guild Master: see Standing). Prices: {prices}. Each ship beyond the "
           f"first costs {F.SHIP_UPKEEP} CR a round in upkeep, and counts for {int(F.BOOK_PCT * 100)}% of its price "
           "in net worth.",
           "- Buy one while docked: `POST /referee/vessels/buy` (optional `vessel_id`: the ship that is buying; "
           "the new ship appears where it is, with an empty hold and no FUEL).",
           "- Goods and FUEL are carried by one ship. CR is the fleet's. Orders, MOVEs, offers and deliveries take "
           "`vessel_id` (e.g. `amos/2`); without it they mean ship 1.",
           "- A ship trades only at the station it is docked at, and each ship makes one trip at a time.",
           "- Move goods between two of your ships docked at the same station: `POST /referee/vessels/transfer` "
           "`{\"from\": \"amos/1\", \"to\": \"amos/2\", \"instrument\": \"ORE\", \"qty\": 100}`. "
           "Goods left at a station with no ship of yours under them (a scrapped ship's cargo, a refunded offer) "
           "wait in your station hold `@<station>` until a ship of yours docks there and loads them.",
           f"- A bought ship you no longer want: `POST /referee/vessels/scrap` `{{\"vessel_id\": \"amos/2\"}}` "
           f"while it is docked pays back {int(F.SCRAP_PCT * 100)}% of its price (what net worth already counts it at) "
           "and ends its upkeep; its cargo stays in your station hold.",
           "- `GET /referee/vessels?agent_id=<you>` lists your ships, holds and the next ship's price.",
           ""]
    cap = getattr(ref, 'ship_hold', 0) or 0
    if cap:
        out[-1:] = [
            f"- **Hold size.** Each ship's hold carries at most {cap} cargo units: one unit per FRAG, FOOD or ORE. "
            f"FUEL rides in the ship's tank, up to {F.FUEL_TANK}; only FUEL above that takes hold space. "
            "Cargo on a trip is still aboard. A BUY whose goods would not fit, beside what your ship's other "
            "resting BUYs already keep room for, is rejected (`hold_full`); so is a transfer onto a full ship. "
            "Goods nobody chose the size of (loot, salvage, a refunded or collected offer) fill the ship up and "
            "the rest waits in your station hold `@<station>`. Your station holds have no size limit. "
            "`GET /referee/vessels?agent_id=<you>` shows each ship's `hold_used`, `hold_reserved`, `hold_free` "
            "and `hold_capacity`.",
            ""]
        if viewer and ref.fleet.is_corp(viewer):
            mine = []
            for vid in (s['vessel_id'] for s in ref.fleet.ships(viewer)):
                h = ref.fleet.hold_status(vid)
                mine.append(f"{vid} {h['hold_used']}/{h['hold_capacity']} used, {h['hold_reserved']} kept for "
                            f"resting BUYs, {h['hold_free']} free")
            if mine:
                out[-1:] = [f"Your holds now: {'; '.join(mine)}.", ""]
    multi = [r for r in board if (r.get('ships') or 1) > 1]
    if multi:
        out.append("| Fleet | Ship | Where | FUEL | FRAG | FOOD | ORE | Hold used |")
        out.append("|---|---|---|---|---|---|---|---|")
        for r in multi:
            for sh in _ships(ref, r['agent_id']):
                h = sh['hold']
                used = f"{sh['hold_used']} / {sh['hold_capacity']}" if sh['hold_capacity'] else str(sh['hold_used'])
                out.append(f"| {r['agent_id']} | {sh['vessel_id']} | {sh['where']} | {h['FUEL']} | {h['FRAG']} | "
                           f"{h['FOOD']} | {h['ORE']} | {used} |")
        out.append("")
    return out
