"""Live server defaults: every shipped feature on unless its env var says off."""
import os
import unittest
from unittest import mock

from agora.server import build_referee_from_env


class TestDefaultsOn(unittest.TestCase):
    def test_all_on_by_default_and_survive_bare_new_game(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGORA_')}
        with mock.patch.dict(os.environ, env, clear=True):
            ref = build_referee_from_env(':memory:')
        ref.new_game(seed=1)
        self.assertTrue(ref.depots_enabled)
        self.assertEqual(ref.depot_model, 'reactive')
        self.assertEqual(ref.circuit_breaker.band_pct, 0.25)
        self.assertTrue(ref.peer_trades)

    def test_env_can_turn_off(self):
        with mock.patch.dict(os.environ, {'AGORA_PEER_TRADES': '0', 'AGORA_DEPOT_MODEL': 'static'}):
            ref = build_referee_from_env(':memory:')
        self.assertFalse(ref.peer_trades)
        self.assertEqual(ref.depot_model, 'static')


if __name__ == '__main__':
    unittest.main()
