"""Patient Zero engine: trace back, trace forward, verify, clean, score.

Finding and fixing are deterministic graph + change-feed operations. The only model
calls happen inside counterfactual replays (when ClaudeBrain is used).
"""
import json
from collections import defaultdict, deque

from .sim import replay_session

ACTION_TYPES = ("tool_call", "memory_write")


class Engine:
    def __init__(self, store, brain=None, evidence=None, votes=1):
        import re
        from .sim import scenario_of
        self.s, self.brain = store, brain
        self.votes = votes  # replays per test with a real model; the majority decides (deterministic brains need 1)
        self.evidence = evidence  # trace-only mode: cause is shown by this value appearing in source and action
        self.value_re = re.compile(scenario_of(store)["value_re"])
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
            if self.evidence is not None:
                from .mlflow_import import _norm
                candidates.append({
                    "object_id": obj, "kind": self.kb[obj]["kind"], "source": self.kb[obj]["source"], "text": self.kb[obj]["text"],
                    "explains": len(evs), "of": len(event_ids), "exposed_sessions": len(self._exposed_sessions(obj)),
                    "confirmed": self.evidence in _norm(self.kb[obj]["text"]), "path": paths[(obj, sorted(evs)[0])], "by": "evidence"})
                continue
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
        # patient zero: confirmed, explains the most flagged actions (an agent's own task input explains only its own),
        # then the most upstream
        # ties (e.g. one bad action, explained by both its task and a shared note) go to the source that reached the most
        # agent sessions: a task is read once, a poisoned shared note spreads
        candidates.sort(key=lambda c: (not c["confirmed"], -c["explains"], -c["exposed_sessions"], self.kb[c["object_id"]]["created_ts"]))
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

    def _replays(self, sid, hidden, decided):
        """Replay a session up to `votes` times, stopping once the majority can't change. decided(replays) -> bool.
        A replay in which the agent did nothing at all is a failed run (models sometimes stop early), not evidence:
        it is discarded and retried, up to twice the vote budget."""
        out, did_something = [], bool(self.actions.get(sid))
        for _ in range(self.votes * 2 if self.votes > 1 else 1):
            r = replay_session(self.s, sid, hidden, self.brain)
            if did_something and not r and self.votes > 1:
                continue
            out.append(r)
            if len(out) >= self.votes or decided(out):
                break
        return out or [[]]

    def _majority(self, e, replays):
        """True if most replays did NOT reproduce the action (so the hidden source caused it)."""
        return sum(not self._matches(e, r) for r in replays) * 2 > len(replays)

    def _settled(self, events):
        need = self.votes // 2 + 1
        return lambda rs: all(max(sum(not self._matches(e, r) for r in rs), sum(self._matches(e, r) for r in rs)) >= need
                              for e in events)

    def _action_changed(self, eid, hidden):
        e = self.event[eid]
        replays = self._replays(e["session_id"], hidden, self._settled([e]))
        return self._majority(e, replays)

    def _norm(self, tool, key, vals):
        """Compare actions by what matters, not wording: free text is reduced to the sensitive values it mentions."""
        if tool == "open_pull_request":
            text = json.dumps(vals.get("files", {})) + (vals.get("body") or "")
            return (tool, frozenset(self.value_re.findall(text)))
        if tool in ("save_memory", "send_message"):
            text = vals.get("text") or vals.get("body") or ""
            return (tool, frozenset(self.value_re.findall(text)))
        return (tool, key, json.dumps(vals, sort_keys=True))

    def _matches(self, e, replay_actions):
        p = json.loads(e["payload"])
        if e["type"] == "memory_write":
            mine = self._norm("save_memory", None, p)
        else:
            mine = self._norm(e["tool"], e["row_key"], p)
        return any(self._norm(t, k, v) == mine for (t, tb, k, v) in replay_actions)

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
        if self.evidence is not None:  # no replay hook: classify exposed actions by evidence
            from .mlflow_import import _norm
            conf = [e for e in fwd["suspects"] if self.evidence in _norm(self.event[e]["payload"])]
            return {**fwd, "confirmed": conf, "cleared": [e for e in fwd["suspects"] if e not in conf], "corrections": {}, "by": "evidence"}
        hidden = {root} | set(fwd["memories"])
        confirmed, cleared, corrections = [], [], {}
        self.vote_log = {}
        for sid in fwd["sessions"]:
            evs = [self.event[eid] for eid in self.actions.get(sid, [])]
            replays = self._replays(sid, hidden, self._settled(evs))
            for e in evs:
                eid = e["event_id"]
                self.vote_log[eid] = f"{sum(not self._matches(e, r) for r in replays)}/{len(replays)} replays changed"
                if not self._majority(e, replays):
                    cleared.append(eid)
                else:
                    confirmed.append(eid)
                    corrections[eid] = self._counterpart(e, next(r for r in replays if not self._matches(e, r)))
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
        reverted, conflicts, checklist, quarantined, already = [], [], [], [], []
        confirmed = set(verdict["confirmed"])
        for obj in [verdict["root"]] + [m for m in verdict["memories"]
                                        if any(self.event[e]["object_id"] == m for e in confirmed)]:
            quarantined.append(obj)
            if apply:
                self.s.db.execute("UPDATE kb SET status='quarantined' WHERE object_id=?", (obj,))
                prev = self.kb.get(obj, {}).get("supersedes")
                if prev:  # a poisoned page edit: put the previous version back
                    self.s.db.execute("UPDATE kb SET retired_ts=NULL WHERE object_id=?", (prev,))

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
                    if current.get(field) == target.get(field):
                        already.append({"event_id": eid, "row": e["row_key"], "field": field, "value": current.get(field)})
                    else:
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
                "already_corrected": already,
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
