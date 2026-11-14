"""API + UI.  Run: ./.venv/bin/uvicorn pz.server:app --port 8100"""
import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import databricks, investigator
from .claude_brain import ClaudeBrain
from .live import ATTACK_TEMPLATE, LiveRun
from .engine import Engine
from .scenarios import DEFAULT, SCENARIOS, fmt_amount
from .sim import attack_defaults, build

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Patient Zero")
state = {}


def load(scenario=DEFAULT, attack=None):
    live = state.get("live")
    state.clear()
    if live:
        state["live"] = live
    sc = SCENARIOS[scenario]
    store = build(scenario=scenario, attack=attack)
    state.update(store=store, engine=Engine(store), scenario=scenario, sc=sc,
                 attack={**attack_defaults(sc), **(attack or {})}, verdict=None, approval=None)


load()


def agent_name(agent_id):
    for _role, (aid, name, _d) in state["sc"]["agents"].items():
        if aid == agent_id:
            return name
    return {"system": "Nightly job", "intake": "Intake pipeline"}.get(agent_id, agent_id)


def describe(tool, row, p):
    sc, T = state["sc"], state["sc"]["texts"]
    amt = fmt_amount(sc, p["amount"]) if "amount" in p else ""
    if tool == "create_item":
        return T["primary"].format(row=row, amount=amt, value=p["value"])
    if tool == "change_value":
        return T["reroute"].format(row=row, value=p["value"])
    if tool == "approve":
        return T["approve"].format(row=row)
    if tool == "finalize":
        return T["final"].format(row=row)
    if tool == "upgrade":
        return T["upgrade"].format(row=row, tier=p["tier"])
    if tool == "send_message":
        return f"Messaged {p['to_addr']}: “{p['body']}”"
    if tool == "save_memory":
        return f"Saved a team memory: “{p.get('text', '')}”"
    return f"{tool} {row}"


def label(n):
    """Plain-English label for any node id (event, kb object, session)."""
    eng = state["engine"]
    if n in eng.event:
        e = eng.event[n]
        return describe(e["tool"] or "save_memory", e["row_key"], json.loads(e["payload"]))
    if n in eng.kb:
        k = eng.kb[n]
        if k["source"] not in ("internal", "agent"):
            return f"{n}, a {k['kind']} from {'an' if k['source'][0] in 'aeiou' else 'a'} {k['source']}"
        return f"{'Team memory' if k['kind'] == 'memory' else 'Doc'} {n}"
    sess = state["store"].one("SELECT * FROM sessions WHERE session_id=?", (n,))
    return f"{agent_name(sess['agent_id'])}, day {sess['ts'] // 1000}" if sess else n


# ---------------- scenario + simulation ----------------

class ResetReq(BaseModel):
    scenario: str = DEFAULT
    threshold: int | None = None
    value: str | None = None
    category: str | None = None
    worm: bool | None = None
    day: int | None = None


@app.get("/api/scenarios")
def scenarios():
    return [{"id": k, "title": s["title"], "short": s["short"], "company": s["company"], "blurb": s["blurb"], "real": s["real"],
             "agents": s["agents"], "defaults": attack_defaults(s), "categories": s["categories"], "unit": s["unit"],
             "field": s["field"], "item": s["item"], "entity": s["entity"], "poison_label": s["poison"]["label"]}
            for k, s in SCENARIOS.items()]


@app.post("/api/reset")
def reset(req: ResetReq):
    if req.scenario not in SCENARIOS:
        raise HTTPException(400, "unknown scenario")
    sc = SCENARIOS[req.scenario]
    a = {k: v for k, v in req.model_dump().items() if k != "scenario" and v is not None}
    if "value" in a and not re.fullmatch(sc["value_re"], a["value"]):
        raise HTTPException(400, f"value should look like {sc['attacker_value']}")
    if "category" in a and a["category"] not in sc["categories"]:
        raise HTTPException(400, "unknown category")
    if "day" in a and not 1 <= a["day"] <= 6:
        raise HTTPException(400, "day must be 1-6")
    load(req.scenario, a or None)
    owned = {r["value"] for r in state["store"].q("SELECT value FROM entities")}
    if state["attack"]["value"] in owned:
        load(req.scenario)
        raise HTTPException(400, f"that {sc['field']} belongs to a real {sc['entity']}; pick an attacker value")
    return {"ok": True, "scenario": req.scenario, "attack": state["attack"]}


@app.get("/api/overview")
def overview():
    s, sc = state["store"], state["sc"]
    p = s.one("SELECT * FROM kb WHERE object_id IN ('D-7','M-7')")
    return {
        "scenario": state["scenario"], "title": sc["title"], "company": sc["company"], "blurb": sc["blurb"], "real": sc["real"],
        "agents": sc["agents"], "item": sc["item"], "entity": sc["entity"], "field": sc["field"],
        "sessions": s.one("SELECT COUNT(*) n FROM sessions")["n"],
        "actions": s.one("SELECT COUNT(*) n FROM events WHERE type IN ('tool_call','memory_write')")["n"],
        "attack": state["attack"], "poison": {**p, "label": sc["poison"]["label"]},
        "claude": investigator.claude_available(),
    }


@app.get("/api/alerts")
def alerts():
    """A plain detection rule produces the symptom: a sensitive value no known entity owns."""
    s = state["store"]
    owned = {r["value"] for r in s.q("SELECT value FROM entities")}
    out = []
    for e in s.q("SELECT * FROM events WHERE tool IN ('create_item','change_value') ORDER BY ts"):
        val = json.loads(e["payload"]).get("value")
        if val and val not in owned:
            row = s.get_row("items", e["row_key"]) or {}
            out.append({"event_id": e["event_id"], "agent": agent_name(e["agent_id"]), "role": _role(e["agent_id"]),
                        "row": e["row_key"], "value": val, "amount": fmt_amount(state["sc"], row.get("amount") or 0),
                        "day": e["ts"] // 1000, "text": label(e["event_id"])})
    return out


def _role(agent_id):
    return next((r for r, (aid, _n, _d) in state["sc"]["agents"].items() if aid == agent_id), "other")


@app.get("/api/graph")
def graph():
    s, eng = state["store"], state["engine"]
    nodes = [{"id": o["object_id"], "kind": o["kind"], "source": o["source"], "ts": o["created_ts"], "text": o["text"]}
             for o in s.q("SELECT * FROM kb ORDER BY created_ts")]
    for x in s.q("SELECT * FROM sessions ORDER BY ts"):
        acts = [eng.event[e] for e in eng.actions.get(x["session_id"], [])]
        nodes.append({"id": x["session_id"], "kind": "session", "role": _role(x["agent_id"]), "agent_name": agent_name(x["agent_id"]),
                      "ts": x["ts"], "day": x["ts"] // 1000,
                      "actions": [{"event_id": a["event_id"], "text": label(a["event_id"]), "reversible": a["reversible"]} for a in acts]})
    edges = [{"src": o, "dst": sid, "type": "read"} for sid, objs in eng.reads.items() for o in objs]
    edges += [{"src": e["session_id"], "dst": e["object_id"], "type": "wrote"}
              for e in eng.event.values() if e["type"] in ("memory_write", "kb_ingest")]
    return {"nodes": nodes, "edges": edges}


# ---------------- live mode: real Claude agents, real Databricks data ----------------

class LiveReq(BaseModel):
    attack_text: str | None = None
    use_databricks: bool = True


@app.get("/api/live/defaults")
def live_defaults():
    sc = SCENARIOS["inbox"]
    return {"original_policy": sc["seed_docs"][0][2], "attack_template": ATTACK_TEMPLATE, "company": sc["company"],
            "agents": sc["agents"], "databricks": databricks.available(), "claude": investigator.claude_available(),
            "real": sc["real"]}


@app.post("/api/live/start")
def live_start(req: LiveReq):
    if state.get("live") and not state["live"].done:
        raise HTTPException(409, "a live run is already in progress")
    text = (req.attack_text or "").strip() or None
    run = LiveRun(attack_text=text, use_databricks=req.use_databricks).start()
    state["live"] = run
    return {"run_id": run.id, "databricks": run.use_databricks}


@app.get("/api/live/stream")
async def live_stream():
    run = state.get("live")
    if not run:
        raise HTTPException(404, "no live run")

    async def stream():
        i = 0
        while True:
            while i < len(run.events):
                yield _sse(run.events[i])
                i += 1
            if run.done and i >= len(run.events):
                break
            await asyncio.sleep(0.25)
        if not run.error:
            _adopt_live(run)
            yield _sse({"type": "ready", "attacker_value": run.attacker_value})

    return StreamingResponse(stream(), media_type="text/event-stream")


def _adopt_live(run):
    """Point the API at the live run's data; replays use the same real Claude agents."""
    if state.get("adopted") == run.id:
        return
    state.update(store=run.store, engine=Engine(run.store, brain=ClaudeBrain(run.sc)), scenario="inbox", sc=run.sc,
                 attack={"value": run.attacker_value}, verdict=None, approval=None, adopted=run.id)


# ---------------- the investigator agent (server-sent events) ----------------

class ApproveReq(BaseModel):
    approved: bool


@app.get("/api/investigate")
async def investigate(event_ids: str, mode: str = "auto"):
    ids = [e for e in event_ids.split(",") if e]
    eng = state["engine"]
    if not ids or any(e not in eng.event for e in ids):
        raise HTTPException(404, "unknown event")
    tb = investigator.Toolbox(eng, state["sc"], label, agent_name)
    run = state.get("live")
    if run and state.get("adopted") == run.id and run.delta:
        tb.on_cleanup = lambda result: {**run.delta.apply_cleanup(result["reverted"], {"root": result["quarantined"][0] if result["quarantined"] else "",
                                                                                      "proven": len(tb.verdict["confirmed"]),
                                                                                      "cleared": len(tb.verdict["cleared"])}),
                                        "link": run.delta.table_link("payments")}
    use_claude = mode == "claude" or (mode == "auto" and investigator.claude_available())
    gen = (investigator.claude if use_claude else investigator.scripted)(tb, ids)

    async def stream():
        yield _sse({"type": "mode", "claude": use_claude})
        value = None
        while True:
            try:
                ev = await asyncio.to_thread(gen.send, value) if value is not None else await asyncio.to_thread(next, gen)
            except StopIteration:
                break
            except Exception as ex:  # surface errors to the UI instead of a dead stream
                yield _sse({"type": "error", "text": str(ex)})
                break
            value = None
            if ev["type"] == "tool_result":
                if ev["name"] == "replay_sessions":
                    state["verdict"] = tb.verdict
                yield _sse({**ev, "data": _ui_data(ev["name"], ev["data"])})
            elif ev["type"] == "approval":
                fut = asyncio.get_running_loop().create_future()
                state["approval"] = fut
                yield _sse({"type": "approval", "summary": ev["summary"], "rows": _plan_rows(ev["plan"]),
                            "checklist": ev["plan"]["checklist"][:3]})
                value = await fut
            else:
                yield _sse(ev)
            if not use_claude:
                await asyncio.sleep(0.9 if ev["type"] == "thought" else 0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/approve")
def approve(req: ApproveReq):
    fut = state.get("approval")
    if not fut or fut.done():
        raise HTTPException(400, "nothing waiting for approval")
    fut.get_loop().call_soon_threadsafe(fut.set_result, req.approved)
    return {"ok": True}


def _sse(obj):
    return f"data: {json.dumps(obj, default=str)}\n\n"


def _plan_rows(plan):
    rows = []
    for r in plan["reverted"]:
        if r.get("deleted"):
            rows.append({"row": r["row"], "deleted": True})
            continue
        f = r["fields"].get("value")
        rows.append({"row": r["row"], "was": f and f["was"], "now": f and f["now"], "kept": r["kept"].get("status")})
    return rows


def _ui_data(name, data):
    """Only what the graph needs to animate each tool result."""
    if data is None:
        return None
    if name == "trace_back":
        best = next((c for c in data["candidates"] if c["confirmed"]), data["candidates"][0])
        return {"root": data["root"], "path": best["path"],
                "candidates": [{"id": c["object_id"], "confirmed": c["confirmed"]} for c in data["candidates"]]}
    if name == "trace_forward":
        return {"sessions": data["sessions"], "memories": data["memories"], "agents": data["agents"]}
    if name == "replay_sessions":
        return {"confirmed": data["confirmed"], "cleared": data["cleared"], "score": data["score"]}
    if name == "plan_cleanup":
        return {"quarantined": data["quarantined"]}
    if name == "apply_cleanup":
        return {"quarantined": data["quarantined"], "rows": _plan_rows(data), "delta": data.get("delta")}
    return None


@app.get("/api/explain/{event_id}")
def explain(event_id: str):
    """Why is this action infected (or not)? Chain to the source + with/without-poison comparison."""
    eng, v = state["engine"], state["verdict"]
    e = eng.event.get(event_id)
    if not e:
        raise HTTPException(404)
    out = {"event_id": event_id, "agent": agent_name(e["agent_id"]), "day": e["ts"] // 1000,
           "with_poison": label(event_id), "status": "unexposed", "path": []}
    if not v:
        return out
    if event_id in v["confirmed"]:
        corr = v["corrections"].get(event_id)
        out["status"] = "infected"
        out["without_poison"] = describe(corr["tool"], corr["row_key"], corr["values"]) if corr else "Would not have done this at all"
    elif event_id in v["cleared"]:
        out["status"] = "cleared"
        out["without_poison"] = "Same action, so the poison did not cause it"
    out["path"] = [label(n) for n in (eng.path_to(event_id, v["root"]) or [])[1:]]
    return out


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
