# Patient Zero

**Incident response for AI agents.** When a poisoned email, GitHub issue, ticket or memory compromises your agents,
Patient Zero's investigator agent finds the source, traces everything it spread to across every agent, proves which
actions it caused by replaying them without it, and, after a human approves, undoes only that damage.

## Run it
```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn pz.server:app --port 8100    # open http://localhost:8100
./.venv/bin/python -m pz github                  # terminal run of one scenario
./.venv/bin/python -m pytest -q                  # 17 tests across all scenarios
```
Set `ANTHROPIC_API_KEY` to have Claude drive the investigation live; without it the investigator runs in offline
mode with scripted reasoning over the same tools (the UI says which mode is on).

## Scenarios (fictional companies, modeled on real incidents)
| Scenario | Inspired by |
|---|---|
| Poisoned email to an accounts-payable assistant | EchoLeak, CVE-2025-32711, Microsoft 365 Copilot (June 2025) |
| Malicious GitHub issue to a coding agent | GitHub MCP prompt injection, Invariant Labs (May 2025) |
| Poisoned long-term memory in a sales assistant | SpAIware, ChatGPT memory (September 2024) |
| Refund fraud through a support ticket | United Airlines support chatbot (single reported case) |

## How the investigator works
Alert -> `trace_back` (walk back through what each agent read; hide each candidate and replay) -> `trace_forward`
(every session, agent and memory it reached) -> `replay_sessions` (actions that change without the poison were caused
by it) -> `plan_cleanup` -> human approval -> `apply_cleanup` (field-level revert from the change feed, quarantine,
checklist for irreversible messages) -> report.

The agent orchestrates; the tools decide. Findings come from graph walks and replays, so the poisoned text the
investigator reads can't talk it into a wrong answer. Precision/recall numbers come from deterministic simulated
agents with planted ground truth; real-model runs will be noisier.

See `CLAUDE.md` for architecture and roadmap.
