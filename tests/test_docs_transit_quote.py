"""Docs cover the transit quote route and every field the code really returns (#284)."""
import pathlib
import re

from agora.referee import AgoraReferee
from tests.test_docs_parity import registered_routes

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = {p: (ROOT / p).read_text() for p in (
    "public/documentation.html", "docs/wire-spec.md", "docs/game-guide.md")}


def _words(text):
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))


def _quote_and_receipt():
    ref = AgoraReferee(piracy="0.15,0.04", hazards="0.20,0.25")
    ref.new_game(seed=42, warmup_rounds=2, hazards="0.20,0.25")
    st = ref.get_vessel_location("amos")["station_id"]
    dest = "ceres" if st != "ceres" else "earth"
    with ref.lock, ref.conn:
        ref.fleet._move("doc-give-fuel", (("amos/1", "FUEL", 500), ("SYSTEM", "FUEL", -500)))
    quote = ref.initiate_transit(agent_id="amos", destination=dest, cargo_qty=5, dry_run=True)
    receipt = ref.initiate_transit(agent_id="amos", destination=dest, cargo_qty=5)
    return quote, receipt


def test_quote_routes_registered_and_documented():
    routes = registered_routes()
    for r in ("/stations/transit/quote", "/referee/transit/quote", "/referee/piracy/quote"):
        assert r in routes, r
        for name, text in DOCS.items():
            if name != "docs/game-guide.md":
                assert r in text, (r, name)


def test_quote_fields_are_documented():
    quote, _ = _quote_and_receipt()
    assert quote["kind"] == "transit_quote"
    keys = set(quote["payload"]) | set(quote["payload"]["piracy"]) | set(quote["payload"]["hazard"])
    keys |= set(quote) - {"payload", "v"}
    for name in ("public/documentation.html", "docs/wire-spec.md"):
        missing = sorted(keys - _words(DOCS[name]))
        assert not missing, (name, missing)


def test_receipt_fields_are_documented():
    _, receipt = _quote_and_receipt()
    keys = set(receipt["payload"]) | (set(receipt) - {"payload", "v"})
    for name in ("public/documentation.html", "docs/wire-spec.md"):
        missing = sorted(keys - _words(DOCS[name]))
        assert not missing, (name, missing)
