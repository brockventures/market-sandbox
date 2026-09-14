#!/usr/bin/env python3
"""
patterns/validate.py — CI gate for the agent-practices-commons pattern
directory. Run from the repo root: python3 patterns/validate.py

Checks, per <kebab-name>/SKILL.md:
  - frontmatter present and parses as simple `key: value` YAML (no nesting
    beyond flat lists written as `[a, b]` or a `- ` block)
  - required fields present: title, author, verified_by, scar_level,
    triggers, pr_evidence
  - scar_level is one of none|silent|critical
  - verified_by is set and != author (the merge gate)
  - any fenced ```python block in SKILL.md compiles (python3 -m py_compile)
  - recipe.py, if present, compiles and carries a PEP 723 `# /// script`
    header

Exits non-zero on any failure, printing every failure found (not just the
first) so a PR gets one round of fixes instead of a chain of them.
"""
import ast
import pathlib
import py_compile
import re
import sys
import tempfile

REQUIRED_FIELDS = ["title", "author", "verified_by", "scar_level", "triggers", "pr_evidence"]
VALID_SCAR_LEVELS = {"none", "silent", "critical"}
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FENCE_RE = re.compile(r"```(\w+)\n(.*?)\n```", re.S)


def parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
            out[key] = items
        elif val.lower() in ("null", "~", ""):
            out[key] = None
        else:
            out[key] = val.strip('"').strip("'")
    return out


def check_skill(path: pathlib.Path) -> list:
    errors = []
    text = path.read_text()
    fm = parse_frontmatter(text)
    if not fm:
        errors.append(f"{path}: no --- frontmatter block found")
        return errors

    for field in REQUIRED_FIELDS:
        if field not in fm:
            errors.append(f"{path}: missing frontmatter field '{field}'")

    scar = fm.get("scar_level")
    if scar is not None and scar not in VALID_SCAR_LEVELS:
        errors.append(f"{path}: scar_level={scar!r} not in {sorted(VALID_SCAR_LEVELS)}")

    author = fm.get("author")
    verified_by = fm.get("verified_by")
    if not verified_by:
        errors.append(f"{path}: verified_by is unset — gate requires a peer to confirm before merge")
    elif verified_by == author:
        errors.append(f"{path}: verified_by == author ({author!r}) — needs a different peer")

    for lang, code in FENCE_RE.findall(text):
        if lang.lower() != "python":
            continue
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            tmp = f.name
        try:
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"{path}: embedded python fence fails to compile: {e}")
    return errors


def check_recipe(path: pathlib.Path) -> list:
    errors = []
    text = path.read_text()
    if "# /// script" not in text:
        errors.append(f"{path}: missing PEP 723 '# /// script' inline metadata block")
    try:
        ast.parse(text)
    except SyntaxError as e:
        errors.append(f"{path}: syntax error: {e}")
    return errors


def main() -> int:
    root = pathlib.Path(__file__).parent
    errors = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        skill = entry / "SKILL.md"
        if not skill.exists():
            errors.append(f"{entry}: directory has no SKILL.md")
            continue
        errors.extend(check_skill(skill))
        recipe = entry / "recipe.py"
        if recipe.exists():
            errors.extend(check_recipe(recipe))

    if errors:
        print(f"patterns/validate.py: {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("patterns/validate.py: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
