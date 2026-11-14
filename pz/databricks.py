"""Databricks layer.

- Real input data: vendors and invoice amounts come from the TPC-H sample dataset (samples.tpch).
- System of record: every session's provenance events and business-row changes are written to Delta tables
  in workspace.patient_zero, with Change Data Feed enabled on the business table.
- Cleanup runs against Delta: field-level UPDATEs, then the before/after images are read back from table_changes().

Writes happen on a background thread so live agents are never blocked by the warehouse.
Config comes from .env: DATABRICKS_HOST, DATABRICKS_TOKEN (and optional DATABRICKS_HTTP_PATH).
"""
import json
import os
import queue
import threading
from pathlib import Path

SCHEMA = "workspace.patient_zero"
ENV = Path(__file__).resolve().parent.parent / ".env"


def _load_env():
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def available():
    _load_env()
    return bool(os.environ.get("DATABRICKS_HOST") and os.environ.get("DATABRICKS_TOKEN"))


def workspace_url():
    _load_env()
    return os.environ.get("DATABRICKS_HOST", "")


class Delta:
    def __init__(self):
        _load_env()
        from databricks import sql
        self.host = os.environ["DATABRICKS_HOST"].replace("https://", "").rstrip("/")
        self.http_path = os.environ.get("DATABRICKS_HTTP_PATH") or self._find_warehouse()
        self.conn = sql.connect(server_hostname=self.host, http_path=self.http_path, access_token=os.environ["DATABRICKS_TOKEN"])
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.errors = []
        threading.Thread(target=self._worker, daemon=True).start()

    def _find_warehouse(self):
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient(host="https://" + self.host, token=os.environ["DATABRICKS_TOKEN"])
        wh = next(iter(w.warehouses.list()))
        return wh.odbc_params.path

    def sql(self, statement, params=None):
        with self.lock:
            cur = self.conn.cursor()
            try:
                cur.execute(statement, params or {})
                try:
                    return [r.asDict() for r in cur.fetchall()]
                except Exception:
                    return []
            finally:
                cur.close()

    # ---------- setup + real data ----------
    def setup(self, run_id):
        self.run_id = run_id
        self.sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        self.sql(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.agent_events (run_id STRING, event_id STRING, session_id STRING, agent_id STRING, "
                 "ts BIGINT, type STRING, object_id STRING, tool STRING, row_key STRING, reversible BOOLEAN, payload STRING)")
        self.sql(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.knowledge (run_id STRING, object_id STRING, kind STRING, source STRING, "
                 "created_ts BIGINT, status STRING, text STRING)")
        self.sql(f"CREATE OR REPLACE TABLE {SCHEMA}.payments (item_id STRING, entity_id STRING, category STRING, amount DOUBLE, "
                 "value STRING, status STRING) TBLPROPERTIES (delta.enableChangeDataFeed = true)")
        self.sql(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.incidents (run_id STRING, root STRING, proven INT, cleared INT, rows_fixed INT, "
                 "created_at TIMESTAMP)")
        self.base_version = self.sql(f"DESCRIBE HISTORY {SCHEMA}.payments LIMIT 1")[0]["version"]

    def real_vendors(self, n=40, offset=0):
        rows = self.sql(f"SELECT s_suppkey, s_name, s_nationkey FROM samples.tpch.supplier ORDER BY s_suppkey LIMIT {n} OFFSET {offset}")
        return [{"id": f"V-{r['s_suppkey']}", "name": r["s_name"].replace("Supplier#", "Supplier "), "bank": f"BANK-10-{r['s_suppkey']}"} for r in rows]

    def real_amounts(self, n=200):
        rows = self.sql(f"SELECT o_totalprice FROM samples.tpch.orders WHERE o_totalprice BETWEEN 800 AND 9000 LIMIT {n}")
        return [float(r["o_totalprice"]) for r in rows]

    # ---------- streaming writes ----------
    def push_session(self, store, session_id):
        """Queue one finished agent session for Delta (events + business row changes)."""
        events = store.q("SELECT * FROM events WHERE session_id=?", (session_id,))
        changes = store.q("SELECT * FROM row_changes WHERE session_id=? AND table_name='items' ORDER BY version", (session_id,))
        kb = store.q("SELECT * FROM kb WHERE created_by_session=?", (session_id,))
        self.q.put(("session", events, changes, kb))

    def push_kb(self, store):
        self.q.put(("kb", store.q("SELECT * FROM kb"), None, None))

    def flush(self, timeout=120):
        done = threading.Event()
        self.q.put(("flush", done, None, None))
        done.wait(timeout)

    def _worker(self):
        while True:
            kind, a, b, c = self.q.get()
            try:
                if kind == "flush":
                    a.set()
                elif kind == "kb":
                    self._insert("knowledge", [(self.run_id, r["object_id"], r["kind"], r["source"], r["created_ts"], r["status"], r["text"]) for r in a])
                else:
                    self._insert("agent_events", [(self.run_id, e["event_id"], e["session_id"], e["agent_id"], e["ts"], e["type"], e["object_id"],
                                                   e["tool"], e["row_key"], bool(e["reversible"]), e["payload"]) for e in a])
                    for ch in b:
                        self._apply_change(ch)
                    if c:
                        self._insert("knowledge", [(self.run_id, r["object_id"], r["kind"], r["source"], r["created_ts"], r["status"], r["text"]) for r in c])
            except Exception as ex:  # keep the demo running; surface errors in /api/databricks
                self.errors.append(str(ex)[:300])

    def _insert(self, table, rows):
        if not rows:
            return
        names = [f":p{i}_{j}" for i in range(len(rows)) for j in range(len(rows[0]))]
        params = {n[1:]: v for n, v in zip(names, [x for r in rows for x in r])}
        values = ",".join("(" + ",".join(names[i * len(rows[0]):(i + 1) * len(rows[0])]) + ")" for i in range(len(rows)))
        self.sql(f"INSERT INTO {SCHEMA}.{table} VALUES {values}", params)

    def _apply_change(self, ch):
        after = json.loads(ch["after_json"]) if ch["after_json"] else None
        if ch["op"] == "insert":
            self.sql(f"INSERT INTO {SCHEMA}.payments VALUES (:i,:e,:c,:a,:v,:s)",
                     {"i": after["item_id"], "e": after["entity_id"], "c": after["category"], "a": after["amount"], "v": after["value"], "s": after["status"]})
        elif ch["op"] == "update":
            before = json.loads(ch["before_json"])
            sets = {k: v for k, v in after.items() if before.get(k) != v}
            if sets:
                self.sql(f"UPDATE {SCHEMA}.payments SET " + ",".join(f"{k}=:{k}" for k in sets) + " WHERE item_id=:item_id",
                         {**sets, "item_id": ch["row_key"]})
        elif ch["op"] == "delete":
            self.sql(f"DELETE FROM {SCHEMA}.payments WHERE item_id=:i", {"i": ch["row_key"]})

    # ---------- cleanup in Delta ----------
    def apply_cleanup(self, reverted, incident):
        """Field-level revert in Delta, then return the before/after images Delta recorded for it."""
        self.flush()
        start = self.sql(f"DESCRIBE HISTORY {SCHEMA}.payments LIMIT 1")[0]["version"] + 1
        for r in reverted:
            if r.get("deleted"):
                self.sql(f"DELETE FROM {SCHEMA}.payments WHERE item_id=:i", {"i": r["row"]})
            else:
                fixes = {f: v["now"] for f, v in r["fields"].items()}
                self.sql(f"UPDATE {SCHEMA}.payments SET " + ",".join(f"{k}=:{k}" for k in fixes) + " WHERE item_id=:item_id",
                         {**fixes, "item_id": r["row"]})
        self.sql(f"INSERT INTO {SCHEMA}.incidents VALUES (:r,:root,:p,:c,:f,current_timestamp())",
                 {"r": self.run_id, "root": incident["root"], "p": incident["proven"], "c": incident["cleared"], "f": len(reverted)})
        cdf = self.sql(f"SELECT item_id, value, status, _change_type, _commit_version FROM table_changes('{SCHEMA}.payments', {start}) "
                       "ORDER BY _commit_version, item_id")
        return {"from_version": start, "changes": cdf}

    def table_link(self, table="payments"):
        return f"https://{self.host}/explore/data/workspace/patient_zero/{table}"
