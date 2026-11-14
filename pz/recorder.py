"""The recorder: a thin wrapper every agent goes through.

It is the product's core. It captures the causal chain that ordinary logs miss:
which knowledge-base objects were in an agent's context when it acted.
"""
import json
import itertools


class Recorder:
    def __init__(self, store):
        self.s = store
        self._seq = itertools.count(1)

    def _event_id(self, session_id):
        return f"evt_{session_id}_{next(self._seq):04d}"

    def start_session(self, session_id, agent_id, ts, task):
        self.s.db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (session_id, agent_id, ts, json.dumps(task)))
        return Session(self, session_id, agent_id, ts)


class Session:
    def __init__(self, rec, session_id, agent_id, ts):
        self.rec, self.s = rec, rec.s
        self.id, self.agent, self.ts = session_id, agent_id, ts
        self.context = []        # kb object ids read so far
        self.exclude = set()     # used by counterfactual replay: objects hidden from retrieval
        self.dry_run = False     # replay mode: compute actions, write nothing
        self.actions = []        # (tool, args) produced this session

    def _event(self, **kw):
        eid = self.rec._event_id(self.id)
        if not self.dry_run:
            self.s.db.execute(
                "INSERT INTO events (event_id,session_id,agent_id,ts,type,object_id,tool,table_name,row_key,delta_version,reversible,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, self.id, self.agent, self.ts, kw.get("type"), kw.get("object_id"), kw.get("tool"), kw.get("table_name"),
                 kw.get("row_key"), kw.get("delta_version"), kw.get("reversible", 1), json.dumps(kw.get("payload", {}))),
            )
        return eid

    # ---------- reads ----------
    def retrieve(self, topics, k=4):
        topics = [topics] if isinstance(topics, str) else list(topics)
        marks = ",".join("?" * len(topics))
        rows = self.s.q(
            f"SELECT * FROM kb WHERE status='active' AND topic IN ({marks}) AND created_ts<? "
            "AND (visible_to='*' OR instr(visible_to, ?)>0) ORDER BY created_ts DESC",
            (*topics, self.ts, self.agent),
        )
        rows = [r for r in rows if r["object_id"] not in self.exclude][:k]
        for r in rows:
            self.context.append(r["object_id"])
            self._event(type="retrieve", object_id=r["object_id"], payload={"topics": topics})
        return rows

    # ---------- writes ----------
    def tool_call(self, tool, table, row_key, values, reversible=True, poisoned=None):
        self.actions.append((tool, table, row_key, dict(values)))
        if self.dry_run:
            return None
        eid = self.rec._event_id(self.id)
        version = self.s.write_row(table, row_key, values, self.id, eid, self.ts)
        self.s.db.execute(
            "INSERT INTO events (event_id,session_id,agent_id,ts,type,object_id,tool,table_name,row_key,delta_version,reversible,payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, self.id, self.agent, self.ts, "tool_call", row_key, tool, table, row_key, version, int(reversible), json.dumps(values)),
        )
        if poisoned is not None:
            self.s.db.execute("INSERT INTO ground_truth VALUES (?,?)", (eid, int(poisoned)))
        return eid

    def memory_write(self, object_id, topic, text, visible_to="*", poisoned=None, source="agent"):
        self.actions.append(("save_memory", "kb", object_id, {"text": text}))
        if self.dry_run:
            return None
        self.s.db.execute(
            "INSERT INTO kb VALUES (?,?,?,?,?,?,?,?, 'active')",
            (object_id, "memory", topic, text, source, visible_to, self.ts, self.id),
        )
        eid = self._event(type="memory_write", object_id=object_id, payload={"text": text, "derived_from": self.context})
        if poisoned is not None:
            self.s.db.execute("INSERT INTO ground_truth VALUES (?,?)", (eid, int(poisoned)))
        return eid

    def kb_ingest(self, object_id, topic, text, source, visible_to="*"):
        self.s.db.execute(
            "INSERT INTO kb VALUES (?,?,?,?,?,?,?,?, 'active')",
            (object_id, "doc", topic, text, source, visible_to, self.ts, self.id),
        )
        return self._event(type="kb_ingest", object_id=object_id, payload={"source": source})
