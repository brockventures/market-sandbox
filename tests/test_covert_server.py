"""
tests/test_covert_server.py - HTTP integration tests for Covert Ops & Rivalry routes.
"""

import json
import unittest

from agora.server import build_referee_from_env
from agora.referee import AgoraReferee


class TestCovertServerRoutes(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(events=True, corporate=True, piracy='0.3,0.1')
        self.covert = self.ref.covert

    def test_wiretap_and_intel_endpoint_methods(self):
        # Direct verification of server-facing handler logic
        res = self.covert.plant_wiretap('zero', 'amos')
        self.assertEqual(res['kind'], 'wiretap_ok')

        active = self.covert.active_wiretaps('zero')
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['target'], 'amos')

        intel = self.covert.get_intel('zero', 'amos')
        self.assertEqual(intel['kind'], 'intel_ok')
        self.assertEqual(intel['payload']['target'], 'amos')

    def test_sabotage_and_rivalry_endpoint_methods(self):
        sab = self.covert.execute_sabotage('zero', 'amos', mode='docked')
        self.assertEqual(sab['kind'], 'sabotage_ok')

        rivalry = self.covert.rivalry_scoreboard('zero')
        self.assertIn('rivalries', rivalry)
        self.assertIn('hostility', rivalry)


if __name__ == '__main__':
    unittest.main()
