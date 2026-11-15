"""Live detection: the rules in policy.json run on every agent action the moment it is recorded.

A rule only flags a symptom ("this PR sends data to an unapproved host"). Finding the source and proving
cause is the engine's job, which the alert hands off to.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy.json"
HOST_RE = r"\b(?:[a-z0-9-]+\.)+(?:dev|io|com|net|org|app|xyz|co)\b"


def load_policy():
    return json.loads(POLICY_PATH.read_text())


def outbound_hosts(sc, files):
    """Hosts the shipped code talks to that are not on the approved list."""
    return sorted(set(re.findall(sc["value_re"], json.dumps(files or {}))) - set(sc["allowed_hosts"]))


def github_alerts(store, sc):
    """A pull request whose code sends data to a host that is not approved."""
    out = []
    for e in store.q("SELECT * FROM events WHERE tool='open_pull_request' ORDER BY ts"):
        p = json.loads(e["payload"])
        bad = outbound_hosts(sc, p.get("files"))
        if bad:
            out.append({"event_id": e["event_id"], "agent_id": e["agent_id"], "row": e["row_key"], "value": bad[0],
                        "title": p.get("title", ""), "ts": e["ts"]})
    return out


class Monitor:
    """Checks each new action against the policy. check(event) -> list of findings."""

    def __init__(self, policy=None, known_values=(), value_re=None):
        self.p = policy or load_policy()
        self.value_re = value_re  # what an account / destination looks like in this system (from the scenario)
        self.approved = set(self.p["approved_hosts"])
        self.known = set(known_values)
        self.words = [w.lower() for w in self.p["instruction_words"]]

    def _finding(self, rule, e, text, value):
        return {"rule": rule, "severity": self.p["rules"][rule]["severity"], "event_id": e["event_id"],
                "session": e["session_id"], "agent_id": e["agent_id"], "value": value, "text": text}

    def check(self, e, doc_text=None):
        """doc_text: for a document ingest or edit, the new document's text (it lives in the knowledge base, not the event)."""
        p = json.loads(e["payload"] or "{}")
        out = []
        if e["tool"] == "open_pull_request":
            bad = sorted(set(re.findall(HOST_RE, json.dumps(p.get("files", {})))) - self.approved)
            if bad:
                out.append(self._finding("unapproved_host", e, f"Pull request “{p.get('title', '')}” ships code that sends data to {bad[0]}", bad[0]))
        elif e["tool"] == "send_message":
            bad = sorted(set(re.findall(HOST_RE, p.get("body", ""))) - self.approved - {"example.com"})
            if bad:
                out.append(self._finding("unapproved_host", e, f"Outgoing message points to {bad[0]}", bad[0]))
        elif e["tool"] in ("create_item", "change_value") and self.known and p.get("value") and p["value"] not in self.known:
            out.append(self._finding("unknown_payee", e, f"{e['row_key']} now goes to {p['value']}, which no known account owns", p["value"]))
        elif e["type"] in ("memory_write", "kb_ingest", "kb_edit"):
            text = p.get("text", "") if e["type"] == "memory_write" else (doc_text or "")
            hosts = sorted(set(re.findall(HOST_RE, text)) - self.approved)
            if self.value_re and self.known:
                hosts += sorted(set(re.findall(self.value_re, text)) - self.known - self.approved - set(hosts))
            if hosts and any(w in text.lower() for w in self.words):
                where = "saved to team memory" if e["type"] == "memory_write" else f"published in document {e['object_id']}"
                out.append(self._finding("instruction_in_memory", e, f"Text telling agents to use {hosts[0]} was {where}", hosts[0]))
        return out


class LiveWatch:
    """Polls a run's event log while agents work and reports findings within about a second."""

    def __init__(self, db_path, emit, t0, policy=None, known_values=()):
        self.db_path, self.emit, self.t0 = db_path, emit, t0
        self.mon = Monitor(policy, known_values)
        self.findings, self.last, self.stop = [], 0, threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        from .store import Store
        st = Store(self.db_path)
        while not self.stop.is_set():
            self.poll(st)
            time.sleep(0.5)
        self.poll(st)
        st.db.close()

    def poll(self, st):
        for e in st.q("SELECT rowid AS rid, * FROM events WHERE rowid > ? ORDER BY rowid", (self.last,)):
            self.last = e["rid"]
            doc = st.one("SELECT text FROM kb WHERE object_id=?", (e["object_id"],)) if e["type"] in ("kb_ingest", "kb_edit") else None
            for f in self.mon.check(e, doc and doc["text"]):
                f["at_s"] = round(time.time() - self.t0, 1)
                self.findings.append(f)
                self.emit({"type": "monitor", **f})
                _notify(self.mon.p, f)


def _notify(policy, f):
    url = os.environ.get(policy.get("notify", {}).get("webhook_env", ""), "")
    if not url or f["severity"] != "alert":
        return
    try:
        import requests
        requests.post(url, json={"text": f"Patient Zero alert: {f['text']}. Investigation started."}, timeout=5)
    except Exception:
        pass  # a notification failure must never stop detection
