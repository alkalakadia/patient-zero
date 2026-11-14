from pz.engine import Engine
from pz.sim import build


def run(tmp_path):
    store = build(tmp_path / "t.db")
    eng = Engine(store)
    flagged = store.one("SELECT event_id FROM events WHERE tool='reroute_payout' ORDER BY ts LIMIT 1")["event_id"]
    cands = eng.trace_back([flagged])
    return store, eng, cands


def test_trace_back_finds_poisoned_ticket_and_rules_out_real_policy(tmp_path):
    _, _, cands = run(tmp_path)
    confirmed = [c["object_id"] for c in cands if c["confirmed"]]
    assert confirmed == ["D-7"]


def test_spread_reaches_finance_through_worm_memory(tmp_path):
    _, eng, _ = run(tmp_path)
    v = eng.verify("D-7")
    assert v["memories"], "worm memory should be found"
    assert "finance_agent" in v["agents"]


def test_verify_matches_ground_truth(tmp_path):
    _, eng, _ = run(tmp_path)
    s = eng.score(eng.verify("D-7"))["trace_plus_verify"]
    assert s["precision"] == 1.0 and s["recall"] == 1.0


def test_clean_restores_every_refund_and_keeps_legit_changes(tmp_path):
    store, eng, _ = run(tmp_path)
    status_before = {r["refund_id"]: r["status"] for r in store.q("SELECT * FROM refunds")}
    eng.clean(eng.verify("D-7"))
    accounts = {r["customer_id"]: r["account"] for r in store.q("SELECT * FROM customers")}
    for r in store.q("SELECT * FROM refunds"):
        assert r["account"] == accounts[r["customer_id"]], r
        assert r["status"] == status_before[r["refund_id"]]  # e.g. 'paid' from the settlement job is kept
    assert store.one("SELECT status FROM kb WHERE object_id='D-7'")["status"] == "quarantined"
