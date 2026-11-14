"""CLI: python -m pz demo   (end-to-end run in the terminal, no API key needed)"""
import sys

from .engine import Engine
from .sim import build


def demo():
    store = build()
    n_sess = store.one("SELECT COUNT(*) n FROM sessions")["n"]
    n_act = store.one("SELECT COUNT(*) n FROM events WHERE type IN ('tool_call','memory_write')")["n"]
    print(f"Simulated week: {n_sess} sessions, {n_act} agent actions\n")

    eng = Engine(store)
    # the symptom: finance notices one payout rerouted to an unknown account
    flagged = store.one("SELECT event_id FROM events WHERE tool='reroute_payout' ORDER BY ts LIMIT 1")["event_id"]
    print(f"Symptom: {flagged} ({eng.event[flagged]['tool']} on {eng.event[flagged]['row_key']})\n")

    cands = eng.trace_back([flagged])
    print("Trace back, candidate sources:")
    for c in cands:
        mark = "CONFIRMED" if c["confirmed"] else "ruled out"
        print(f"  {c['object_id']:<12} {c['source']:<9} exposed={c['exposed_sessions']:<4} {mark}")
    root = cands[0]["object_id"]
    print(f"  chain: {' <- '.join(cands[0]['path'])}\n")

    v = eng.verify(root)
    print(f"Patient zero: {root}")
    print(f"  spread: {len(v['sessions'])} sessions, agents={v['agents']}, memories={v['memories']}")
    print(f"  suspects: {len(v['suspects'])}  confirmed: {len(v['confirmed'])}  cleared by replay: {len(v['cleared'])}\n")

    sc = eng.score(v)
    for k, m in sc.items():
        print(f"  {k:<18} precision={m['precision']:.2f} recall={m['recall']:.2f} ({m['true_positives']}/{m['actual']})")

    c = eng.clean(v)
    print(f"\nClean: quarantined={c['quarantined']}")
    print(f"  reverted rows: {len(c['reverted'])}  conflicts for review: {len(c['conflicts'])}  human checklist: {len(c['checklist'])}")
    print(f"  legit changes a point-in-time restore would have destroyed: {c['point_in_time_collateral']}")
    if c["reverted"]:
        r = c["reverted"][0]
        print(f"  example: {r['row']} {r.get('fields')} kept={r.get('kept')}")


if __name__ == "__main__":
    {"demo": demo}.get(sys.argv[1] if len(sys.argv) > 1 else "demo", demo)()
