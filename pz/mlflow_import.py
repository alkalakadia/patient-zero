"""Import an agent's existing MLflow traces into Patient Zero. No proxy, no code changes in the agent.

Mapping (MLflow span types, which follow OpenTelemetry GenAI conventions):
  trace                 -> one agent session
  RETRIEVER span docs   -> what the agent read (doc_uri = object id, metadata.source = provenance)
  TOOL span save_note*  -> a memory write (a later trace that retrieves that note is downstream of it)
  other TOOL spans      -> actions in outside systems (recorded, not reversible from traces alone)

Without a replay hook Patient Zero can't re-run the agent, so cause is established by evidence instead:
the flagged value (e.g. the attacker's account) appearing in both the source and the action.
"""
import json
import time
import unicodedata

from . import databricks
from .recorder import Recorder
from .store import DB_PATH, Store

NOTE_TOOLS = ("save_note", "save_memory", "remember", "add_memory")


def _norm(s):
    # models often print look-alike dashes (U+2011 etc.); normalize so evidence matching is reliable
    return "".join("-" if unicodedata.category(c) == "Pd" else c for c in str(s))


def fetch_traces(experiment):
    import mlflow
    databricks._load_env()
    mlflow.set_tracking_uri("databricks")
    exp = mlflow.get_experiment_by_name(experiment) if experiment.startswith("/") else mlflow.get_experiment(experiment)
    traces = mlflow.search_traces(locations=[exp.experiment_id], max_results=200, return_type="list", include_spans=True)
    return exp, sorted(traces, key=lambda t: t.info.request_time)


class ImportRun:
    """Looks like a finished live run to the rest of the app (events for the feed, a store for the engine)."""

    def __init__(self, experiment, indicator):
        self.experiment, self.indicator = experiment, _norm(indicator).strip()
        self.events, self.done, self.error, self.delta, self.mlflow_run_id = [], True, None, None, None
        self.id = "mlf" + str(int(time.time()))[-5:]
        self.db = DB_PATH.parent / f"import_{self.id}.db"
        self.store = Store(self.db, fresh=True)
        self.store.db.execute("INSERT INTO meta VALUES ('scenario', 'mlflow_import')")

    def emit(self, ev):
        self.events.append({**ev, "t": round(len(self.events) * 0.4, 1)})

    def run(self):
        exp, traces = fetch_traces(self.experiment)
        self.experiment_id = exp.experiment_id
        self.emit({"type": "phase", "text": f"Imported {len(traces)} traces from MLflow experiment {exp.name} (no proxy, no agent changes)",
                   "link": f"{databricks.workspace_url().rstrip('/')}/ml/experiments/{exp.experiment_id}/traces"})
        rec = Recorder(self.store)
        for n, t in enumerate(traces):
            sid, ts = f"t{n + 1:02d}", 1000 + n
            spans = sorted(t.data.spans, key=lambda s: s.start_time_ns)
            root = spans[0]
            agent = root.name or "agent"
            sess = rec.start_session(sid, agent, ts, {"kind": "imported", "trace_id": t.info.trace_id, "tags": dict(t.info.tags)})
            label = t.info.tags.get("ticket") or t.info.trace_id[:10]
            self.emit({"type": "session_start", "session": sid, "agent": agent, "day": 1, "task": f"Trace {label}: {str(root.inputs)[:120]}"})
            for s in spans[1:]:
                kind = str(s.span_type)
                if kind == "RETRIEVER":
                    for d in s.outputs or []:
                        meta = d.get("metadata", {}) if isinstance(d, dict) else {}
                        oid = meta.get("doc_uri") or meta.get("id") or d.get("id")
                        if not oid:
                            continue
                        note = str(oid).startswith("note") or meta.get("source") == "team note"
                        if not self.store.one("SELECT 1 AS x FROM kb WHERE object_id=?", (oid,)):
                            self.store.db.execute(
                                "INSERT INTO kb (object_id,kind,topic,text,source,visible_to,created_ts,created_by_session) VALUES (?,?,?,?,?,?,?,?)",
                                (oid, "memory" if note else "doc", "imported", d.get("page_content", ""), meta.get("source", "knowledge base"),
                                 "*", ts - 1, "imported"))
                        sess.read(oid)
                        self.emit({"type": "tool", "session": sid, "agent": agent, "tool": "read", "args": {"id": oid}, "result": meta.get("source", "")})
                elif kind == "TOOL":
                    inp, out = s.inputs or {}, s.outputs
                    if s.name in NOTE_TOOLS:
                        oid = (out or {}).get("note_id") if isinstance(out, dict) else None
                        oid = oid or f"note-{sid}"
                        text = inp.get("text", json.dumps(inp))
                        sess.memory_write(oid, "imported", text, source="team note",
                                          poisoned=bool(self.indicator) and self.indicator in _norm(text))
                        self.emit({"type": "tool", "session": sid, "agent": agent, "tool": "save_team_note", "args": {"text": inp.get("text", "")}, "result": oid})
                    else:
                        payload = {**inp, "_output": out}
                        poisoned = bool(self.indicator) and self.indicator in _norm(json.dumps(inp))
                        sess.external_call(s.name, f"{s.name}:{sid}:{s.span_id[:6]}", payload, reversible=False, poisoned=poisoned)
                        self.emit({"type": "tool", "session": sid, "agent": agent, "tool": s.name, "args": inp, "result": str(out)[:80]})
            self.store.commit()
        self.store.db.execute("INSERT OR REPLACE INTO meta VALUES ('events', ?)", (json.dumps(self.events),))
        self.store.commit()
        self.emit({"type": "finished", "cost": 0.0})
        return self
