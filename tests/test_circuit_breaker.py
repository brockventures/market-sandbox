"""
tests.test_circuit_breaker - Unit and integration tests for dynamic LULD bands,
discrete 2-round halts, and call auction reopens (Issue #24).
"""

import unittest
from agora.order_book import Order, OrderBook
from agora.circuit_breaker import CircuitBreakerEngine, find_clearing_price
from agora.referee import AgoraReferee



class TestCircuitBreaker(unittest.TestCase):
    def test_find_clearing_price(self):
        # Setup crossed book:
        # Bids: 50 @ 22, 30 @ 20, 20 @ 18
        # Asks: 40 @ 19, 40 @ 21
        bids = [
            Order(order_id='b1', agent_id='amos', instrument='FRAG', side='bid', qty=50, limit_price=22, seq_seen=0),
            Order(order_id='b2', agent_id='zero', instrument='FRAG', side='bid', qty=30, limit_price=20, seq_seen=0),
            Order(order_id='b3', agent_id='marvin', instrument='FRAG', side='bid', qty=20, limit_price=18, seq_seen=0),
        ]
        asks = [
            Order(order_id='a1', agent_id='marvin', instrument='FRAG', side='ask', qty=40, limit_price=19, seq_seen=0),
            Order(order_id='a2', agent_id='amos', instrument='FRAG', side='ask', qty=40, limit_price=21, seq_seen=0),
        ]

        clearing_price, max_vol = find_clearing_price(bids, asks, ref_price=20.0)
        # At price 21, demand is 50 (b1), supply is 80 (a1+a2) -> 50 executed
        # At price 22, demand is 50, supply is 80 -> 50 executed
        # 21 is closer to ref_price 20.0, so 21 is chosen
        assert max_vol == 50
        assert clearing_price == 21


    def test_circuit_breaker_bands_and_vwap(self):
        ref = AgoraReferee()
        cb = ref.circuit_breaker

        # Initial Ceres FRAG VWAP from spot price (22.0)
        bands = cb.get_bands('ceres', 'FRAG')
        assert bands['station_id'] == 'ceres'
        assert bands['instrument'] == 'FRAG'
        assert bands['status'] == 'open'
        assert bands['vwap'] == 22.0
        # +-10% of 22.0 = [19.8, 24.2]
        assert bands['lower_limit'] == 19.8
        assert bands['upper_limit'] == 24.2


    def test_in_band_execution_updates_vwap(self):
        ref = AgoraReferee()

        # Initial Ceres FRAG ask at 22 (within band [19.8, 24.2])
        ask_res = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'ask-c-1',
                'agent_id': 'amos',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'ask',
                'qty': 10,
                'limit_price': 22,
                'seq_seen': 0
            }
        })
        assert ask_res['payload']['best_ask'] == 22

        # In-band bid at 22 executes cleanly
        bid_res = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'bid-c-1',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'bid',
                'qty': 10,
                'limit_price': 22,
                'seq_seen': 0
            }
        })
        assert bid_res['payload']['trades_count'] == 1
        assert bid_res['payload']['last_price'] == 22

        # VWAP updated with trade at 22
        assert ref.circuit_breaker.get_vwap('ceres', 'FRAG') == 22.0


    def test_out_of_band_breach_triggers_2_round_halt(self):
        ref = AgoraReferee()

        # Place ask at 30 (which is outside Ceres FRAG upper limit 24.2)
        # Asking does not execute immediately, rests in book
        ask_high = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'ask-spike-1',
                'agent_id': 'amos',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'ask',
                'qty': 10,
                'limit_price': 30,
                'seq_seen': 0
            }
        })
        assert ask_high['payload']['best_ask'] == 30

        # Aggressive bid at 30 crosses the ask at 30, which breaches upper band (24.2)
        bid_breach = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'bid-spike-1',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'bid',
                'qty': 10,
                'limit_price': 30,
                'seq_seen': 0
            }
        })

        # Circuit breaker triggers!
        assert bid_breach['kind'] == 'status'
        assert bid_breach['status'] == 'circuit_breaker_halted'
        assert bid_breach['floor'] == 'halted'
        assert bid_breach['payload']['trigger_price'] == 30
        assert bid_breach['payload']['halt_round'] == 0
        assert bid_breach['payload']['reopen_round'] == 2  # Discrete 2-round halt

        # Book is now halted
        assert ref.circuit_breaker.is_halted('ceres', 'FRAG') is True
        bands = ref.circuit_breaker.get_bands('ceres', 'FRAG')
        assert bands['status'] == 'halted'
        assert bands['halt_info'] is not None


    def test_halted_book_accepts_auction_orders_and_reopens(self):
        ref = AgoraReferee()

        # 1. Trigger halt on Ceres FRAG via manual halt or spike
        ref.circuit_breaker.trigger_halt(
            station_id='ceres',
            instrument='FRAG',
            trigger_price=35.0,
            reason='Test volatility spike',
            current_round=1
        )
        assert ref.circuit_breaker.is_halted('ceres', 'FRAG') is True

        # 2. While halted, orders rest in the book without executing immediate fills (Auction Mode)
        # amos places ask: 20 @ 25 CR
        ask_auc = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'ask-auc-1',
                'agent_id': 'amos',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'ask',
                'qty': 20,
                'limit_price': 25,
                'seq_seen': 0
            }
        })
        assert ask_auc['payload']['status'] == 'halted'
        assert ask_auc['payload']['trades_count'] == 0
        assert ask_auc['payload']['auction_resting'] is True

        # zero places crossing bid: 20 @ 25 CR
        bid_auc = ref.submit_envelope({
            'kind': 'order',
            'payload': {
                'order_id': 'bid-auc-1',
                'agent_id': 'zero',
                'station_id': 'ceres',
                'instrument': 'FRAG',
                'side': 'bid',
                'qty': 20,
                'limit_price': 25,
                'seq_seen': 0
            }
        })
        # Orders did not cross during halt!
        assert bid_auc['payload']['status'] == 'halted'
        assert bid_auc['payload']['trades_count'] == 0
        assert bid_auc['payload']['auction_resting'] is True

        # 3. Round 2: Still halted (halt was round 1, reopen is round 3)
        step2 = ref.step_round(2)
        assert ref.circuit_breaker.is_halted('ceres', 'FRAG') is True
        assert len(step2.get('circuit_breaker_reopens', [])) == 0

        # 4. Round 3: Reopen round reached! Call auction triggers automatically
        step3 = ref.step_round(3)
        assert ref.circuit_breaker.is_halted('ceres', 'FRAG') is False
        reopens = step3.get('circuit_breaker_reopens', [])
        assert len(reopens) == 1
        reopen_rep = reopens[0]
        assert reopen_rep['status'] == 'reopened'
        assert reopen_rep['clearing_price'] == 25
        assert reopen_rep['reopen_volume'] == 20
        assert len(reopen_rep['trades']) == 1

        # 5. Invariant check: ledger conservation maintained
        valid, errors = ref.verify_ledger_invariants()
        assert valid is True, f"Invariant errors: {errors}"


if __name__ == "__main__":
    unittest.main()
