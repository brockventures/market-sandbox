#!/usr/bin/env python3
"""
patterns/generate_catalog.py — regenerates manifest.json, llms.txt, and the
table in README.md from each <name>/SKILL.md's frontmatter. Run on merge
to main by CI; none of the three outputs are hand-edited.

    python3 patterns/generate_catalog.py
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from validate import parse_frontmatter  # noqa: E402

ROOT = pathlib.Path(__file__).parent


def collect() -> list:
    entries = []
    for entry in sorted(ROOT.iterdir()):
        skill = entry / "SKILL.md"
        if not entry.is_dir() or not skill.exists():
            continue
        fm = parse_frontmatter(skill.read_text())
        fm["name"] = entry.name
        fm["has_recipe"] = (entry / "recipe.py").exists()
        entries.append(fm)
    return entries


def write_manifest(entries: list) -> None:
    (ROOT / "manifest.json").write_text(json.dumps({"patterns": entries}, indent=2) + "\n")


def write_llms_txt(entries: list) -> None:
    lines = ["# market-sandbox agent patterns", "", "Compact index. Fetch <name>/SKILL.md for the full entry.", ""]
    for e in entries:
        triggers = ", ".join(e.get("triggers") or [])
        lines.append(f"- {e['name']}: {e.get('title', '')} (triggers: {triggers})")
    (ROOT / "llms.txt").write_text("\n".join(lines) + "\n")


def write_readme(entries: list) -> None:
    lines = [
        "# Agent practices commons",
        "",
        "See `SPEC.md` for the format and the merge gate. One directory per",
        "pattern; `llms.txt` and `manifest.json` are generated from these.",
        "",
        "| Pattern | Author | Verified By | Direct Command |",
        "|---|---|---|---|",
    ]
    for e in entries:
        cmd = f"`uv run {e['name']}/recipe.py`" if e.get("has_recipe") else "—"
        verified = e.get("verified_by") or "_unverified — not yet mergeable_"
        lines.append(f"| [{e.get('title', e['name'])}]({e['name']}/SKILL.md) | {e.get('author', '')} | {verified} | {cmd} |")
    (ROOT / "README.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    entries = collect()
    write_manifest(entries)
    write_llms_txt(entries)
    write_readme(entries)
    print(f"generated catalog for {len(entries)} pattern(s)")


if __name__ == "__main__":
    main()
