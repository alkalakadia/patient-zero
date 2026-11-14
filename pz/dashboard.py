"""Creates (or updates) the Patient Zero AI/BI dashboard in Databricks over the Delta tables."""
import json

from . import databricks

S = databricks.SCHEMA
NAME = "Patient Zero: agent incident response"


def _ds(name, label, sql):
    return {"name": name, "displayName": label, "queryLines": [sql]}


def _counter(name, ds, field, label, x, y):
    return {"widget": {"name": name, "queries": [{"name": "main_query", "query": {"datasetName": ds, "fields": [{"name": field, "expression": f"`{field}`"}], "disaggregated": True}}],
                       "spec": {"version": 2, "widgetType": "counter", "encodings": {"value": {"fieldName": field, "displayName": label}},
                                "frame": {"showTitle": True, "title": label}}},
            "position": {"x": x, "y": y, "width": 2, "height": 3}}


def _table(name, ds, cols, title, x, y, w=6, h=7):
    return {"widget": {"name": name, "queries": [{"name": "main_query", "query": {"datasetName": ds, "fields": [{"name": c, "expression": f"`{c}`"} for c in cols], "disaggregated": True}}],
                       "spec": {"version": 2, "widgetType": "table", "encodings": {"columns": [{"fieldName": c, "displayName": c} for c in cols]},
                                "frame": {"showTitle": True, "title": title}}},
            "position": {"x": x, "y": y, "width": w, "height": h}}


def spec():
    datasets = [
        _ds("incidents", "Incidents", f"SELECT coalesce(kind, 'payments') AS kind, run_id, root, proven, cleared, rows_fixed, detail, created_at FROM {S}.incidents ORDER BY created_at DESC"),
        _ds("totals", "Totals", f"SELECT count(*) AS incidents, coalesce(sum(proven),0) AS actions_proven, coalesce(sum(rows_fixed),0) AS rows_fixed FROM {S}.incidents"),
        _ds("cdf", "Cleanup change feed", f"SELECT item_id, value, status, _change_type, _commit_version, _commit_timestamp FROM table_changes('{S}.payments', 1) "
                                          "WHERE _change_type IN ('update_preimage','update_postimage') ORDER BY _commit_version DESC LIMIT 200"),
        _ds("events", "Agent activity", f"SELECT run_id, agent_id, tool, count(*) AS actions FROM {S}.agent_events WHERE type='tool_call' GROUP BY ALL ORDER BY run_id, actions DESC"),
    ]
    layout = [
        {"widget": {"name": "title", "multilineTextboxSpec": {"lines": ["## Patient Zero\n", "Incident response for AI agents: what was poisoned, what it caused, what was undone."]}},
         "position": {"x": 0, "y": 0, "width": 6, "height": 2}},
        _counter("c1", "totals", "incidents", "Incidents", 0, 2),
        _counter("c2", "totals", "actions_proven", "Agent actions proven caused by poison", 2, 2),
        _counter("c3", "totals", "rows_fixed", "Changes undone (Delta rows + GitHub)", 4, 2),
        _table("t1", "incidents", ["kind", "run_id", "root", "proven", "rows_fixed", "detail", "created_at"], "Incidents (payments and GitHub)", 0, 5),
        _table("t2", "cdf", ["item_id", "value", "status", "_change_type", "_commit_version", "_commit_timestamp"], "Change Data Feed: payments", 0, 12),
        _table("t3", "events", ["run_id", "agent_id", "tool", "actions"], "Agent activity by tool", 0, 19),
    ]
    return {"datasets": datasets, "pages": [{"name": "incident", "displayName": "Incident view", "layout": layout}]}


def ensure():
    """Create or update the dashboard and publish it. Returns its URL."""
    databricks._load_env()
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.dashboards import Dashboard
    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list())).id
    me = w.current_user.me().user_name
    existing = next((d for d in w.lakeview.list() if d.display_name == NAME), None)
    body = Dashboard(display_name=NAME, serialized_dashboard=json.dumps(spec()), warehouse_id=wh, parent_path=f"/Users/{me}")
    d = w.lakeview.update(existing.dashboard_id, body) if existing else w.lakeview.create(body)
    w.lakeview.publish(d.dashboard_id, warehouse_id=wh, embed_credentials=True)
    return f"{databricks.workspace_url().rstrip('/')}/dashboardsv3/{d.dashboard_id}/published"
