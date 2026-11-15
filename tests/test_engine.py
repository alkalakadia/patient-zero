import json

import pytest

from pz.engine import Engine
from pz.scenarios import SCENARIOS
from pz.sim import build


def flagged_events(store):
    owned = {r["value"] for r in store.q("SELECT value FROM entities")}
    return [e["event_id"] for e in store.q("SELECT * FROM events WHERE tool IN ('create_item','change_value') ORDER BY ts")
            if json.loads(e["payload"]).get("value") not in owned]


def run(tmp_path, scenario, attack=None):
    store = build(tmp_path / "t.db", scenario=scenario, attack=attack)
    eng = Engine(store)
    alerts = flagged_events(store)
    reviewer = [e for e in alerts if eng.event[e]["tool"] == "change_value"]
    return store, eng, eng.trace_back([(reviewer or alerts)[0]])


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_finds_planted_source_and_rules_out_real_policy(tmp_path, scenario):
    _, eng, cands = run(tmp_path, scenario)
    root = cands[0]
    assert root["confirmed"] and root["object_id"] in ("D-7", "M-7")
    assert not any(c["confirmed"] for c in cands if c["object_id"].startswith("D-") and c["object_id"] != "D-7")


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_spread_reaches_reviewer_through_worm_memory(tmp_path, scenario):
    _, eng, cands = run(tmp_path, scenario)
    v = eng.verify(cands[0]["object_id"])
    assert v["memories"]
    assert SCENARIOS[scenario]["agents"]["reviewer"][0] in v["agents"]


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_verify_matches_ground_truth(tmp_path, scenario):
    _, eng, cands = run(tmp_path, scenario)
    s = eng.score(eng.verify(cands[0]["object_id"]))["trace_plus_verify"]
    assert s["precision"] == 1.0 and s["recall"] == 1.0


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_clean_restores_every_item_and_keeps_legit_changes(tmp_path, scenario):
    store, eng, cands = run(tmp_path, scenario)
    status_before = {r["item_id"]: r["status"] for r in store.q("SELECT * FROM items")}
    eng.clean(eng.verify(cands[0]["object_id"]))
    truth = {r["entity_id"]: r["value"] for r in store.q("SELECT * FROM entities")}
    for r in store.q("SELECT * FROM items"):
        assert r["value"] == truth[r["entity_id"]], r
        assert r["status"] == status_before[r["item_id"]]  # e.g. 'final' from the nightly job is kept


def test_custom_attack_without_worm_stays_with_primary_agent(tmp_path):
    _, eng, cands = run(tmp_path, "inbox", {"worm": False, "threshold": 4000, "day": 4})
    v = eng.verify(cands[0]["object_id"])
    assert not v["memories"]
    assert eng.score(v)["trace_plus_verify"]["recall"] == 1.0


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_majority_vote_survives_flaky_replays(scenario):
    from pz.evals import one
    for seed in range(1000, 1004):
        r = one(scenario, seed, {"worm": True}, flaky=0.25, votes=3)
        assert r["root_correct"] and r["precision"] == 1.0 and r["recall"] == 1.0, r


def test_eval_clean_week_raises_no_alarm():
    from pz.evals import one
    assert one("inbox", 5000, {"day": 99})["alerts"] == 0
