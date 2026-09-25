"""
tests/test_xss_terminal_sanitization_264.py - Verification for Issue #264:
Stored XSS mitigation in GalNet wire renderer and terminal sinks.
"""

import json
import re
import subprocess
import unittest

from agora.referee import AgoraReferee


class TestXSSMitigation264(unittest.TestCase):
    def setUp(self):
        self.ref = AgoraReferee(events=True, corporate=True)
        # Fund actor for covert operations
        with self.ref.conn:
            self.ref.conn.execute("UPDATE accounts SET balance = 5000 WHERE agent_id = 'amos' AND instrument = 'CR'")

    def test_covert_plant_rumor_strips_html_tags(self):
        """Verify plant_rumor strips angle brackets from headline and body as defense-in-depth."""
        xss_headline = "<script>alert('XSS')</script> CERES FUEL CRISIS"
        xss_body = "Warning: <img src=x onerror=alert(1)> Market collapsing at <a href='javascript:evil()'>dock</a>!"

        res = self.ref.covert.plant_rumor(
            actor='amos',
            station_id='ceres',
            commodity='FUEL',
            direction='bullish',
            headline=xss_headline,
            body=xss_body,
        )

        self.assertEqual(res.get('kind'), 'rumor_ok', res)
        stored_headline = res['payload']['headline']
        stored_body = res['payload']['body']

        # Ensure all angle brackets are stripped
        self.assertNotIn('<', stored_headline)
        self.assertNotIn('>', stored_headline)
        self.assertNotIn('<', stored_body)
        self.assertNotIn('>', stored_body)

        self.assertEqual(stored_headline, "scriptalert('XSS')/script CERES FUEL CRISIS")
        self.assertIn("Warning: img src=x onerror=alert(1) Market collapsing at", stored_body)

    def test_covert_plant_rumor_empty_tags_fallback(self):
        """Verify that passing only angle brackets <> falls back to default generated headline/body."""
        res = self.ref.covert.plant_rumor(
            actor='amos',
            station_id='ceres',
            commodity='FUEL',
            direction='bullish',
            headline="<><><>",
            body="<><>",
        )

        self.assertEqual(res.get('kind'), 'rumor_ok', res)
        # Empty string after stripping should fall back to default template
        self.assertTrue(res['payload']['headline'].startswith("UNVERIFIED REPORTS:"))
        self.assertTrue(res['payload']['body'].startswith("GalNet anonymous dispatches allege"))

    def test_terminal_html_contains_escape_html_function(self):
        """Verify public/terminal.html defines escapeHtml correctly and executes in JS engine."""
        with open("public/terminal.html", "r", encoding="utf-8") as f:
            html = f.read()

        self.assertIn("function escapeHtml(val)", html)

        # Extract escapeHtml function using regex
        match = re.search(r"function escapeHtml\(val\)\s*\{[\s\S]*?\n  \}", html)
        self.assertIsNotNone(match, "Could not find escapeHtml definition in terminal.html")
        fn_code = match.group(0)

        # Test execution in Node.js
        test_script = f"""
{fn_code}

const tests = [
  [null, ''],
  [undefined, ''],
  ['<script>alert(1)</script>', '&lt;script&gt;alert(1)&lt;/script&gt;'],
  ['"test" & \\'val\\'', '&quot;test&quot; &amp; &#039;val&#039;'],
  [123, '123'],
  ['Safe string', 'Safe string']
];

for (const [input, expected] of tests) {{
  const actual = escapeHtml(input);
  if (actual !== expected) {{
    console.error(`Mismatch for ${{input}}: expected "${{expected}}", got "${{actual}}"`);
    process.exit(1);
  }}
}}
console.log("ALL_JS_TESTS_PASS");
"""
        proc = subprocess.run(["node", "-e", test_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        self.assertIn("ALL_JS_TESTS_PASS", proc.stdout)

    def test_terminal_html_escapes_galnet_and_sinks(self):
        """Verify all identified innerHTML sinks in public/terminal.html utilize escapeHtml."""
        with open("public/terminal.html", "r", encoding="utf-8") as f:
            html = f.read()

        # GalNet wire renderer
        self.assertIn("const headline = escapeHtml(item.headline || '');", html)
        self.assertIn("const body = escapeHtml(item.body || '');", html)
        self.assertIn("${headline}", html)
        self.assertIn("${body}", html)

        # Tape Feed news
        self.assertIn("const headline = escapeHtml(p.headline || \"BREAKING NEWS\");", html)

        # Leaderboard
        self.assertIn("const agentDisp = escapeHtml((a.agent_id || \"\").toUpperCase());", html)

        # Manifest
        self.assertIn("const transitId = escapeHtml((t.transit_id || 'TX-?').slice(0, 12));", html)

        # Distress Beacons & RFQs & Claims
        self.assertIn("const beaconId = escapeHtml((b.beacon_id || '').slice(0, 16));", html)
        self.assertIn("const rfqId = escapeHtml((rfq.rfq_id || '').slice(0, 16));", html)
        self.assertIn("const claimId = escapeHtml((c.claim_id || '').slice(0, 16));", html)


if __name__ == '__main__':
    unittest.main()
