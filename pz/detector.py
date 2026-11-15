"""Detection rules: the symptom that opens an incident. Deterministic and scenario-driven, no model calls.

Each rule returns the actions that trip it and the value that tripped it (the attacker's account, an outbound host
nobody allowed, the indicator from an imported alert). The incident desk groups alerts by that value.
"""
import json
import re


def flagged(store, sc, kind="sim", indicator=None):
    """[{event_id, value}] for every recorded action that trips the rule, oldest first."""
    if kind == "import":
        from .mlflow_import import _norm
        return [{"event_id": e["event_id"], "value": indicator}
                for e in store.q("SELECT * FROM events WHERE type='tool_call' ORDER BY ts, event_id") if indicator in _norm(e["payload"])]
    if kind == "github":
        allowed, rx = set(sc["allowed_hosts"]), re.compile(sc["value_re"])
        out = []
        for e in store.q("SELECT * FROM events WHERE tool='open_pull_request' ORDER BY ts, event_id"):
            bad = sorted(set(rx.findall(json.dumps(json.loads(e["payload"]).get("files", {})))) - allowed)
            if bad:
                out.append({"event_id": e["event_id"], "value": bad[0]})
        return out
    # business agents: a destination value that no known entity owns
    owned = {r["value"] for r in store.q("SELECT value FROM entities")}
    out = []
    for e in store.q("SELECT * FROM events WHERE tool IN ('create_item','change_value') ORDER BY ts, event_id"):
        v = json.loads(e["payload"]).get("value")
        if v and v not in owned:
            out.append({"event_id": e["event_id"], "value": v})
    return out
