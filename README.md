# Patient Zero

**Contact tracing for AI agents.** When a poisoned document or memory compromises your agents, Patient Zero
finds the source, traces everything it spread to across every agent, proves which actions it actually caused,
and surgically undoes only that damage.

## Quick start
```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pz demo                    # terminal run, no API key needed
./.venv/bin/uvicorn pz.server:app --port 8100    # open http://localhost:8100
./.venv/bin/python -m pytest -q                  # regression tests
```

## Current demo result (simulated week, 176 sessions, 351 agent actions)
- Trace back: poisoned ticket D-7 confirmed as the source; real policy docs D-1, D-2 ruled out by counterfactual test
- Spread: 39 sessions across 3 agents, including second-hand infection through a worm memory
- Verify: 127 suspects narrowed to 38 proven infected (precision 30% -> 100%, recall 100% on planted ground truth)
- Clean: 32 rows reverted field by field, 5 irreversible emails on a human checklist,
  266 legitimate changes kept that a point-in-time restore would have destroyed

Note: these numbers come from the deterministic offline agents. Real-model numbers will differ.

See `CLAUDE.md` for architecture, data model and roadmap.
