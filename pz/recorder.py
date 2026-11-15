"""The recorder: a thin wrapper every agent goes through.

It is the product's core. It captures the causal chain that ordinary logs miss:
which knowledge-base objects were in an agent's context when it acted.
"""
import json
import itertools

COLS = "(object_id,kind,topic,text,source,visible_to,created_ts,created_by_session)"


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
        self.held = None         # why the last write was held by an open incident (None = it went through)

    # ---------- containment: an open incident holds writes; replays always see the world as recorded ----------
    def hold_reason(self, payload):
        """Why this write must be held: it carries a flagged value, or this session already read withdrawn knowledge."""
        if self.dry_run:
            return None
        holds = self.s.q("SELECT * FROM holds WHERE active=1")
        if not holds:
            return None
        text = json.dumps(payload)
        for h in holds:
            if h["kind"] == "value" and h["value"] in text:
                return h["incident_id"], f"carries {h['value']}"
            if h["kind"] == "object" and h["value"] in self.context:
                return h["incident_id"], f"this session read {h['value']}, which is withdrawn"
        return None

    def _hold(self, tool, key, payload):
        r = self.hold_reason(payload)
        self.held = r and r[1]
        if r:
            self.s.db.execute("INSERT INTO held_actions (incident_id, session_id, agent_id, ts, tool, row_key, payload, reason) VALUES (?,?,?,?,?,?,?,?)",
                              (r[0], self.id, self.agent, self.ts, tool, key, json.dumps(payload), r[1]))
        return bool(r)

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
        live = "" if self.dry_run else "status='active' AND "  # withdrawn knowledge is hidden from agents, not from replays
        rows = self.s.q(
            f"SELECT * FROM kb WHERE {live}topic IN ({marks}) AND created_ts<? AND (retired_ts IS NULL OR retired_ts>?) "
            "AND (visible_to='*' OR instr(visible_to, ?)>0) ORDER BY created_ts DESC",
            (*topics, self.ts, self.ts, self.agent),
        )
        # counterfactual: hiding an edit means the agent would have seen the version it replaced
        if self.exclude:
            ex = list(self.exclude)
            restored = [r["supersedes"] for r in self.s.q(
                f"SELECT supersedes FROM kb WHERE supersedes IS NOT NULL AND object_id IN ({','.join('?' * len(ex))})", ex)]
            if restored:
                rows += self.s.q(f"SELECT * FROM kb WHERE object_id IN ({','.join('?' * len(restored))}) AND topic IN ({marks})",
                                 (*restored, *topics))
        rows = [r for r in rows if r["object_id"] not in self.exclude][:k]
        for r in rows:
            self.context.append(r["object_id"])
            self._event(type="retrieve", object_id=r["object_id"], payload={"topics": topics})
        return rows

    def read(self, object_id):
        """Record that this session read one specific object. Returns False if it is hidden (replay) or withdrawn
        (suspect or quarantined by an incident)."""
        row = self.s.one("SELECT status FROM kb WHERE object_id=?", (object_id,))
        if object_id in self.exclude or (not self.dry_run and row and row["status"] != "active"):
            return False
        if object_id not in self.context:
            self.context.append(object_id)
            self._event(type="retrieve", object_id=object_id, payload={})
        return True

    def external_call(self, tool, key, payload, reversible=True, poisoned=None):
        """An action in an outside system (GitHub, email...). Recorded, never applied to local tables."""
        self.actions.append((tool, "external", key, dict(payload)))
        if self.dry_run or self._hold(tool, key, payload):
            return None
        eid = self._event(type="tool_call", object_id=key, tool=tool, table_name="external", row_key=key,
                          reversible=int(reversible), payload=payload)
        if poisoned is not None:
            self.s.db.execute("INSERT INTO ground_truth VALUES (?,?)", (eid, int(poisoned)))
        return eid

    # ---------- writes ----------
    def tool_call(self, tool, table, row_key, values, reversible=True, poisoned=None):
        self.actions.append((tool, table, row_key, dict(values)))
        if self.dry_run or self._hold(tool, row_key, values):
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
        if self.dry_run or self._hold("save_memory", object_id, {"text": text}):
            return None
        self.s.db.execute(
            "INSERT INTO kb (object_id,kind,topic,text,source,visible_to,created_ts,created_by_session) VALUES (?,?,?,?,?,?,?,?)",
            (object_id, "memory", topic, text, source, visible_to, self.ts, self.id),
        )
        eid = self._event(type="memory_write", object_id=object_id, payload={"text": text, "derived_from": self.context})
        if poisoned is not None:
            self.s.db.execute("INSERT INTO ground_truth VALUES (?,?)", (eid, int(poisoned)))
        return eid

    def kb_edit(self, old_id, new_id, text, source):
        """A page edit: the new version replaces the old one from this moment on (the old one stays for time travel)."""
        old = self.s.one("SELECT * FROM kb WHERE object_id=?", (old_id,))
        self.s.db.execute("UPDATE kb SET retired_ts=? WHERE object_id=?", (self.ts, old_id))
        self.s.db.execute(f"INSERT INTO kb {COLS} VALUES (?,?,?,?,?,?,?,?)",
                          (new_id, "doc", old["topic"], text, source, old["visible_to"], self.ts, self.id))
        self.s.db.execute("UPDATE kb SET supersedes=? WHERE object_id=?", (old_id, new_id))
        return self._event(type="kb_ingest", object_id=new_id, payload={"source": source, "edit_of": old_id})

    def kb_ingest(self, object_id, topic, text, source, visible_to="*"):
        self.s.db.execute(
            "INSERT INTO kb (object_id,kind,topic,text,source,visible_to,created_ts,created_by_session) VALUES (?,?,?,?,?,?,?,?)",
            (object_id, "doc", topic, text, source, visible_to, self.ts, self.id),
        )
        return self._event(type="kb_ingest", object_id=object_id, payload={"source": source})
