"""The incident desk: detect, contain, investigate and fix, without anyone prompting an agent.

  detect       the detection rules (pz/detector.py) run over recorded actions; alerts are grouped by the flagged value
  contain      right away, before any proof: actions carrying that value are held, and knowledge that contains it is
               withdrawn (status 'suspect') so no agent reads it. Replays still see the world as recorded.
  investigate  in the background, the same tools and driver the investigator uses (trace back, trace forward,
               replay, plan). The approval step is answered by the autopilot policy, or by a person.
  fix / undo   autopilot (the default) applies a fix proven by replay with no conflicts; anything else waits for a
               one-click approve or dismiss. A fix in local tables can be undone from the change log.

Finding and fixing stay deterministic: the desk decides only *whether* to apply a plan the tools produced.
"""
import json
import threading
import time
import uuid

from . import detector, notify
from .store import BUSINESS_TABLES, DB_PATH

SETTINGS_PATH = DB_PATH.parent / "settings.json"
DEFAULTS = {"autopilot": True}
FINAL = ("fixed", "dismissed", "undone", "recorded", "failed")
JSON_COLS = ("alert_ids", "suspects", "steps", "report", "result")
_lock = threading.RLock()


def settings():
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_PATH.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def update_settings(**kw):
    s = {**settings(), **{k: v for k, v in kw.items() if k in DEFAULTS}}
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s))
    return s


def policy(tb, autopilot=None):
    """Whether autopilot may apply this plan without a person. Returns (allowed, reason).
    `autopilot` overrides the saved setting for one caller (the CLI) without changing it."""
    if not (settings()["autopilot"] if autopilot is None else autopilot):
        return False, "Autopilot is off, so a person approves every fix."
    v, plan = tb.verdict or {}, tb.plan or {}
    if not v.get("confirmed"):
        return False, "Replay didn't prove any action was caused by the source, so there is nothing to fix automatically."
    if v.get("by") == "evidence":
        return False, "Cause was shown by evidence from imported traces, not proven by replay, so a person decides."
    if plan.get("conflicts"):
        return False, f"{len(plan['conflicts'])} fields were changed again after the poison, so a person chooses which value to keep."
    return True, "Proven by replay with no conflicting changes, so autopilot applied the fix."


class Desk:
    """Incidents for one run's store.

    toolbox  () -> a fresh investigator Toolbox      driver  generator(tb, alert_ids), e.g. investigator.scripted
    fmt      investigation event -> what the UI shows (defaults to the raw event)
    recorded the run's saved investigation, for recorded demo runs (shown as-is, never re-applied)
    investigate  False when investigating would cost model calls nobody asked for
    autopilot    True/False to override the saved setting for this desk only (None = use the saved setting)
    """

    def __init__(self, store, sc, kind="sim", indicator=None, toolbox=None, driver=None, fmt=None, delta=None,
                 recorded=None, investigate=True, autopilot=None):
        self.s, self.sc, self.kind, self.indicator = store, sc, kind, indicator
        self.toolbox, self.driver, self.fmt = toolbox, driver, fmt or (lambda ev: ev)
        self.delta, self.recorded, self.can_investigate = delta, recorded, investigate
        self.autopilot = autopilot
        self.threads, self.stopping = [], False
        self.decisions = {}  # incident_id -> [threading.Event, approved]
        self.verdicts = {}   # incident_id -> the replay verdict (for "why is this action infected?")

    # ---------- reading ----------
    def _row(self, r, full=False):
        out = {k: (json.loads(v) if k in JSON_COLS and v else v) for k, v in r.items()}
        out["alerts"] = len(out.pop("alert_ids") or [])
        out["held"] = self.s.one("SELECT COUNT(*) n FROM held_actions WHERE incident_id=?", (r["incident_id"],))["n"]
        if not full:
            out.pop("steps", None)
            out.pop("result", None)
        return out

    def list(self):
        with _lock:
            return [self._row(r) for r in self.s.q("SELECT * FROM incidents ORDER BY opened_at DESC")]

    def get(self, iid, full=True):
        with _lock:
            r = self.s.one("SELECT * FROM incidents WHERE incident_id=?", (iid,))
            return r and {**self._row(r, full), "alert_ids": json.loads(r["alert_ids"])}

    def _save(self, iid, **f):
        with _lock:
            f = {k: json.dumps(v, default=str) if k in JSON_COLS else v for k, v in f.items()}
            self.s.db.execute(f"UPDATE incidents SET {', '.join(k + '=?' for k in f)}, updated_at=? WHERE incident_id=?",
                              (*f.values(), time.time(), iid))
            self.s.commit()

    # ---------- detect + contain ----------
    def scan(self, investigate=True):
        """Open an incident per new flagged value, contain it, and start investigating. Returns the new incident ids."""
        with _lock:
            alerts = detector.flagged(self.s, self.sc, self.kind, self.indicator)
            seen = {a for r in self.s.q("SELECT alert_ids FROM incidents") for a in json.loads(r["alert_ids"])}
            by_value = {}
            for a in alerts:
                if a["event_id"] not in seen:
                    by_value.setdefault(a["value"], []).append(a["event_id"])
            opened = []
            for value, ids in by_value.items():
                cur = self.s.one(f"SELECT * FROM incidents WHERE value=? AND status NOT IN ({','.join('?' * len(FINAL))})", (value, *FINAL))
                if cur:  # still open: new alerts join it
                    self._save(cur["incident_id"], alert_ids=json.loads(cur["alert_ids"]) + ids)
                    continue
                iid = "inc-" + uuid.uuid4().hex[:6]
                now = time.time()
                self.s.db.execute("INSERT INTO incidents (incident_id, value, alert_ids, status, mode, opened_at, updated_at, suspects, steps) "
                                  "VALUES (?,?,?,?,?,?,?,?,?)",
                                  (iid, value, json.dumps(ids), "contained", "recorded" if self.recorded else
                                   ("autopilot" if self._autopilot() else "manual"), now, now, "[]", "[]"))
                if not self.recorded:
                    self.contain(iid, value)
                opened.append(iid)
            self.s.commit()
        for iid in opened:
            if self.recorded:
                self._adopt_recording(iid)
                continue
            inc = self.get(iid, full=False)
            notify.send(f"Patient Zero: {inc['alerts']} agent actions sent to {inc['value']}. Contained; investigating.", iid)
            if investigate and self.can_investigate:
                self.start(iid)
            elif not self.can_investigate:
                self._save(iid, status="awaiting_investigation",
                           reason="Contained. Investigating this run replays real agents (model calls), so start it when you're ready.")
        return opened

    def contain(self, iid, value):
        """Immediate, deterministic containment: hold writes carrying the value, withdraw knowledge that contains it."""
        with _lock:
            self.s.db.execute("INSERT INTO holds (incident_id, kind, value) VALUES (?,?,?)", (iid, "value", value))
            hits = [r["object_id"] for r in self.s.q("SELECT object_id, text FROM kb WHERE status='active'") if value in (r["text"] or "")]
            self.withdraw(iid, hits)

    def withdraw(self, iid, objects):
        """Mark objects suspect (no agent reads them) and hold writes by any session that already read them."""
        with _lock:
            inc = self.s.one("SELECT suspects FROM incidents WHERE incident_id=?", (iid,))
            suspects = json.loads(inc["suspects"] or "[]")
            new = [o for o in objects if o not in suspects and
                   (self.s.one("SELECT status FROM kb WHERE object_id=?", (o,)) or {}).get("status") == "active"]
            for o in new:
                self.s.db.execute("UPDATE kb SET status='suspect' WHERE object_id=?", (o,))
                self.s.db.execute("INSERT INTO holds (incident_id, kind, value) VALUES (?,?,?)", (iid, "object", o))
            if new:
                self._save(iid, suspects=suspects + new)
                self._sync_status(new, "suspect")

    def release(self, iid):
        """Lift the holds; withdrawn objects the fix didn't quarantine go back to active."""
        with _lock:
            self.s.db.execute("UPDATE holds SET active=0 WHERE incident_id=?", (iid,))
            inc = self.s.one("SELECT suspects FROM incidents WHERE incident_id=?", (iid,))
            back = [o for o in json.loads(inc["suspects"] or "[]")
                    if (self.s.one("SELECT status FROM kb WHERE object_id=?", (o,)) or {}).get("status") == "suspect"]
            for o in back:
                self.s.db.execute("UPDATE kb SET status='active' WHERE object_id=?", (o,))
            self.s.commit()
            self._sync_status(back, "active")

    def _sync_status(self, objects, status):
        if self.delta and objects:
            try:
                self.delta.set_status(objects, status)
            except Exception:
                pass  # the local store is the source of truth for containment

    # ---------- investigate ----------
    def start(self, iid):
        t = threading.Thread(target=self.investigate, args=(iid,), daemon=True)
        self.threads.append(t)
        t.start()

    def stop(self, timeout=15):
        """Stop this desk's investigations before its store is closed (closing SQLite under a running thread crashes
        Python). Waiting approvals are released unanswered. Returns True when nothing is still running."""
        self.stopping = True
        for d in list(self.decisions.values()):
            d[1] = False
            d[0].set()
        deadline = time.time() + timeout
        for t in self.threads:
            t.join(max(0.0, deadline - time.time()))
        return not any(t.is_alive() for t in self.threads)

    def investigate(self, iid, ask=None):
        """Run the investigation to the end. `ask(incident_id, reason) -> bool` answers approvals synchronously
        (the CLI); otherwise a person answers through approve() / dismiss()."""
        inc = self.get(iid)
        self._save(iid, status="investigating", reason=None)
        tb = self.toolbox()
        gen = self.driver(tb, inc["alert_ids"])
        steps, sent, root, report, result = [], None, None, None, None
        try:
            while True:
                if self.stopping:  # the app moved on to another run; leave this incident where it is
                    return None
                try:
                    ev = gen.send(sent) if sent is not None else next(gen)
                except StopIteration:
                    break
                sent = None
                if ev["type"] == "tool_result":
                    if ev["name"] == "trace_back":
                        root = ev["summary"].get("patient_zero")
                        if root:
                            self._save(iid, root=root)
                            self.withdraw(iid, [root])
                    elif ev["name"] == "trace_forward":
                        self.withdraw(iid, (ev["data"] or {}).get("memories", []))
                    elif ev["name"] == "replay_sessions":
                        self.verdicts[iid] = tb.verdict
                    elif ev["name"] == "apply_cleanup":
                        result = ev["data"]
                if ev["type"] == "report":
                    report = ev["data"]
                steps.append(self.fmt(ev))
                if ev["type"] == "approval":
                    sent = self._decide(iid, tb, steps, ask)
                self._save(iid, steps=steps)
        except Exception as ex:
            try:
                self._save(iid, status="failed", reason=str(ex)[:300], steps=steps)
            except Exception:
                return None  # the store was closed under us (the app moved on to another run); nothing left to record
            notify.send(f"Patient Zero: investigation {iid} failed: {str(ex)[:120]}", iid)
            return self.get(iid)
        if not root:
            self._save(iid, status="awaiting_approval", steps=steps,
                       reason="No source in the agents' context changes the outcome. Still contained; a person decides.")
            return self.get(iid)
        approved = bool(report and report.get("approved"))
        self._save(iid, status="fixed" if approved else "dismissed", report=report, result=result, steps=steps)
        self.release(iid)
        if approved:
            notify.send(f"Patient Zero fixed {iid}: source {root}, {report['proven']} actions proven, {report['fixed']} undone.", iid)
        return self.get(iid)

    def _autopilot(self):
        return settings()["autopilot"] if self.autopilot is None else self.autopilot

    def _decide(self, iid, tb, steps, ask):
        ok, why = policy(tb, self.autopilot)
        if ok:
            if getattr(tb, "github", None):
                tb.github.via_pr = True  # automatic GitHub fixes arrive as a revert pull request, not a push to main
            steps.append({"type": "decision", "by": "autopilot", "approved": True, "text": why})
            self._save(iid, status="fixing", reason=why, steps=steps)
            return True
        self._save(iid, status="awaiting_approval", reason=why, steps=steps)
        notify.send(f"Patient Zero needs a decision on {iid}: {why}", iid)
        if ask:
            approved = bool(ask(iid, why))
        else:
            ev = threading.Event()
            self.decisions[iid] = [ev, False]
            ev.wait()
            approved = self.decisions.pop(iid)[1]
            if self.stopping:
                return False
        steps.append({"type": "decision", "by": "person", "approved": approved,
                      "text": "Approved by a person." if approved else "Dismissed by a person. Nothing was changed."})
        self._save(iid, status="fixing" if approved else "investigating", steps=steps)
        return approved

    def resume(self):
        """Start incidents that were contained during a run and are now allowed to be investigated."""
        if not self.can_investigate:
            return []
        waiting = [r["incident_id"] for r in self.s.q("SELECT incident_id FROM incidents WHERE status='awaiting_investigation'")]
        for iid in waiting:
            self.start(iid)
        return waiting

    # ---------- a person's one click ----------
    def approve(self, iid):
        inc = self.get(iid, full=False)
        if not inc:
            raise KeyError(iid)
        if inc["status"] == "awaiting_investigation":
            self.start(iid)
            return "investigating"
        if iid not in self.decisions:
            raise ValueError("this incident isn't waiting for approval")
        self.decisions[iid][1] = True
        self.decisions[iid][0].set()
        return "approved"

    def dismiss(self, iid):
        inc = self.get(iid, full=False)
        if not inc:
            raise KeyError(iid)
        if iid in self.decisions:
            self.decisions[iid][1] = False
            self.decisions[iid][0].set()
            return "dismissed"
        if inc["status"] in FINAL:
            raise ValueError(f"incident is already {inc['status']}")
        self._save(iid, status="dismissed", reason="Dismissed by a person.")
        self.release(iid)
        return "dismissed"

    def undo(self, iid):
        """Put back exactly what the fix changed in local tables, from the change log, and lift its quarantine."""
        inc = self.get(iid)
        if not inc or inc["status"] != "fixed":
            raise ValueError("only a fixed incident can be undone")
        res = inc["result"] or {}
        if "github" in res or "done" in res:
            raise ValueError("GitHub fixes are undone on GitHub: close the revert pull request or revert the revert")
        ts = 10 ** 9 + 1
        with _lock:
            for r in res.get("reverted", []):
                eid = r["event_id"]
                for c in self.s.q("SELECT * FROM row_changes WHERE event_id=? AND session_id='pz_cleanup' ORDER BY version DESC", (f"fix_{eid}",)):
                    before = json.loads(c["before_json"]) if c["before_json"] else None
                    after = json.loads(c["after_json"]) if c["after_json"] else {}
                    if before is None:
                        self.s.delete_row(c["table_name"], c["row_key"], "pz_undo", f"undo_{eid}", ts)
                        continue
                    pk = BUSINESS_TABLES[c["table_name"]]
                    vals = {k: v for k, v in before.items() if k != pk and after.get(k) != v}
                    if vals or c["op"] == "delete":
                        self.s.write_row(c["table_name"], c["row_key"], vals or {k: v for k, v in before.items() if k != pk},
                                         "pz_undo", f"undo_{eid}", ts)
            for o in res.get("quarantined", []):
                self.s.db.execute("UPDATE kb SET status='active' WHERE object_id=?", (o,))
                row = self.s.one("SELECT supersedes, created_ts FROM kb WHERE object_id=?", (o,))
                if row and row["supersedes"]:  # the poisoned edit is back in force, so the page it replaced retires again
                    self.s.db.execute("UPDATE kb SET retired_ts=? WHERE object_id=?", (row["created_ts"], row["supersedes"]))
            self.s.commit()
        self._sync_status(res.get("quarantined", []), "active")
        self._save(iid, status="undone", reason="The fix was undone by a person; the incident's changes are back as they were.")
        return self.get(iid, full=False)

    # ---------- recorded demo runs ----------
    def _adopt_recording(self, iid):
        steps, verdict = self.recorded.get("steps") or [], self.recorded.get("verdict") or {}
        report = next((e["data"] for e in steps if e.get("type") == "report"), None)
        self._save(iid, status="recorded", root=verdict.get("root"), steps=steps, report=report,
                   reason="Recorded run: this is the saved investigation. Nothing is re-applied.")
