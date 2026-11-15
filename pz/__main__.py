"""CLI: python -m pz [scenario] [--manual]

Runs a simulated scenario through the incident desk end to end (no API key needed): the detector opens an incident,
contains it, Patient Zero investigates, and the fix is applied by autopilot (default) or after you approve (--manual).
The choice applies to this run only; the saved setting the UI and server use (data/settings.json) is never written.
"""
import sys
import time

from . import incidents, server
from .scenarios import DEFAULT, SCENARIOS


def demo(scenario=DEFAULT, manual=False):
    server.load(scenario, autopilot=not manual)  # opens the desk; incidents start contained
    desk, st = server.state["desk"], server.state
    print(f"{SCENARIOS[scenario]['title']}  ({st['sc']['company']})")
    for iid in [i["incident_id"] for i in desk.list()]:
        while desk.get(iid, full=False)["status"] in ("contained", "investigating", "fixing") and iid not in desk.decisions:
            time.sleep(0.05)
        inc = desk.get(iid)
        print(f"{inc['alerts']} alerts -> incident {iid} on {inc['value']} (contained: {', '.join(inc['suspects']) or 'nothing yet'})\n")
        for ev in inc["steps"]:
            if ev["type"] == "thought":
                print(f"  > {ev['text']}")
            elif ev["type"] == "tool_call":
                print(f"  [{ev['name']}]")
            elif ev["type"] == "decision":
                print(f"  [{ev['by']}] {ev['text']}")
        if inc["status"] == "awaiting_approval" and iid in desk.decisions:
            print(f"\n  Needs you: {inc['reason']}")
            ok = (input("  Approve the fix? [y/N] ").strip().lower() == "y") if sys.stdin.isatty() else True
            desk.approve(iid) if ok else desk.dismiss(iid)
            while desk.get(iid, full=False)["status"] not in incidents.FINAL:
                time.sleep(0.05)
            inc = desk.get(iid)
            print(f"  [person] {'approved' if ok else 'dismissed'}")
        r = inc["report"] or {}
        print(f"\n{inc['status'].upper()}: source {inc['root']}, {r.get('proven', 0)} actions proven, {r.get('cleared', 0)} cleared, "
              f"{r.get('fixed', 0)} rows fixed, {r.get('checklist', 0)} for a human, {r.get('amount_corrected') or '-'} corrected")
        if r.get("score"):
            print(f"Precision {r['score']['precision']}, recall {r['score']['recall']} (against the planted attack)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    demo(args[0] if args else DEFAULT, manual="--manual" in sys.argv)
