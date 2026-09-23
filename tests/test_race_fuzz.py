"""
Smoke test for tools/race_fuzz.py, the concurrent ledger fuzzer.

Small and fast (a few seconds): it proves the harness runs end to end and
that its checker actually catches a broken ledger. It does not assert that a
concurrent run is clean -- the tool exists to find races, and a race it finds
must not turn CI intermittently red. Run the tool itself at size for that.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "race_fuzz.py"


def run_fuzz(*args, timeout=120):
    # Play the default all-features game regardless of the caller's AGORA_* env.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGORA_")}
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=timeout)


class TestRaceFuzzSmoke(unittest.TestCase):
    def test_serial_run_is_clean(self):
        r = run_fuzz("--seed", "7", "--serial", "--ops", "300", "--rounds", "6", "--check-every", "100")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertIn("CLEAN: seed=7", r.stdout)
        self.assertIn("coverage:", r.stdout)

    def test_concurrent_run_completes(self):
        # 0 = clean, 1 = the fuzzer found something. 2 would be the harness itself breaking.
        r = run_fuzz("--seed", "7", "--threads", "4", "--ops", "300", "--rounds", "6", "--check-every", "100")
        self.assertIn(r.returncode, (0, 1), r.stdout[-3000:] + r.stderr[-3000:])
        self.assertIn("ops=", r.stdout)

    def test_checker_catches_injected_corruption(self):
        r = run_fuzz("--seed", "7", "--serial", "--ops", "200", "--rounds", "4", "--check-every", "50",
                     "--inject-corruption", "60")
        self.assertEqual(r.returncode, 1, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertIn("VIOLATION", r.stdout)
        self.assertIn("seed=7", r.stdout)
        self.assertIn("Reconciliation breach: amos CR", r.stdout)
        self.assertIn("replay: ", r.stdout)
        self.assertIn("last 15 ops per thread", r.stdout)


if __name__ == "__main__":
    unittest.main()
