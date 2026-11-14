"""API + UI.  Run: ./.venv/bin/uvicorn pz.server:app --reload --port 8100"""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import Engine
from .sim import build
from .store import Store, DB_PATH

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Patient Zero")
state = {}


def load(fresh=False):
    store = build() if fresh or not DB_PATH.exists() else Store()
    state.clear()
    state.update(store=store, engine=Engine(store), verdict=None, cleaned=None)


load(fresh=True)


class TraceReq(BaseModel):
    event_ids: list[str]


class RootReq(BaseModel):
    root: str


@app.post("/api/reset")
def reset():
    load(fresh=True)
    return {"ok": True}


@app.get("/api/overview")
def overview():
    s = state["store"]
    return {
        "sessions": s.one("SELECT COUNT(*) n FROM sessions")["n"],
        "actions": s.one("SELECT COUNT(*) n FROM events WHERE type IN ('tool_call','memory_write')")["n"],
        "agents": [r["agent_id"] for r in s.q("SELECT DISTINCT agent_id FROM sessions ORDER BY agent_id")],
        "kb": s.q("SELECT object_id, kind, topic, source, status, text FROM kb ORDER BY created_ts"),
    }


@app.get("/api/alerts")
def alerts():
    """A plain detection rule produces the symptom: payouts going to an account no customer owns."""
    s = state["store"]
    owned = {r["account"] for r in s.q("SELECT account FROM customers")}
    out = []
    for e in s.q("SELECT * FROM events WHERE tool IN ('issue_refund','reroute_payout') ORDER BY ts"):
        acct = json.loads(e["payload"]).get("account")
        if acct and acct not in owned:
            row = s.get_row("refunds", e["row_key"]) or {}
            out.append({"event_id": e["event_id"], "agent": e["agent_id"], "tool": e["tool"], "row": e["row_key"],
                        "account": acct, "amount": row.get("amount"), "ts": e["ts"]})
    return out


@app.get("/api/graph")
def graph():
    """Session-level timeline graph: KB objects and sessions, with read/wrote edges."""
    s, eng = state["store"], state["engine"]
    sessions = s.q("SELECT * FROM sessions ORDER BY ts")
    nodes = []
    for o in s.q("SELECT * FROM kb ORDER BY created_ts"):
        nodes.append({"id": o["object_id"], "kind": o["kind"], "source": o["source"], "status": o["status"],
                      "ts": o["created_ts"], "text": o["text"], "topic": o["topic"]})
    for x in sessions:
        acts = [eng.event[e] for e in eng.actions.get(x["session_id"], [])]
        nodes.append({"id": x["session_id"], "kind": "session", "agent": x["agent_id"], "ts": x["ts"],
                      "task": json.loads(x["task"]),
                      "actions": [{"event_id": a["event_id"], "tool": a["tool"] or "save_memory", "row": a["row_key"],
                                   "reversible": a["reversible"], "payload": json.loads(a["payload"])} for a in acts]})
    edges = []
    for sid, objs in eng.reads.items():
        for o in objs:
            edges.append({"src": o, "dst": sid, "type": "read"})
    for e in eng.event.values():
        if e["type"] in ("memory_write", "kb_ingest"):
            edges.append({"src": e["session_id"], "dst": e["object_id"], "type": "wrote"})
    return {"nodes": nodes, "edges": edges}


@app.post("/api/trace")
def trace(req: TraceReq):
    eng = state["engine"]
    for e in req.event_ids:
        if e not in eng.event:
            raise HTTPException(404, f"unknown event {e}")
    cands = eng.trace_back(req.event_ids)
    root = next((c["object_id"] for c in cands if c["confirmed"]), None)
    return {"candidates": cands, "root": root, "forward": eng.trace_forward(root) if root else None}


@app.post("/api/verify")
def verify(req: RootReq):
    v = state["engine"].verify(req.root)
    state["verdict"] = v
    return {**v, "score": state["engine"].score(v)}


@app.post("/api/clean")
def clean(req: RootReq):
    v = state["verdict"]
    if not v or v["root"] != req.root:
        raise HTTPException(400, "run verify first")
    before = {r["refund_id"]: r for r in state["store"].q("SELECT * FROM refunds")}
    result = state["engine"].clean(v)
    state["cleaned"] = result
    after = {r["refund_id"]: r for r in state["store"].q("SELECT * FROM refunds")}
    touched = {r["row"] for r in result["reverted"]}
    diff = [{"row": k, "before": before.get(k), "after": after.get(k), "reverted": k in touched}
            for k in sorted(before) if before.get(k) != after.get(k) or k in touched]
    return {**result, "diff": diff}


@app.get("/api/rows/{table}")
def rows(table: str):
    if table not in ("refunds", "customers", "emails"):
        raise HTTPException(404)
    return state["store"].q(f"SELECT * FROM {table}")


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
