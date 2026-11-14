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
    keep = {k: state[k] for k in ("live", "dashboard_url") if k in state}
    state.clear()
    state.update(keep)
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


def describe(tool, row, p, sc=None):
    sc = sc or state["sc"]
    T = sc["texts"]
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


def label(n, eng=None, sc=None):
    """Plain-English label for any node id (event, kb object, session) in the given run (default: current)."""
    eng = eng or state["engine"]
    if n in eng.event:
        e = eng.event[n]
        return describe(e["tool"] or "save_memory", e["row_key"], json.loads(e["payload"]), sc)
    if n in eng.kb:
        k = eng.kb[n]
        if k["source"] not in ("internal", "agent"):
            return f"{n}, a {k['kind']} from {'an' if k['source'][0] in 'aeiou' else 'a'} {k['source']}"
        return f"{'Team memory' if k['kind'] == 'memory' else 'Doc'} {n}"
    sess = eng.s.one("SELECT * FROM sessions WHERE session_id=?", (n,))
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
    p = s.one("SELECT * FROM kb WHERE source NOT IN ('internal','agent') ORDER BY created_ts DESC LIMIT 1") or {}
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


@app.get("/api/live/links")
def live_links():
    from . import tracking
    run = state.get("live")
    out = {"payments": f"{databricks.workspace_url().rstrip('/')}/explore/data/workspace/patient_zero/payments"}
    try:
        out["mlflow"] = tracking.experiment_url()
    except Exception:
        pass
    if "dashboard_url" not in state:
        try:
            from . import dashboard
            state["dashboard_url"] = dashboard.ensure()  # created/published once per server start
        except Exception:
            state["dashboard_url"] = None
    if state["dashboard_url"]:
        out["dashboard"] = state["dashboard_url"]
    out["investigated"] = bool(run and getattr(run, "investigation", None))
    return out


@app.get("/api/live/runs")
def live_runs():
    return LiveRun.list_runs()


class LoadReq(BaseModel):
    run_id: str


@app.post("/api/live/load")
def live_load(req: LoadReq):
    if not re.fullmatch(r"[0-9a-f]{8}", req.run_id):
        raise HTTPException(400, "bad run id")
    run = LiveRun.load(req.run_id)
    state["live"] = run
    state.pop("adopted", None)
    _adopt_live(run)
    if run.verdict:
        state["verdict"] = run.verdict
    return {"run_id": run.id, "investigated": bool(run.investigation)}


@app.get("/api/live/stream")
async def live_stream(speed: float = 1.0):
    run = state.get("live")
    if not run:
        raise HTTPException(404, "no live run")
    replay = run.done  # a finished (or reloaded) run streams its recording, paced like the original

    async def stream():
        i, last_t = 0, 0.0
        while True:
            while i < len(run.events):
                ev = run.events[i]
                if replay:
                    await asyncio.sleep(max(0.0, min(3.0, (ev.get("t", last_t) - last_t) / max(speed, 0.1))))
                    last_t = ev.get("t", last_t)
                yield _sse({**ev, "replay": replay})
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
    if run.delta:
        run.store.time_travel = lambda ts: run.delta.items_as_of(ts)[1]  # replays read payments via Delta time travel
    state.update(store=run.store, engine=Engine(run.store, brain=type(run.brain)(run.sc)), scenario="inbox", sc=run.sc,
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
    sc = state["sc"]
    tb = investigator.Toolbox(eng, sc, lambda n: label(n, eng, sc), agent_name)
    run = state.get("live")
    if run and state.get("adopted") == run.id and run.delta:
        tb.delta = run.delta
        tb.on_cleanup = lambda result: {**run.delta.apply_cleanup(result["reverted"], {"root": result["quarantined"][0] if result["quarantined"] else "",
                                                                                      "proven": len(tb.verdict["confirmed"]),
                                                                                      "cleared": len(tb.verdict["cleared"])}),
                                        "link": run.delta.table_link("payments")}
    use_claude = mode == "claude" or (mode == "auto" and investigator.claude_available())
    gen = (investigator.claude if use_claude else investigator.scripted)(tb, ids)

    recording = []
    is_live = bool(run and state.get("adopted") == run.id)

    async def stream():
        yield _sse({"type": "mode", "claude": use_claude})
        value = None
        while True:
            try:
                ev = await asyncio.to_thread(gen.send, value) if value is not None else await asyncio.to_thread(next, gen)
            except StopIteration:
                if is_live and tb.verdict:
                    run.save_investigation(recording, tb.verdict)
                break
            except Exception as ex:  # surface errors to the UI instead of a dead stream
                yield _sse({"type": "error", "text": str(ex)})
                break
            value = None
            if is_live:
                recording.append({**ev, "data": _ui_data(ev["name"], ev["data"])} if ev["type"] == "tool_result" else
                                 {"type": "approval", "summary": ev["summary"], "note": ev.get("note"), "rows": _plan_rows(ev["plan"]), "checklist": ev["plan"]["checklist"][:3]}
                                 if ev["type"] == "approval" else ev)
            if ev["type"] == "tool_result":
                if ev["name"] == "replay_sessions":
                    state["verdict"] = tb.verdict
                yield _sse({**ev, "data": _ui_data(ev["name"], ev["data"])})
            elif ev["type"] == "report" and run and state.get("adopted") == run.id and run.mlflow_run_id:
                try:
                    from . import tracking
                    await asyncio.to_thread(tracking.log_investigation, run.mlflow_run_id, ev["data"],
                                            investigator.MODEL if use_claude else "scripted")
                    ev = {**ev, "data": {**ev["data"], "mlflow_url": tracking.experiment_url()}}
                except Exception as ex:
                    ev = {**ev, "data": {**ev["data"], "mlflow_error": str(ex)[:200]}}
                yield _sse(ev)
            elif ev["type"] == "approval":
                fut = asyncio.get_running_loop().create_future()
                state["approval"] = fut
                yield _sse({"type": "approval", "summary": ev["summary"], "note": ev.get("note"), "rows": _plan_rows(ev["plan"]),
                            "checklist": ev["plan"]["checklist"][:3]})
                value = await fut
            else:
                yield _sse(ev)
            if not use_claude:
                await asyncio.sleep(0.9 if ev["type"] == "thought" else 0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/investigate/replay")
async def investigate_replay():
    """Replays a recorded investigation (no model calls). Pauses at the approval step like the real one."""
    run = state.get("live")
    if not run or not getattr(run, "investigation", None):
        raise HTTPException(404, "no recorded investigation for this run")

    async def stream():
        yield _sse({"type": "mode", "claude": True, "replay": True})
        for ev in run.investigation:
            if ev["type"] == "mode":
                continue
            yield _sse(ev)
            if ev["type"] == "approval":
                fut = asyncio.get_running_loop().create_future()
                state["approval"] = fut
                await fut
            await asyncio.sleep(1.1 if ev["type"] == "thought" else 0.6)

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
        return {"sessions": data["sessions"], "memories": data["memories"], "agents": data["agents"],
                "sql": data.get("sql"), "sql_agrees": data.get("sql_agrees")}
    if name == "find_lookalikes":
        return {"hits": data["hits"]}
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
