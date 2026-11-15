import json

import pytest

from pz import incidents
from pz.engine import Engine
from pz.investigator import Toolbox, scripted
from pz.recorder import Recorder, Session
from pz.scenarios import SCENARIOS
from pz.sim import build


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "SETTINGS_PATH", tmp_path / "settings.json")


def desk_for(tmp_path, scenario="inbox", **kw):
    store = build(tmp_path / "t.db", scenario=scenario)
    sc = SCENARIOS[scenario]
    eng = Engine(store)
    desk = incidents.Desk(store, sc, toolbox=lambda: Toolbox(Engine(store), sc, str), driver=scripted, **kw)
    return store, eng, desk


def items_match_truth(store):
    truth = {r["entity_id"]: r["value"] for r in store.q("SELECT * FROM entities")}
    return all(r["value"] == truth[r["entity_id"]] for r in store.q("SELECT * FROM items"))


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_autopilot_groups_alerts_and_fixes_without_a_person(tmp_path, scenario):
    store, _, desk = desk_for(tmp_path, scenario)
    opened = desk.scan(investigate=False)
    assert len(opened) == 1  # every alert shares one flagged value, so they are one incident
    inc = desk.investigate(opened[0])
    assert inc["status"] == "fixed" and inc["root"] in ("D-7", "M-7")
    assert inc["report"]["score"]["precision"] == 1.0 and inc["report"]["score"]["recall"] == 1.0
    assert items_match_truth(store)
    assert any(s["type"] == "decision" and s["by"] == "autopilot" for s in inc["steps"])
    assert not store.q("SELECT * FROM holds WHERE active=1")  # containment lifted once fixed
    assert desk.scan(investigate=False) == []  # already-handled alerts don't reopen it


def test_manual_mode_waits_then_one_click_approves(tmp_path):
    incidents.update_settings(autopilot=False)
    store, _, desk = desk_for(tmp_path)
    iid = desk.scan(investigate=False)[0]
    asked = []
    inc = desk.investigate(iid, ask=lambda i, why: asked.append(why) or True)
    assert asked and "Autopilot is off" in asked[0]
    assert inc["status"] == "fixed" and items_match_truth(store)


def test_dismiss_changes_nothing_and_lifts_containment(tmp_path):
    incidents.update_settings(autopilot=False)
    store, _, desk = desk_for(tmp_path)
    before = store.q("SELECT * FROM items ORDER BY item_id")
    iid = desk.scan(investigate=False)[0]
    assert store.one("SELECT status FROM kb WHERE object_id='D-7'")["status"] == "suspect"
    inc = desk.investigate(iid, ask=lambda i, why: False)
    assert inc["status"] == "dismissed"
    assert store.q("SELECT * FROM items ORDER BY item_id") == before
    assert store.one("SELECT status FROM kb WHERE object_id='D-7'")["status"] == "active"
    assert not store.q("SELECT * FROM holds WHERE active=1")


def test_conflicting_later_change_goes_to_a_person(tmp_path):
    store, _, desk = desk_for(tmp_path)
    bad = store.one("SELECT item_id FROM items WHERE value='BANK-99-4471' LIMIT 1")["item_id"]
    store.write_row("items", bad, {"value": "BANK-10-9999"}, "finance", "manual_fix", 9000)  # someone edited it after
    store.commit()
    iid = desk.scan(investigate=False)[0]
    inc = desk.investigate(iid, ask=lambda i, why: False)
    assert next(s for s in inc["steps"] if s["type"] == "decision")["by"] == "person"
    assert "changed again after the poison" in inc["reason"]


def test_policy_refuses_conflicts_evidence_and_no_proof():
    class TB:
        verdict, plan = {"confirmed": ["e1"]}, {"conflicts": []}
    assert incidents.policy(TB)[0]
    TB.plan = {"conflicts": [{"field": "value"}]}
    assert not incidents.policy(TB)[0]
    TB.plan, TB.verdict = {}, {"confirmed": ["e1"], "by": "evidence"}
    assert not incidents.policy(TB)[0]
    TB.verdict = {"confirmed": []}
    assert not incidents.policy(TB)[0]


def test_undo_restores_rows_and_knowledge_exactly(tmp_path):
    store, _, desk = desk_for(tmp_path)
    before = store.q("SELECT * FROM items ORDER BY item_id")
    iid = desk.scan(investigate=False)[0]
    desk.investigate(iid)
    assert store.one("SELECT status FROM kb WHERE object_id='D-7'")["status"] == "quarantined"
    desk.undo(iid)
    assert store.q("SELECT * FROM items ORDER BY item_id") == before
    assert store.one("SELECT status FROM kb WHERE object_id='D-7'")["status"] == "active"
    assert desk.get(iid, full=False)["status"] == "undone"


def test_containment_hides_knowledge_from_agents_but_not_from_replays(tmp_path):
    store, _, desk = desk_for(tmp_path)
    desk.scan(investigate=False)
    topic = store.one("SELECT topic FROM kb WHERE object_id='D-7'")["topic"]
    live = Session(Recorder(store), "x1", "ap_agent", 9000)
    assert "D-7" not in [r["object_id"] for r in live.retrieve([topic])]
    assert not live.read("D-7")
    replay = Session(Recorder(store), "x2", "ap_agent", 9000)
    replay.dry_run = True
    assert "D-7" in [r["object_id"] for r in replay.retrieve([topic])]


def test_open_incident_holds_writes_carrying_the_value_or_from_exposed_sessions(tmp_path):
    store, _, desk = desk_for(tmp_path)
    iid = desk.scan(investigate=False)[0]
    sess = Recorder(store).start_session("x1", "ap_agent", 9000, {"kind": "test"})
    n = store.one("SELECT COUNT(*) n FROM events")["n"]
    assert sess.tool_call("create_item", "items", "P-999", {"entity_id": "V-100", "value": "BANK-99-4471", "amount": 1}) is None
    assert sess.held and store.get_row("items", "P-999") is None
    sess.context.append("mem_s204")  # read the worm memory earlier in this session, before it was withdrawn
    assert sess.tool_call("approve", "items", "P-001", {"status": "approved"}) is None
    assert store.one("SELECT COUNT(*) n FROM events")["n"] == n
    held = store.q("SELECT * FROM held_actions WHERE incident_id=?", (iid,))
    assert len(held) == 2 and json.loads(held[0]["payload"])["value"] == "BANK-99-4471"
    clean = Recorder(store).start_session("x2", "ap_agent", 9001, {"kind": "test"})
    assert clean.tool_call("approve", "items", "P-002", {"status": "approved"})  # unrelated work goes through


def test_replay_is_never_held(tmp_path):
    store, _, desk = desk_for(tmp_path)
    desk.scan(investigate=False)
    sess = Session(Recorder(store), "x1", "ap_agent", 9000)
    sess.dry_run = True
    sess.tool_call("create_item", "items", "P-999", {"value": "BANK-99-4471"})
    assert sess.actions and not sess.held


def test_per_run_autopilot_override_never_changes_the_saved_setting(tmp_path):
    incidents.update_settings(autopilot=False)  # the person switched autopilot off in the UI
    saved = incidents.SETTINGS_PATH.read_text()
    store, _, desk = desk_for(tmp_path, autopilot=True)  # e.g. a CLI run that wants autopilot for itself
    inc = desk.investigate(desk.scan(investigate=False)[0])
    assert inc["status"] == "fixed" and inc["mode"] == "autopilot"
    assert incidents.SETTINGS_PATH.read_text() == saved and not incidents.settings()["autopilot"]
    _, _, desk2 = desk_for(tmp_path / "b", autopilot=False)  # and the reverse: forced manual while the setting is on
    incidents.update_settings(autopilot=True)
    asked = []
    desk2.investigate(desk2.scan(investigate=False)[0], ask=lambda i, why: asked.append(why) or False)
    assert asked and incidents.settings()["autopilot"]


def test_stop_releases_a_waiting_investigation_before_the_store_closes(tmp_path):
    incidents.update_settings(autopilot=False)
    store, _, desk = desk_for(tmp_path)
    iid = desk.scan()[0]  # investigates in a background thread, then waits for a person
    for _ in range(200):
        if iid in desk.decisions:
            break
        __import__("time").sleep(0.02)
    assert iid in desk.decisions
    assert desk.stop(timeout=5)  # nothing left running, so closing the store is safe
    store.close()
