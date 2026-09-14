# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
recipe.py — two things: an ephemeral-clone helper for one-off git work
(the general fix), and a guard for a persistent clone that should only
ever be fast-forwarded (the specific fix for the deploy-cron scar).

Run directly: `uv run recipe.py --demo` clones this pattern's own repo
into a throwaway temp dir and shows the guard tripping on a clean clone,
then not tripping after a simulated off-branch checkout.

No dependencies beyond the stdlib and a `git` on PATH.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def ephemeral_clone(remote_url: str, ref: str = "main") -> Path:
    """Clone `remote_url` at `ref` into a fresh temp dir. Caller owns cleanup.

    Use this — never a cron's own persistent clone — for any one-off
    checkout-and-push: opening a PR branch, testing a rebase, anything
    exploratory. The clone is used once and discarded, so nothing else
    that depends on that clone's HEAD staying on `ref` can be disturbed.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ephemeral-clone-"))
    subprocess.run(
        ["git", "clone", "--branch", ref, "--single-branch", remote_url, str(tmp)],
        check=True, capture_output=True,
    )
    return tmp


def assert_on_expected_branch(clone_dir: Path, expected: str = "main") -> None:
    """Refuse instead of guessing when a persistent clone has drifted.

    Call this at the top of any script that treats `clone_dir` as a
    standing, fast-forward-only working copy (a deploy cron's own clone,
    for example) before doing a pull-and-deploy cycle. Raises instead of
    silently no-op'ing the way the original fast-forward guard did.
    """
    result = subprocess.run(
        ["git", "-C", str(clone_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    current = result.stdout.strip()
    if current != expected:
        raise RuntimeError(
            f"{clone_dir} is on {current!r}, expected {expected!r} — "
            f"something (a one-off checkout, a manual push) moved this "
            f"persistent clone off the branch it's supposed to track. "
            f"Fix the clone (git checkout {expected}) before pulling, "
            f"don't let the fast-forward step silently no-op."
        )


def _demo() -> None:
    here = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
    ).stdout.strip()
    clone = ephemeral_clone(here, ref="main")
    print(f"ephemeral clone at {clone}")
    assert_on_expected_branch(clone, expected="main")
    print("guard passed: clone is on main, as expected")

    # Simulate the scar: check out something else in what would otherwise
    # be treated as the cron's persistent clone.
    subprocess.run(["git", "-C", str(clone), "checkout", "-b", "someone-elses-pr"], check=True, capture_output=True)
    try:
        assert_on_expected_branch(clone, expected="main")
        print("BUG: guard should have raised here")
        sys.exit(1)
    except RuntimeError as e:
        print(f"guard correctly refused: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="run the self-contained demonstration")
    args = parser.parse_args()
    if args.demo:
        _demo()
    else:
        parser.print_help()
