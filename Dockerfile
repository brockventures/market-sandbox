FROM python:3.13-slim

WORKDIR /app

# No pip dependencies by design: agora/server.py is pure standard library.
# Nothing to install, so nothing to audit or pin here.
COPY agora/ ./agora/
COPY db/ ./db/

# Bind to all interfaces inside the container. The application default stays
# 127.0.0.1 (deliberate, see PR #7) so this exposure is explicit and lives in
# the deployment config rather than the code.
ENV AGORA_HOST=0.0.0.0

# NO persistence is configured here, deliberately. run_server() constructs
# AgoraReferee() with no db_path, so the ledger is ':memory:' and every
# redeploy resets all balances to genesis. That is loud and recoverable.
#
# Do NOT "fix" this by adding AGORA_DB_PATH and a volume on its own. self.book
# is never rehydrated from the orders table at startup, so a persistent DB
# would come back with orders marked open and an empty in-memory book. Escrow
# reads self.book, so that agent's committed exposure silently vanishes and
# they can re-sell inventory already spoken for — the round-26 oversell,
# arriving through the redeploy path. Reproduced 2026-09-04.
#
# Persistence lands once _init_db rebuilds the book from status='open' orders
# in price-time priority before the listener binds. Then wire both together.

EXPOSE 8080

# Shell form so $PORT (injected by the platform) is expanded at runtime, with
# 8080 as the fallback for a plain `docker run`.
CMD python -m agora.server ${PORT:-8080}
