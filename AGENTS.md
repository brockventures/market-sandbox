# AGENTS.md

Market Sandbox: "Station Agora", a multi-agent trading game for Crab Cavern. A stdlib Python HTTP referee (`agora/`) plus a Discord announcer/order router (`tools/agora_announcer.py`).

## Run and test
- Server: `python3 -m agora.server` (the `Procfile` web process; deployed to Railway).
- Tests (as CI does): `python -m unittest discover -s tests -v`. Scoped: `python3 -m pytest tests -k "announcer or docs" -q`.
- CI also runs `python tools/fuzz_harness.py 1500`; economy changes must pass `tools/band_check.py` (see `.github/workflows/balance-band.yml`).

## Where things live
- `agora/`: game logic; HTTP routes are hand-registered in `agora/server.py`.
- `tools/`: announcer (Discord bot), `market_standup.py`, sims and fuzzers.
- `public/`: web terminal, orrery, and `documentation.html` (every server route must appear there; `tests/test_docs_parity.py` enforces it).
- `docs/`: game guide, rules of engagement, wire spec, ledger schema, standup schedule, merge authority.

## Conventions
- Discord: AGORA game traffic is in `#agora` (channel id default in `tools/agora_announcer.py`, override with `AGORA_DISCORD_CHANNEL_ID` or `--channel`). Standups go to `#agent-chat`.
- Merge rules: see `docs/merge-authority.md`.
- Never commit tokens; the announcer reads them from `.env`/environment.
