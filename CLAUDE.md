# Patient Zero: contact tracing for AI agents

Hackathon project (Databricks track). When an AI agent is compromised by poisoned knowledge or memory,
Patient Zero finds the source, traces everything it spread to across every agent, proves which actions
it caused (counterfactual replay), and surgically undoes only that damage.

Pitch line: "Rubrik rewinds an agent to a point in time. Patient Zero finds the infection, proves what it
caused, and removes only that, across every agent."

## Hard rules
- Finding and fixing are deterministic (graph queries + change feed). No LLM decides what is infected or what gets reverted.
- LLMs are used only to run agents, run counterfactual replays, and explain incidents.
- Current Claude models reject `temperature`; replay determinism comes from repeated replays + majority vote, not temperature 0.
- Never hardcode demo answers (e.g. "D-7") in engine code. The simulation plants the attack; the engine must discover it.
- `tests/test_engine.py` is the contract. Keep it green: `./.venv/bin/python -m pytest -q`.

## Layout
- `pz/store.py`     storage. SQLite locally, emulating Delta: `row_changes` = Change Data Feed, `table_as_of` = time travel
- `pz/recorder.py`  the recorder every agent goes through (retrieve / tool_call / memory_write). This is the product's core.
- `pz/agents.py`    RuleBrain: deterministic agents (no API key). Knows when it used the poison -> ground truth.
- `pz/sim.py`       Northpeak Outfitters: 3 agents, 7 days, ~176 sessions, poisoned ticket T-4127 -> doc D-7 + worm memory
- `pz/engine.py`    trace_back, trace_forward, verify, clean, score
- `pz/server.py`    FastAPI API + serves `web/index.html` (Cytoscape infection graph)

## Data model (mirror these as Delta tables)
- `kb(object_id, kind doc|memory, topic, text, source internal|ticket|agent, visible_to, created_ts, created_by_session, status active|quarantined)`
- `sessions(session_id, agent_id, ts, task)`
- `events(event_id, session_id, agent_id, ts, type retrieve|tool_call|memory_write|kb_ingest, object_id, tool, table_name, row_key, delta_version, reversible, payload)`
- `row_changes(version, table_name, row_key, op, before_json, after_json, session_id, event_id, ts)`  <- Delta CDF
- business tables: `refunds`, `customers`, `emails` (emails are irreversible)
- `ground_truth(event_id, poisoned)` demo only

## Run
```
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pz demo                       # terminal end-to-end
./.venv/bin/uvicorn pz.server:app --port 8100       # UI at http://localhost:8100
```

## Next (in priority order)
1. Databricks: mirror tables to Delta, enable CDF on business tables, use real time travel for revert (`pz/databricks_sync.py`)
2. ClaudeBrain: same tasks run by Claude through the recorder; replays use majority vote
3. Baseline: "Claude with raw logs + DB access" vs Patient Zero, scored on the same ground truth
4. MCP proxy + Python SDK so the recorder attaches to agents we didn't write
5. Live attack: QR-code ticket form so a judge plants the poison
6. Recall mode: doc v2 -> v3 update triggers a forward trace of every decision made on v2
