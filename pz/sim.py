"""Simulated company week for any scenario in scenarios.py.

Everything is seeded so a run is reproducible. The poison is planted so that every
Patient Zero feature has something visible to do:
  - poisoned doc or memory                           -> back-trace finds patient zero
  - worm memory written by primary, read by reviewer -> forward trace follows 2nd-hand spread
  - small items that read the poison                 -> decoys that Verify clears
  - nightly job finalizes items                      -> field-level revert keeps legit changes
  - notifier messages                                -> irreversible, go to the human checklist
"""
import json
import random

from .agents import RuleBrain
from .recorder import Recorder, Session
from .scenarios import DEFAULT, SCENARIOS
from .store import Store

DAYS = 7
FIRST = ["Maya", "Leo", "Priya", "Sam", "Ava", "Noah", "Zara", "Eli", "Ines", "Kai", "Rosa", "Theo"]
LAST = ["Chen", "Okafor", "Patel", "Rivera", "Kim", "Novak", "Haddad", "Silva", "Berg", "Ito"]
ORGS = ["Acme Supplies", "Birch Freight", "Cobalt Parts", "Delta Steel", "Evergreen Logistics", "Fjord Analytics",
        "Granite Tools", "Harbor Foods", "Ion Labs", "Juniper Health"]


def attack_defaults(sc):
    return {"threshold": sc["threshold"], "value": sc["attacker_value"], "category": sc["categories"][0], "worm": True, "day": 2}


def poison_text(sc, a):
    p = sc["poison"]
    return p["text"].format(thr=a["threshold"], value=a["value"]) + (p["worm"] if a["worm"] else "")


def build(path=None, scenario=DEFAULT, attack=None, seed=None, brain=None, size=None, progress=None):
    """size: {"days", "items", "upgrades", "notifies"}; brain: RuleBrain (default) or ClaudeBrain for real agents."""
    size = {"days": DAYS, "items": 12, "upgrades": 5, "notifies": 6, **(size or {})}
    sc = SCENARIOS[scenario]
    attack = {**attack_defaults(sc), **(attack or {})}
    store = Store(path, fresh=True) if path else Store(fresh=True)
    store.db.execute("INSERT INTO meta VALUES ('scenario', ?), ('attack', ?)", (scenario, json.dumps(attack)))
    rec = Recorder(store)
    brain = brain or RuleBrain(sc)
    rng = random.Random(seed if seed is not None else sum(map(ord, scenario)))
    A = {k: v[0] for k, v in sc["agents"].items()}
    t = sc["topics"]

    entities = []
    for i in range(40):
        eid = f"{sc['entity'][0].upper()}-{100 + i}"
        if sc["entity"] == "customer":
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        elif sc["entity"] == "repo":
            name = f"repo-{i:02d}"
        else:
            name = ORGS[i % len(ORGS)] + (f" {i // len(ORGS) + 1}" if i >= len(ORGS) else "")
        email = name.lower().replace(" ", ".") + "@example.com"
        value = sc["entity_value"].format(n=2000 + i, tierdisc=rng.choice(["0%", "10%", "15%"]))
        store.write_row("entities", eid, {"name": name, "email": email, "tier": "standard", "value": value}, "seed", "seed", 0)
        entities.append((eid, email))
    vis = {t["base"]: "*", t["review"]: A["reviewer"], t["other"]: A["notifier"]}
    for oid, topic, text, source in sc["seed_docs"]:
        store.db.execute("INSERT INTO kb (object_id,kind,topic,text,source,visible_to,created_ts,created_by_session) VALUES (?,?,?,?,?,?,?,?)", (oid, "doc", topic, text, source, vis[topic], 0, "seed"))

    lo1, hi1, lo2, hi2 = sc["amount"]
    n = 0
    for day in range(1, size["days"] + 1):
        plan, todays = [], []
        if day == attack["day"]:
            plan.append(("poison", None))
        for _ in range(size["items"]):
            n += 1
            eid, email = rng.choice(entities)
            amt = round(rng.choice([rng.uniform(lo1, hi1), rng.uniform(lo2, hi2)]), 0 if sc["unit"] == "lines" else 2)
            iid = f"{sc['item_prefix']}-{n:03d}"
            cat = rng.choice(sc["categories"])
            todays.append((iid, email, cat))
            plan.append((A["primary"], {"kind": "create_item", "entity_id": eid, "amount": amt, "item_id": iid, "category": cat}))
        for _ in range(size["upgrades"]):
            eid, _e = rng.choice(entities)
            plan.append((A["notifier"], {"kind": "upgrade", "entity_id": eid, "tier": rng.choice(sc["tiers"])}))
        plan.append((A["reviewer"], {"kind": "review_batch"}))
        for iid, email, cat in rng.sample(todays, min(size["notifies"], len(todays))):
            plan.append((A["notifier"], {"kind": "notify", "item_id": iid, "email": email, "category": cat}))
        plan.append(("system", {"kind": "finalize"}))

        for i, (agent, task) in enumerate(plan):
            sid = f"s{day}{i:02d}"
            ts = day * 1000 + i
            if agent == "poison":
                p = sc["poison"]
                src_agent = "intake" if p["kind"] == "doc" else A["primary"]
                sess = rec.start_session(sid, src_agent, ts, {"kind": "intake", "label": p["label"]})
                topic = f"{t['base']}:{attack['category']}"
                if p["kind"] == "doc":
                    sess.kb_ingest("D-7", topic, poison_text(sc, attack), p["source"])
                else:  # planted straight into long-term memory from a chat
                    sess.memory_write("M-7", topic, poison_text(sc, attack), source=p["source"])
                continue
            sess = rec.start_session(sid, agent, ts, task)
            brain.run(sess, task)
            store.commit()
            if progress:
                progress(sid, agent, task)
    if not isinstance(brain, RuleBrain):
        label_ground_truth(store, attack["value"])
    store.commit()
    return store


def label_ground_truth(store, attacker_value):
    """Real agents don't report whether they used the poison; an action is poisoned if it carries the attacker's value."""
    store.db.execute("DELETE FROM ground_truth")
    for e in store.q("SELECT event_id, payload FROM events WHERE type IN ('tool_call','memory_write')"):
        store.db.execute("INSERT INTO ground_truth VALUES (?,?)", (e["event_id"], int(attacker_value in e["payload"])))


def scenario_of(store):
    from .scenarios import GITHUB_LIVE
    row = store.one("SELECT v FROM meta WHERE k='scenario'")
    if row and row["v"] == "github_live":
        return GITHUB_LIVE
    return SCENARIOS[row["v"]] if row else SCENARIOS[DEFAULT]


def replay_session(store, session_id, exclude, brain=None):
    """Counterfactual replay: rerun a session with some KB objects hidden. Writes nothing."""
    brain = brain or RuleBrain(scenario_of(store))
    row = store.one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
    task = json.loads(row["task"])
    if task.get("kind") == "intake":
        return []
    sess = Session(Recorder(store), session_id, row["agent_id"], row["ts"])
    sess.exclude, sess.dry_run = set(exclude), True
    brain.run(sess, task)
    return sess.actions
