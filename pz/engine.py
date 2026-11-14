"""Patient Zero engine: trace back, trace forward, verify, clean, score.

Finding and fixing are deterministic graph + change-feed operations. The only model
calls happen inside counterfactual replays (when ClaudeBrain is used).
"""
import json
from collections import defaultdict, deque

from .sim import replay_session

ACTION_TYPES = ("tool_call", "memory_write")


class Engine:
    def __init__(self, store, brain=None):
        self.s, self.brain = store, brain
        self._build_graph()

    # ---------- graph ----------
    def _build_graph(self):
        self.readers = defaultdict(list)      # object -> sessions that retrieved it
        self.reads = defaultdict(list)        # session -> objects it retrieved
        self.actions = defaultdict(list)      # session -> action events
        self.writer = {}                      # kb object -> session that created it
        self.event = {}
        for e in self.s.q("SELECT * FROM events ORDER BY ts, event_id"):
            self.event[e["event_id"]] = e
            if e["type"] == "retrieve":
                if e["session_id"] not in self.readers[e["object_id"]]:
                    self.readers[e["object_id"]].append(e["session_id"])
                if e["object_id"] not in self.reads[e["session_id"]]:
                    self.reads[e["session_id"]].append(e["object_id"])
            elif e["type"] in ACTION_TYPES:
                self.actions[e["session_id"]].append(e["event_id"])
            if e["type"] in ("memory_write", "kb_ingest"):
                self.writer[e["object_id"]] = e["session_id"]
        self.kb = {r["object_id"]: r for r in self.s.q("SELECT * FROM kb")}

    def graph(self):
        """Nodes and edges for the UI."""
        nodes, edges = [], []
        for oid, o in self.kb.items():
            nodes.append({"id": oid, "kind": o["kind"], "label": oid, "source": o["source"], "status": o["status"], "text": o["text"]})
        for sid in set(self.reads) | set(self.actions):
            sess = self.s.one("SELECT * FROM sessions WHERE session_id=?", (sid,))
            nodes.append({"id": sid, "kind": "session", "label": sid, "agent": sess["agent_id"], "ts": sess["ts"]})
        for sid, objs in self.reads.items():
            for o in objs:
                edges.append({"src": o, "dst": sid, "type": "read"})
        for sid, evs in self.actions.items():
            for eid in evs:
                e = self.event[eid]
                nodes.append({"id": eid, "kind": "action", "label": e["tool"] or "save_memory", "agent": e["agent_id"],
                              "ts": e["ts"], "reversible": e["reversible"], "row": e["row_key"]})
                edges.append({"src": sid, "dst": eid, "type": "did"})
                if e["type"] == "memory_write":
                    edges.append({"src": eid, "dst": e["object_id"], "type": "wrote"})
        return {"nodes": nodes, "edges": edges}

    # ---------- trace back ----------
    def trace_back(self, event_ids):
        """Find candidate sources for one or more bad actions, then confirm by counterfactual."""
        explains = defaultdict(set)
        paths = {}
        for eid in event_ids:
            sid = self.event[eid]["session_id"]
            stack, seen = [(sid, [eid, sid])], set()
            while stack:
                sess, path = stack.pop()
                for obj in self.reads.get(sess, []):
                    if obj in seen:
                        continue
                    seen.add(obj)
                    p = path + [obj]
                    w = self.writer.get(obj)
                    # every object is a candidate; memories are also followed upstream to whatever wrote them
                    explains[obj].add(eid)
                    paths.setdefault((obj, eid), p)
                    if self.kb[obj]["kind"] == "memory" and w:
                        stack.append((w, p + [w]))

        candidates = []
        for obj, evs in explains.items():
            # counterfactual root test: hide the object, cascade down the chain (a memory on the path is
            # hidden only if its writer would not have written it without the object), then replay the bad session
            probe = sorted(evs)[0]
            hidden = {obj}
            for mem in reversed([n for n in paths[(obj, probe)] if self.kb.get(n, {}).get("kind") == "memory"]):
                w = self.writer[mem]
                replay = replay_session(self.s, w, hidden, self.brain)
                if not any(t == "save_memory" for (t, *_rest) in replay):
                    hidden.add(mem)
            changed = self._action_changed(probe, hidden)
            candidates.append({
                "object_id": obj, "kind": self.kb[obj]["kind"], "source": self.kb[obj]["source"], "text": self.kb[obj]["text"],
                "explains": len(evs), "of": len(event_ids), "exposed_sessions": len(self._exposed_sessions(obj)),
                "confirmed": changed, "path": paths[(obj, probe)],
            })
        # patient zero is the most upstream object whose removal changes the outcome
        candidates.sort(key=lambda c: (not c["confirmed"], self.kb[c["object_id"]]["created_ts"], -c["explains"]))
        return candidates

    def _derived_memories(self, root):
        out, q = set(), deque([root])
        while q:
            o = q.popleft()
            for sid in self.readers.get(o, []):
                for eid in self.actions.get(sid, []):
                    e = self.event[eid]
                    if e["type"] == "memory_write" and e["object_id"] not in out:
                        out.add(e["object_id"])
                        q.append(e["object_id"])
        return out

    def _exposed_sessions(self, root):
        objs = {root} | self._derived_memories(root)
        return sorted({sid for o in objs for sid in self.readers.get(o, [])})

    def _action_changed(self, eid, hidden):
        e = self.event[eid]
        replay = replay_session(self.s, e["session_id"], hidden, self.brain)
        return not self._matches(e, replay)

    @staticmethod
    def _sig(e):
        if e["type"] == "memory_write":
            return ("save_memory", "kb", e["object_id"], json.loads(e["payload"])["text"])
        return (e["tool"], e["table_name"], e["row_key"], json.dumps(json.loads(e["payload"]), sort_keys=True))

    def _matches(self, e, replay_actions):
        tool, table, key, vals = self._sig(e)
        for (t, tb, k, v) in replay_actions:
            if t != tool:
                continue
            if tool == "save_memory":
                if v["text"] == vals:
                    return True
            elif k == key and json.dumps(v, sort_keys=True) == vals:
                return True
        return False

    def path_to(self, eid, root):
        """The chain from an action back to the root: [event, session, (memory, writer session)*, root]."""
        def walk(sess, path, seen):
            for obj in self.reads.get(sess, []):
                if obj in seen:
                    continue
                seen.add(obj)
                if obj == root:
                    return path + [obj]
                w = self.writer.get(obj)
                if self.kb[obj]["kind"] == "memory" and w:
                    found = walk(w, path + [obj, w], seen)
                    if found:
                        return found
            return None
        sid = self.event[eid]["session_id"]
        return walk(sid, [eid, sid], set())

    # ---------- trace forward ----------
    def trace_forward(self, root):
        memories = self._derived_memories(root)
        sessions = self._exposed_sessions(root)
        suspects = [eid for sid in sessions for eid in self.actions.get(sid, [])]
        return {"root": root, "memories": sorted(memories), "sessions": sessions, "suspects": suspects,
                "agents": sorted({self.event[e]["agent_id"] for e in suspects})}

    # ---------- verify ----------
    def verify(self, root):
        fwd = self.trace_forward(root)
        hidden = {root} | set(fwd["memories"])
        confirmed, cleared, corrections = [], [], {}
        for sid in fwd["sessions"]:
            replay = replay_session(self.s, sid, hidden, self.brain)
            for eid in self.actions.get(sid, []):
                e = self.event[eid]
                if self._matches(e, replay):
                    cleared.append(eid)
                else:
                    confirmed.append(eid)
                    corrections[eid] = self._counterpart(e, replay)
        return {**fwd, "confirmed": confirmed, "cleared": cleared, "corrections": corrections}

    @staticmethod
    def _counterpart(e, replay):
        """What the agent would have done without the poison (same tool, same row)."""
        for (t, tb, k, v) in replay:
            if t == e["tool"] and (k == e["row_key"] or t == "send_message"):
                return {"tool": t, "row_key": k, "values": v}
        return None

    # ---------- clean ----------
    def clean(self, verdict, apply=True):
        ts = 10 ** 9
        reverted, conflicts, checklist, quarantined = [], [], [], []
        confirmed = set(verdict["confirmed"])
        for obj in [verdict["root"]] + [m for m in verdict["memories"]
                                        if any(self.event[e]["object_id"] == m for e in confirmed)]:
            quarantined.append(obj)
            if apply:
                self.s.db.execute("UPDATE kb SET status='quarantined' WHERE object_id=?", (obj,))

        for eid in verdict["confirmed"]:
            e = self.event[eid]
            if e["type"] != "tool_call":
                continue
            corr = verdict["corrections"].get(eid)
            if not e["reversible"]:
                checklist.append({"event_id": eid, "tool": e["tool"], "sent": json.loads(e["payload"]),
                                  "should_have_been": corr["values"] if corr else None})
                continue
            change = self.s.one("SELECT * FROM row_changes WHERE version=?", (e["delta_version"],))
            before = json.loads(change["before_json"]) if change["before_json"] else {}
            after = json.loads(change["after_json"])
            current = self.s.get_row(e["table_name"], e["row_key"]) or {}
            target = dict(before)
            if corr:
                target.update(corr["values"])
            if change["op"] == "insert" and not corr:
                reverted.append({"event_id": eid, "table": e["table_name"], "row": e["row_key"], "deleted": True})
                if apply:
                    self.s.delete_row(e["table_name"], e["row_key"], "pz_cleanup", f"fix_{eid}", ts)
                continue
            fixes = {}
            for field, bad in after.items():
                if before.get(field) == bad or target.get(field) == bad:
                    continue  # field not touched, or the clean replay would have written the same value
                if current.get(field) != bad:
                    conflicts.append({"event_id": eid, "row": e["row_key"], "field": field, "current": current.get(field)})
                    continue
                if field in target:
                    fixes[field] = target[field]
            # fields the infected change set that a later legit change overwrote are kept as-is (e.g. status=paid)
            if fixes:
                reverted.append({"event_id": eid, "table": e["table_name"], "row": e["row_key"],
                                 "fields": {f: {"was": current.get(f), "now": v} for f, v in fixes.items()},
                                 "kept": {f: current.get(f) for f in current if f not in fixes}})
                if apply:
                    self.s.write_row(e["table_name"], e["row_key"], fixes, "pz_cleanup", f"fix_{eid}", ts)

        poison_ts = self.kb[verdict["root"]]["created_ts"]
        later_changes = self.s.one(
            "SELECT COUNT(*) n FROM row_changes WHERE ts>=? AND session_id!='pz_cleanup'", (poison_ts,))["n"]
        infected_changes = len({self.event[e]["delta_version"] for e in confirmed
                                if self.event[e]["type"] == "tool_call"})
        if apply:
            self.s.commit()
        return {"quarantined": quarantined, "reverted": reverted, "conflicts": conflicts, "checklist": checklist,
                "point_in_time_collateral": later_changes - infected_changes}

    # ---------- score ----------
    def score(self, verdict):
        truth = {r["event_id"] for r in self.s.q("SELECT event_id FROM ground_truth WHERE poisoned=1")}

        def pr(pred):
            pred = set(pred)
            tp = len(pred & truth)
            return {"predicted": len(pred), "true_positives": tp, "actual": len(truth),
                    "precision": round(tp / len(pred), 3) if pred else 0.0,
                    "recall": round(tp / len(truth), 3) if truth else 0.0}

        return {"trace_only": pr(verdict["suspects"]), "trace_plus_verify": pr(verdict["confirmed"])}
