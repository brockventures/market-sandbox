"""Docs cover #301's in-flight transit masking with the code's real key set."""
from agora.fog import FogEngine
from tests.test_docs_transit_quote import DOCS, _words

TICK = {"seq": 1, "kind": "transit", "payload": {
    "transit_id": "t1", "agent_id": "zero", "vessel_id": "zero/1", "origin": "ceres",
    "destination": "earth", "commodity": "FOOD", "cargo_qty": 5, "departure_round": 3,
    "arrival_round": 6, "fuel_burned": 9,
    "piracy": {"demand": {"ransom": 10, "deadline": 7}}}}


def test_masked_allowlist_and_demand_are_documented():
    out = FogEngine.mask_transit_ticks([TICK], viewer="amos", cur_round=4)[0]["payload"]
    assert out["piracy"] == {"demand": {"pending": True, "deadline": 7}}
    keys = set(out) | set(out["piracy"]["demand"])
    for name, text in DOCS.items():
        # The game guide is prose: it names the shape, not every id field.
        need = {"in_flight", "pending", "departure_round", "arrival_round"} if name.endswith("game-guide.md") else keys
        missing = sorted(need - _words(text))
        assert not missing, (name, missing)


def test_arrival_unmasks_and_owner_is_exempt():
    for viewer, rnd in (("amos", 6), ("zero", 4)):
        full = FogEngine.mask_transit_ticks([TICK], viewer=viewer, cur_round=rnd)[0]["payload"]
        assert full["destination"] == "earth"


def test_stealth_flag_documented():
    for name, text in DOCS.items():
        assert "--stealth" in text and "AGORA_STEALTH" in text, name
