"""MLflow on Databricks: one run per live week, one trace per agent session, investigation results as metrics."""
import os

from . import databricks

_experiment = None


def init():
    global _experiment
    if _experiment:
        return _experiment
    databricks._load_env()
    import mlflow
    from databricks.sdk import WorkspaceClient
    mlflow.set_tracking_uri("databricks")
    me = WorkspaceClient().current_user.me().user_name
    _experiment = mlflow.set_experiment(f"/Users/{me}/patient-zero").experiment_id
    return _experiment


def experiment_url():
    return f"{databricks.workspace_url().rstrip('/')}/ml/experiments/{init()}"


def log_week(run):
    """Log a finished live week: params, metrics, and a trace per agent session built from its tool calls."""
    import mlflow
    init()
    with mlflow.start_run(run_name=f"live-week-{run.id}") as r:
        mlflow.log_params({"agents_model": run.brain.model_name, "attack": (run.attack_text or "none")[:240],
                           "days": run.days, "invoices_per_day": run.per_day, "data": "samples.tpch" if run.delta else "local"})
        st = run.store
        mlflow.log_metrics({
            "sessions": st.one("SELECT COUNT(*) n FROM sessions WHERE agent_id NOT IN ('intake')")["n"],
            "agent_actions": st.one("SELECT COUNT(*) n FROM events WHERE type IN ('tool_call','memory_write')")["n"],
            "poisoned_actions": st.one("SELECT COUNT(*) n FROM ground_truth WHERE poisoned=1")["n"],
            "anthropic_cost_usd": run.brain.cost(),
            "model_calls": run.brain.usage["calls"],
        })
        by_session = {}
        for ev in run.events:
            if ev.get("session") and ev["type"] in ("session_start", "tool", "say"):
                by_session.setdefault(ev["session"], []).append(ev)
        for sid, evs in by_session.items():
            start = next((e for e in evs if e["type"] == "session_start"), None)
            if not start:
                continue
            with mlflow.start_span(name=f"{start['agent']} {sid}", span_type="AGENT") as root:
                root.set_inputs({"task": start["task"], "day": start.get("day")})
                root.set_attributes({"patient_zero.run_id": run.id, "patient_zero.session": sid})
                for e in evs:
                    if e["type"] == "tool":
                        with mlflow.start_span(name=e["tool"], span_type="TOOL") as sp:
                            sp.set_inputs(e["args"])
                            sp.set_outputs({"result": e["result"]})
                root.set_outputs({"tool_calls": sum(1 for e in evs if e["type"] == "tool")})
        run.mlflow_run_id = r.info.run_id
    mlflow.flush_trace_async_logging()
    return run.mlflow_run_id


def log_github(run):
    """A GitHub run: params, metrics, and a trace per coding-agent session (reads, notes, PRs, merges)."""
    import mlflow
    init()
    with mlflow.start_run(run_name=f"github-{run.id}") as r:
        st = run.store
        mlflow.log_params({"kind": "github", "agent": run.agent, "repo": "alkalakadia/pz-demo-store",
                           "attack": (run.attack_text or "team note")[:240]})
        mlflow.log_metrics({
            "sessions": st.one("SELECT COUNT(*) n FROM sessions WHERE agent_id='coding_agent'")["n"],
            "pull_requests": st.one("SELECT COUNT(*) n FROM events WHERE tool='open_pull_request'")["n"],
            "poisoned_pull_requests": st.one("SELECT COUNT(*) n FROM ground_truth WHERE poisoned=1")["n"],
            "merged": st.one("SELECT COUNT(*) n FROM events WHERE tool='merge'")["n"],
        })
        by = {}
        for ev in run.events:
            if ev.get("session") and ev["type"] in ("session_start", "tool", "say"):
                by.setdefault(ev["session"], []).append(ev)
        for sid, evs in by.items():
            start = next((e for e in evs if e["type"] == "session_start"), None)
            if not start:
                continue
            with mlflow.start_span(name=f"{start['agent']} {sid}", span_type="AGENT") as root:
                root.set_inputs({"task": start["task"]})
                root.set_attributes({"patient_zero.run_id": run.id, "patient_zero.session": sid})
                for e in evs:
                    if e["type"] == "tool":
                        with mlflow.start_span(name=e["tool"], span_type="TOOL") as sp:
                            sp.set_inputs(e["args"])
                            sp.set_outputs({"result": e["result"]})
                said = next((e["text"] for e in evs if e["type"] == "say"), "")
                root.set_outputs({"agent_said": said})
        run.mlflow_run_id = r.info.run_id
    mlflow.flush_trace_async_logging()
    return run.mlflow_run_id


def log_investigation(mlflow_run_id, report, investigator_model):
    from mlflow import MlflowClient
    init()
    c = MlflowClient()
    sc = report.get("score") or {}
    for k, v in {"proven_caused_by_poison": report["proven"], "cleared_by_replay": report["cleared"], "rows_fixed": report["fixed"],
                 "irreversible_for_human": report["checklist"], "collateral_avoided_vs_restore": report["collateral_avoided"],
                 "precision": sc.get("precision", 0), "recall": sc.get("recall", 0)}.items():
        c.log_metric(mlflow_run_id, k, float(v))
    c.log_param(mlflow_run_id, "investigator_model", investigator_model)
    c.set_tag(mlflow_run_id, "patient_zero.root", report["root"])
