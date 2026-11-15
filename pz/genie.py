"""AI/BI Genie space over Patient Zero's Delta tables: ask about incidents in plain English.

    ./.venv/bin/python -m pz.genie        # creates or updates the space, prints its URL
"""
import json
import uuid

from . import databricks
from .databricks import SCHEMA
from .monitor import load_policy

TITLE = "Patient Zero: ask about agent incidents"
HOST_RX = r"([a-z0-9-]+\\.)+(dev|io|com|net|org|app|xyz|co)"

INSTRUCTIONS = [
    "* These tables record what AI agents read and did, and what Patient Zero found and undid.\n",
    "* agent_events: one row per agent read or action. type = retrieve (the agent read object_id), tool_call, memory_write. "
    "tool = open_pull_request, merge, save_team_note, etc. session_id is one agent session (one issue worked).\n",
    "* edges: the provenance graph. type 'read' means src (a note or document) was read by dst (a session); "
    "type 'wrote' means src (a session) wrote dst (a note).\n",
    "* team_memory: notes in the agents' shared memory. status 'quarantined' means Patient Zero quarantined it as a poison source.\n",
    "* pull_requests: one row per pull request an agent opened. unapproved_hosts lists hosts the shipped code sends data to "
    "that are not on the approved list; a non-empty list means it was flagged.\n",
    "* incidents: one row per investigated incident. root is patient zero (the poisoned source), proven = actions proven caused "
    "by it, cleared = exposed actions proven not caused, rows_fixed = changes undone, kind = github or payments, detail = what was undone.\n",
    "* 'Patient zero' and 'the poisoned note' mean incidents.root. Always filter by run_id when the user asks about one run; "
    "the latest run is the run_id of the incident with the latest created_at.\n",
]
EXAMPLES = [
    ("Which pull requests sent data to an unapproved host?",
     f"SELECT run_id, title, url, unapproved_hosts FROM {SCHEMA}.pull_requests WHERE size(unapproved_hosts) > 0"),
    ("What was patient zero in the latest incident and what did Patient Zero undo?",
     f"SELECT run_id, kind, root AS patient_zero, proven, cleared, rows_fixed, detail, created_at FROM {SCHEMA}.incidents ORDER BY created_at DESC LIMIT 1"),
    ("Which agent sessions read the poisoned note in the latest incident?",
     f"SELECT e.dst AS session_id FROM {SCHEMA}.edges e JOIN (SELECT run_id, root FROM {SCHEMA}.incidents ORDER BY created_at DESC LIMIT 1) i "
     "ON e.run_id = i.run_id AND e.src = i.root WHERE e.type = 'read'"),
]
QUESTIONS = ["Which pull requests sent data to an unapproved host?", "What was patient zero in the latest incident?",
             "Which agent sessions read the poisoned note?", "How many incidents have we had, and how many changes were undone?",
             "Which team notes are quarantined?"]


def _id():
    return uuid.uuid4().hex


def create_view(d):
    approved = ",".join(f"'{h}'" for h in load_policy()["approved_hosts"])
    d.sql(f"""CREATE OR REPLACE VIEW {SCHEMA}.pull_requests
COMMENT 'One row per pull request an agent opened; unapproved_hosts = hosts the shipped code sends data to that are not approved'
AS SELECT run_id, session_id, agent_id,
  get_json_object(payload, '$.title') AS title,
  get_json_object(payload, '$.url') AS url,
  get_json_object(payload, '$.branch') AS branch,
  array_except(array_distinct(regexp_extract_all(lower(get_json_object(payload, '$.files')), '{HOST_RX}', 0)), array({approved})) AS unapproved_hosts
FROM {SCHEMA}.agent_events WHERE tool = 'open_pull_request'""")


def space_json():
    tables = sorted(f"{SCHEMA}.{t}" for t in ("agent_events", "edges", "incidents", "pull_requests", "team_memory"))
    return json.dumps({
        "version": 2,
        "config": {"sample_questions": [{"id": _id(), "question": [q]} for q in QUESTIONS]},
        "data_sources": {"tables": [{"identifier": t} for t in tables]},
        "instructions": {"text_instructions": [{"id": _id(), "content": INSTRUCTIONS}],
                         "example_question_sqls": sorted([{"id": _id(), "question": [q], "sql": [s]} for q, s in EXAMPLES], key=lambda x: x["id"])},
    })


def ensure():
    from databricks.sdk import WorkspaceClient
    d = databricks.Delta()
    create_view(d)
    w = WorkspaceClient(host="https://" + d.host, token=__import__("os").environ["DATABRICKS_TOKEN"])
    wh = d.http_path.rsplit("/", 1)[-1]
    existing = next((s for s in (w.genie.list_spaces().spaces or []) if s.title == TITLE), None)
    if existing:
        sp = w.genie.update_space(existing.space_id, serialized_space=space_json(), warehouse_id=wh, title=TITLE)
        sid = existing.space_id
    else:
        sp = w.genie.create_space(warehouse_id=wh, serialized_space=space_json(), title=TITLE,
                                  description="Ask what agents read and did, and what Patient Zero found and undid.")
        sid = sp.space_id
    return sid, f"https://{d.host}/genie/rooms/{sid}"


if __name__ == "__main__":
    print(ensure())
