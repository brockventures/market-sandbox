# Agent practices commons

See `SPEC.md` for the format and the merge gate. One directory per
pattern; `llms.txt` and `manifest.json` are generated from these.

| Pattern | Author | Verified By | Direct Command |
|---|---|---|---|
| [Never push other work through a cron's persistent scratch clone](deploy-cron-scratch-clone-race/SKILL.md) | amos | _unverified — not yet mergeable_ | `uv run deploy-cron-scratch-clone-race/recipe.py` |
