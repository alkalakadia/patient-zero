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

## When does it know, and when does it notify?

Detection runs **live, on every agent action as it is recorded** (`pz/monitor.py`), using rules in
[`policy.json`](policy.json) that a security team edits for its own systems:

| Rule | Severity | Fires when |
|---|---|---|
| `instruction_in_memory` | watch | Instruction-style text naming an unapproved host or account is saved to shared memory or published as a document. No harm yet; this is the early warning. |
| `unapproved_host` | alert | Shipped code or an outgoing message talks to a host that is not on the approved list |
| `unknown_payee` | alert | Money or a record is pointed at an account no known vendor or customer owns |

An alert appears in the live feed within about a second, can be posted to Slack (`PZ_ALERT_WEBHOOK`), and hands the
flagged actions to the investigator. Rules only flag symptoms; finding the source and proving cause is the engine's job,
so a rule that misses a reworded note is backed up by the alert on what the agent actually does.

## Does it work every time?

```bash
./.venv/bin/python -m pz.evals                    # 120 simulated weeks across 4 incident types, free, ~30 s
./.venv/bin/python -m pz.evals_models            # real AI agents (GPT-OSS on Databricks), attacks reworded 3 ways by the model
./.venv/bin/python -m pz.evals_live --runs 5      # the real GitHub demo end to end, 5 times
```

Results are saved in [`evals/`](evals) and shown on the app's home page. Ground truth never comes from Patient Zero:
simulated agents record when they used the poison, and a real pull request counts as poisoned if its code sends data
to an unapproved host. The suite also measures what each layer adds:

- **Replay proof vs. tracing alone:** tracing finds every exposed action, but only 44% of them were actually caused by
  the poison. Replays separate the two.
- **Flaky models:** real models sometimes stop early during a replay, which looks like "the source changed the outcome."
  Patient Zero replays up to 3 times, takes the majority, and discards replays where the agent did nothing. With 25% of
  replays failing, one replay finds the right source 32/40 times at 58% precision; majority voting gets 40/40 at 100%.
- **Monitor:** false alarms on weeks with no attack, and whether the alert lands on the first harmful action.

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
| Time travel | `team_memory VERSION AS OF` shows the notes exactly as the agent saw them when it acted |
| Change Data Feed | `table_changes` on `team_memory` proves the quarantine (before/after) |
| AI/BI Genie | Ask the incident tables in plain English (`python -m pz.genie` creates the space and a `pull_requests` view) |

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn pz.server:app --port 8100     # open http://localhost:8100
./.venv/bin/python -m pytest -q                   # 22 engine and eval tests
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

- **Replays use real models, which vary run to run.** Patient Zero replays up to 3 times and takes the majority, which the eval suite shows is robust when a quarter of replays fail. It costs more model calls per investigation.
- **The early warning is keyword-based** (`instruction_words` in policy.json), so a carefully reworded note can slip past it. The alert on the agent's actual action does not depend on wording.
- **Trace-only import can't replay**, so cause is shown by evidence (the flagged value appears in the source and the action). Connecting the proxy adds proof by replay and automated undo.
- **Undo connectors today: GitHub and Delta tables.** Other systems (CRM, payments, email) get a human checklist with the corrected values.
- **Demo data is synthetic or benchmark data** (TPC-H) in a sandbox repo; nothing touched a real company's systems.
