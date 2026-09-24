#!/usr/bin/env python3
"""Balance band gate: every play style's median must sit inside the band.

Runs the agreed merge-gate simulation (styles scenario, flat genesis, strict
mode, seeds 1-40) and fails if any style's median final value falls outside
[--low, --high]. p10 and p90 are printed but not gated; p90 may exceed the
ceiling by agreement.

    python tools/band_check.py                 # the CI gate
    python tools/band_check.py --seeds 8       # a quick local canary
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROW = re.compile(r'^\|\s*([a-z_]+)\s*\|\s*\d+\s*\|\s*([+-]?[\d,]+)\s*\|\s*([+-]?[\d,]+)\s*\|\s*([+-]?[\d,]+)\s*\|')


def parse_styles_table(text):
    """Return {style: (median, p10, p90)} from economy_sim's --styles table."""
    out = {}
    for line in text.splitlines():
        m = ROW.match(line.strip())
        if m:
            med, p10, p90 = (int(g.replace(',', '')) for g in m.groups()[1:])
            out[m.group(1)] = (med, p10, p90)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--low', type=int, default=100_000)
    ap.add_argument('--high', type=int, default=180_000)
    ap.add_argument('--seeds', type=int, default=40)
    ap.add_argument('--seed-start', type=int, default=1)
    ap.add_argument('--jobs', type=int, default=os.cpu_count() or 1)
    ap.add_argument('--summary', help='append a markdown report to this file (e.g. $GITHUB_STEP_SUMMARY)')
    args = ap.parse_args(argv)

    cmd = [sys.executable, os.path.join(ROOT, 'tools', 'economy_sim.py'),
           '--scenario', 'styles', '--genesis', 'flat', '--mode', 'strict',
           '--seeds', str(args.seeds), '--seed-start', str(args.seed_start),
           '--styles', '--jobs', str(args.jobs)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        print(f'band_check: economy_sim exited {proc.returncode}')
        return 2
    styles = parse_styles_table(proc.stdout)
    if not styles:
        sys.stderr.write(proc.stdout)
        print('band_check: no styles table in economy_sim output')
        return 2

    lines = [f'### Balance band: medians in {args.low:,}-{args.high:,} '
             f'(styles, flat strict, seeds {args.seed_start}-{args.seed_start + args.seeds - 1})', '',
             '| style | median | p10 | p90 | result |', '|---|---|---|---|---|']
    failed = []
    for style, (med, p10, p90) in sorted(styles.items(), key=lambda kv: -kv[1][0]):
        ok = args.low <= med <= args.high
        if not ok:
            failed.append(style)
        lines.append(f'| {style} | {med:,} | {p10:,} | {p90:,} | {"ok" if ok else "OUT OF BAND"} |')
    lines += ['', f'**FAIL:** {", ".join(failed)} outside the band.' if failed else '**PASS**', '']
    report = '\n'.join(lines)
    print(report)
    if args.summary:
        with open(args.summary, 'a') as fh:
            fh.write(report + '\n')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
