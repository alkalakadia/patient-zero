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


def _bar(name, ds, title, x, y, w=6, h=6):
    return {"widget": {"name": name, "queries": [{"name": "main_query", "query": {"datasetName": ds, "fields": [
                {"name": "incident", "expression": "`incident`"}, {"name": "outcome", "expression": "`outcome`"},
                {"name": "sum(actions)", "expression": "SUM(`actions`)"}], "disaggregated": False}}],
                       "spec": {"version": 3, "widgetType": "bar", "encodings": {
                           "x": {"fieldName": "incident", "scale": {"type": "categorical"}, "displayName": "Incident"},
                           "y": {"fieldName": "sum(actions)", "scale": {"type": "quantitative"}, "displayName": "Agent actions"},
                           "color": {"fieldName": "outcome", "scale": {"type": "categorical", "mappings": [
                               {"value": "Caused by the attack", "color": "#A53F2B"}, {"value": "Left alone", "color": "#5F6B24"}]},
                                     "displayName": "Outcome"}},
                                "frame": {"showTitle": True, "title": title}}},
            "position": {"x": x, "y": y, "width": w, "height": h}}


LATEST = f"(SELECT run_id FROM {S}.incidents WHERE created_at IS NOT NULL ORDER BY created_at DESC LIMIT 1)"


def spec():
    label = "concat(coalesce(kind, 'payments'), ' · ', root, ' · ', run_id)"
    datasets = [
        _ds("totals", "Totals", f"SELECT count(*) AS incidents, coalesce(sum(proven),0) AS caused, coalesce(sum(cleared),0) AS left_alone, "
                                f"coalesce(sum(rows_fixed),0) AS undone FROM {S}.incidents"),
        _ds("outcomes", "Caused vs left alone", f"SELECT {label} AS incident, 'Caused by the attack' AS outcome, proven AS actions FROM {S}.incidents "
                                                f"UNION ALL SELECT {label}, 'Left alone', cleared FROM {S}.incidents"),
        _ds("prs", "Latest incident: pull requests", f"SELECT title, url, array_join(unapproved_hosts, ', ') AS sends_data_to FROM {S}.pull_requests "
                                                     f"WHERE run_id = {LATEST} ORDER BY title"),
        _ds("notes", "Team notes", f"SELECT object_id AS note, source, status, run_id FROM {S}.team_memory ORDER BY run_id DESC, note"),
        _ds("note_changes", "Team notes: change history", f"SELECT object_id AS note, run_id, _change_type AS change, status, _commit_version AS version, "
                                                          f"_commit_timestamp AS changed_at FROM table_changes('{S}.team_memory', 1) "
                                                          "WHERE _change_type IN ('update_preimage','update_postimage') ORDER BY _commit_version DESC, _change_type DESC"),
        _ds("incidents", "Incidents", f"SELECT coalesce(kind, 'payments') AS kind, root AS patient_zero, proven AS caused, cleared AS left_alone, "
                                      f"rows_fixed AS undone, created_at FROM {S}.incidents ORDER BY created_at DESC"),
    ]
    layout = [
        {"widget": {"name": "title", "multilineTextboxSpec": {"lines": ["## Patient Zero: agent incidents\n",
                                                                        "What tricked the agents, what it caused, and what was undone. Everything here is read from the Delta tables Patient Zero writes."]}},
         "position": {"x": 0, "y": 0, "width": 6, "height": 2}},
        _counter("c1", "totals", "incidents", "Incidents", 0, 2),
        _counter("c2", "totals", "caused", "Actions caused by the attack", 2, 2),
        _counter("c3", "totals", "left_alone", "Actions left alone", 4, 2),
        _bar("b1", "outcomes", "Each incident: what the attack caused vs. what was left alone", 0, 5),
        _table("t1", "prs", ["title", "url", "sends_data_to"], "Latest incident: pull requests that sent data out", 0, 11, h=5),
        _table("t2", "notes", ["note", "source", "status", "run_id"], "Team notes (poisoned ones are quarantined)", 0, 16, w=3, h=6),
        _table("t3", "note_changes", ["note", "change", "status", "version", "changed_at"], "Change Data Feed: team notes", 3, 16, w=3, h=6),
        _table("t4", "incidents", ["kind", "patient_zero", "caused", "left_alone", "undone", "created_at"], "All incidents", 0, 22, h=6),
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
