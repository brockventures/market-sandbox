import pytest
from tools.agora_announcer import (
    parse_discord_peer,
    PEER_OFFER_PATTERN,
    PEER_ACCEPT_PATTERN,
    PEER_CANCEL_PATTERN
)
from agora.referee import AgoraReferee

def test_peer_command_parsing():
    # Offer
    cmd1 = parse_discord_peer("OFFER 50 FOOD @ 35 AT CERES", "1542081375287640084", "zero")
    assert cmd1 == {
        "action": "offer",
        "agent_id": "zero",
        "station_id": "ceres",
        "instrument": "FOOD",
        "qty": 50,
        "price": 35
    }

    # Offer case insensitive and without @ symbol
    cmd2 = parse_discord_peer("offer 25 fuel 12 at mars", "1541205716948353074", "amos")
    assert cmd2 == {
        "action": "offer",
        "agent_id": "amos",
        "station_id": "mars",
        "instrument": "FUEL",
        "qty": 25,
        "price": 12
    }

    # Accept
    cmd3 = parse_discord_peer("ACCEPT x1a2b3", "1542081375287640084", "zero")
    assert cmd3 == {
        "action": "accept",
        "agent_id": "zero",
        "escrow_id": "x1a2b3"
    }

    # Cancel
    cmd4 = parse_discord_peer("CANCEL x1a2b3", "1542081375287640084", "zero")
    assert cmd4 == {
        "action": "cancel",
        "agent_id": "zero",
        "escrow_id": "x1a2b3"
    }

    cmd5 = parse_discord_peer("CANCEL OFFER x999", "1542081375287640084", "zero")
    assert cmd5 == {
        "action": "cancel",
        "agent_id": "zero",
        "escrow_id": "x999"
    }

    # Non-peer messages return None
    assert parse_discord_peer("BUY 10 FRAG @ 15 AT CERES", "123", "someone") is None
    assert parse_discord_peer("MOVE TO MARS WITH 10 FOOD", "123", "someone") is None


def test_referee_order_receipt_payload():
    ref = AgoraReferee()
    
    # 1. Resting order
    ask_env = {
        'v': 1, 'kind': 'order',
        'payload': {
            'order_id': 'test-ask-101', 'agent_id': 'amos', 'instrument': 'FRAG',
            'side': 'ask', 'qty': 50, 'limit_price': 25, 'seq_seen': ref.current_seq
        }
    }
    res1 = ref.submit_envelope(ask_env)
    assert res1['kind'] == 'market_tick'
    p1 = res1['payload']
    assert p1['order_id'] == 'test-ask-101'
    assert p1['order_status'] == 'resting'
    assert p1['filled_qty'] == 0
    assert p1['remaining_qty'] == 50
    assert p1['trades_count'] == 0

    # 2. Partially filled order
    bid_partial = {
        'v': 1, 'kind': 'order',
        'payload': {
            'order_id': 'test-bid-partial', 'agent_id': 'zero', 'instrument': 'FRAG',
            'side': 'bid', 'qty': 70, 'limit_price': 25, 'seq_seen': ref.current_seq
        }
    }
    res2 = ref.submit_envelope(bid_partial)
    assert res2['kind'] == 'market_tick'
    p2 = res2['payload']
    assert p2['order_id'] == 'test-bid-partial'
    assert p2['order_status'] == 'partially_filled'
    assert p2['filled_qty'] == 50
    assert p2['remaining_qty'] == 20
    assert p2['trades_count'] == 1

    # 3. Completely filled order
    ask_fill = {
        'v': 1, 'kind': 'order',
        'payload': {
            'order_id': 'test-ask-fill', 'agent_id': 'amos', 'instrument': 'FRAG',
            'side': 'ask', 'qty': 20, 'limit_price': 25, 'seq_seen': ref.current_seq
        }
    }
    res3 = ref.submit_envelope(ask_fill)
    assert res3['kind'] == 'market_tick'
    p3 = res3['payload']
    assert p3['order_id'] == 'test-ask-fill'
    assert p3['order_status'] == 'filled'
    assert p3['filled_qty'] == 20
    assert p3['remaining_qty'] == 0
    assert p3['trades_count'] == 1
