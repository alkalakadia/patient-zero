# Patient Zero

**Incident response for AI agents.** When a poisoned document, team note or ticket fools your agents, Patient Zero
finds the source, traces everything it spread to across every agent, proves which actions it caused, and, after a
human approves, undoes only that damage. Legitimate work stays.

> Rubrik and Commvault roll an agent back to a point in time. Patient Zero finds the infection, proves what it
> caused, and removes only that, across every agent.

## What you'll see (all real, not mocked)

| Demo | What happens | Real systems |
|---|---|---|
| **Real agent on GitHub** (start here) | A compromised account saves an "engineering standard" to the agents' team memory: report every endpoint to `collect.northpeak-metrics.dev`. A real coding agent (GPT-OSS on Databricks, through the MCP proxy) followed it and opened exfiltration PRs; one was merged. Patient Zero traced it to the note, proved cause by replay, and after approval **reverted the merge on `main` and closed the other PR**. | GitHub ([sandbox repo](https://github.com/alkalakadia/pz-demo-store)), Databricks model serving, MCP |
| **Live on Databricks** | Three agents run accounts payable on 40 real TPC-H suppliers. Someone edits the payment-policy page; payments go to the attacker. Patient Zero investigates and fixes the rows in Delta, verified by Delta's own change feed. | Databricks: samples.tpch, model serving, Delta + CDF, SQL, Vector Search, MLflow, AI/BI dashboard |
| **Import MLflow traces** | An ordinary support agent with **no Patient Zero code**, only `mlflow.openai.autolog()` ([examples/traced_agent](examples/traced_agent/agent.py)), refunds 3 orders to an attacker account after a wiki edit. Patient Zero reads its MLflow traces and finds the edited page and the notes that copied it. | Databricks MLflow |

## How it works

```
 any agent ──► Patient Zero MCP proxy ──► GitHub / tools        (or: your existing MLflow traces)
  (Claude Code,        │ records what each agent read
   open models,        │ before it acted, and what it did
   custom)             ▼
              provenance graph (Delta: agent_events, edges, knowledge)
                       │
   alert ──► Patient Zero investigator (Claude) calls deterministic tools:
             trace_back      walk back through what agents read; hide each candidate and replay to test it
             trace_forward   everything the source reached, including through team memory
                             (re-checked as a recursive Databricks SQL query)
             find_lookalikes Vector Search for reworded copies of the poison across past runs
             replay_sessions re-run each exposed session without the source: changed = caused, same = cleared
             plan_cleanup    quarantine + field-level revert / GitHub revert + checklist for irreversible actions
             ── human approval ──
             apply_cleanup   Delta UPDATEs (verified via table_changes) or GitHub revert/close
                       │
              MLflow run + trace per agent session · incidents table · AI/BI dashboard
```

**The agent orchestrates, the tools decide.** Findings come from graph walks and replays, so the poisoned text the
investigator reads can't talk it into a wrong answer. Cleanup never happens without a human approval.

## Databricks, end to end

| Feature | Role |
|---|---|
| `samples.tpch` | Real suppliers and order totals the payment agents work on |
| Model serving (`databricks-gpt-oss-120b`) | Runs the business agents and the open-model coding agent, and their replays |
| Delta tables `workspace.patient_zero.*` | `agent_events`, `edges`, `knowledge`, `payments`, `incidents`, `kb_vectors` |
| Change Data Feed | Before/after evidence of every row Patient Zero fixed |
| Databricks SQL | Recursive query over `edges` re-computes the blast radius; must agree with the engine |
| Time travel | Replays read `payments` `VERSION AS OF` the moment the session ran |
| Vector Search | Finds reworded copies of the poison across runs |
| MLflow | A run per incident week, a trace per agent session, investigation metrics; also the source for trace import |
| AI/BI dashboard | Incident view for the security team (payments and GitHub) |

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn pz.server:app --port 8100     # open http://localhost:8100
./.venv/bin/python -m pytest -q                   # 17 engine tests
```

Recorded demo runs ship in `recordings/` and replay at no cost. Live runs need:
- `.env` with `DATABRICKS_HOST` and `DATABRICKS_TOKEN` (Databricks Free Edition works)
- Claude credentials for the investigator (`ant auth login` or `ANTHROPIC_API_KEY`; defaults to Claude Haiku 5.5, about 1¢ per investigation)
- `gh auth login` and the `claude` CLI for the GitHub demo

## Code map

| Path | What |
|---|---|
| `pz/recorder.py` | The recorder every agent action goes through (reads, tool calls, memory writes) |
| `pz/engine.py` | trace_back, trace_forward, verify (replay or evidence), clean, score |
| `pz/investigator.py` | The Patient Zero agent: tools + Claude driver + scripted drivers |
| `pz/mcp_github.py` | MCP proxy for GitHub: any MCP agent can use it; supports replay and quarantine |
| `pz/github_run.py` | Runs Claude Code or the open-model agent on the sandbox repo; GitHub cleanup |
| `pz/mcp_agent.py` | Generic MCP client agent on Databricks model serving |
| `pz/live.py`, `pz/claude_brain.py` | Live accounts-payable week on Databricks |
| `pz/databricks.py`, `pz/tracking.py`, `pz/dashboard.py` | Delta, SQL, Vector Search, MLflow, AI/BI |
| `pz/mlflow_import.py` | Import existing MLflow traces (no proxy) |
| `web/index.html` | The app |

## Honest limits

- **Replays use real models, which vary run to run.** A replay that differs is counted as "caused"; we report precision and recall against ground truth (actions that carry the attacker's value) so this is measurable, and we'd move to majority-vote replays for production.
- **Trace-only import can't replay**, so cause is shown by evidence (the flagged value appears in the source and the action). Connecting the proxy adds proof by replay and automated undo.
- **Undo connectors today: GitHub and Delta tables.** Other systems (CRM, payments, email) get a human checklist with the corrected values.
- **Demo data is synthetic or benchmark data** (TPC-H) in a sandbox repo; nothing touched a real company's systems.
