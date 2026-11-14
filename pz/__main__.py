"""CLI: python -m pz [scenario]   runs a scenario and the scripted investigator end to end (no API key needed)."""
import json
import sys

from . import server
from .investigator import Toolbox, scripted
from .scenarios import DEFAULT, SCENARIOS


def demo(scenario=DEFAULT):
    server.load(scenario)
    st = server.state
    print(f"{SCENARIOS[scenario]['title']}  ({st['sc']['company']})")
    alerts = server.alerts()
    pick = next((a for a in alerts if a["role"] == "reviewer"), alerts[0])
    print(f"{len(alerts)} alerts. Investigating: {pick['text']}\n")
    gen = scripted(Toolbox(st["engine"], st["sc"], server.label, server.agent_name), [pick["event_id"]])
    value = None
    while True:
        try:
            ev = gen.send(value) if value is not None else next(gen)
        except StopIteration:
            break
        value = None
        if ev["type"] == "thought":
            print(f"  > {ev['text']}")
        elif ev["type"] == "tool_call":
            print(f"  [{ev['name']}]")
        elif ev["type"] == "approval":
            print("  [approval requested -> approved]")
            value = True
        elif ev["type"] == "report":
            print("\n" + json.dumps({k: v for k, v in ev["data"].items() if k != "spread"}, indent=2))


if __name__ == "__main__":
    demo(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
