"""Storage layer.

Local mode uses SQLite and emulates the two Delta features Patient Zero depends on:
  - row_changes  ~ Delta Change Data Feed (before/after image per row change, monotonically versioned)
  - versioned KB ~ Delta time travel on the knowledge base

The schema matches the Delta tables in CLAUDE.md so databricks_sync.py can mirror it 1:1.
"""
import json
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pz.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb (
  object_id TEXT PRIMARY KEY,
  kind TEXT,            -- doc | memory
  topic TEXT,
  text TEXT,
  source TEXT,          -- internal | ticket | agent
  visible_to TEXT,      -- comma list of agent ids, or *
  created_ts INTEGER,
  created_by_session TEXT,
  status TEXT DEFAULT 'active',  -- active | quarantined
  retired_ts INTEGER,            -- set when a newer version replaced this one (time travel for retrieval)
  supersedes TEXT                -- object_id of the version this one replaced (a page edit)
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  agent_id TEXT,
  ts INTEGER,
  task TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  session_id TEXT,
  agent_id TEXT,
  ts INTEGER,
  type TEXT,            -- retrieve | tool_call | memory_write | kb_ingest
  object_id TEXT,       -- kb object read/written, or row key for tool calls
  tool TEXT,
  table_name TEXT,
  row_key TEXT,
  delta_version INTEGER,
  reversible INTEGER,
  payload TEXT
);
CREATE TABLE IF NOT EXISTS row_changes (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT,
  row_key TEXT,
  op TEXT,              -- insert | update | delete
  before_json TEXT,
  after_json TEXT,
  session_id TEXT,
  event_id TEXT,
  ts INTEGER
);
-- business tables; scenario skins give them display names (refunds, vendor payments, pull requests, quotes)
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY, entity_id TEXT, category TEXT, amount REAL, value TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY, name TEXT, email TEXT, tier TEXT, value TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  msg_id TEXT PRIMARY KEY, to_addr TEXT, subject TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- incident desk: one row per incident (alerts grouped by the flagged value), see pz/incidents.py
CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY,
  value TEXT,           -- the flagged value the detector saw (attacker's account, host...)
  alert_ids TEXT,       -- json list of flagged event ids
  root TEXT,            -- patient zero, once trace_back confirms it
  status TEXT,          -- contained | investigating | awaiting_approval | fixing | fixed | dismissed | undone | failed | recorded
  mode TEXT,            -- autopilot | manual | recorded
  opened_at REAL, updated_at REAL,
  reason TEXT,          -- why it waits for a person (autopilot policy), or what failed
  suspects TEXT,        -- json list of kb objects withdrawn while the incident is open
  steps TEXT,           -- json list of investigation events (same shape as the /api/investigate stream)
  report TEXT,          -- json incident report
  result TEXT           -- json cleanup result (kept for undo)
);
-- containment while an incident is open: writes carrying a value, or by sessions that read an object, are held
CREATE TABLE IF NOT EXISTS holds (incident_id TEXT, kind TEXT, value TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS held_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, session_id TEXT, agent_id TEXT, ts INTEGER,
  tool TEXT, row_key TEXT, payload TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS ground_truth (
  event_id TEXT PRIMARY KEY, poisoned INTEGER
);
"""

BUSINESS_TABLES = {"items": "item_id", "entities": "entity_id", "messages": "msg_id"}


class _Serialized:
    """One SQLite connection shared by request threads, the incident desk and live runs, used one call at a time.
    Concurrent use of a single sqlite3 connection can hand back scrambled rows, and closing it under a running
    query crashes Python."""

    def __init__(self, con):
        self._con, self.lock = con, threading.RLock()

    def execute(self, *a):
        with self.lock:
            return self._con.execute(*a)

    def executescript(self, sql):
        with self.lock:
            return self._con.executescript(sql)

    def commit(self):
        with self.lock:
            self._con.commit()

    def close(self):
        with self.lock:
            self._con.close()

    def __getattr__(self, name):
        return getattr(self._con, name)


class Store:
    def __init__(self, path=DB_PATH, fresh=False):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if fresh and path.exists():
            path.unlink()
        con = sqlite3.connect(path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        self.db = _Serialized(con)
        self.db.executescript(SCHEMA)

    def close(self):
        # Windows can't delete a SQLite file that still has an open handle, so close before a fresh rebuild.
        self.db.close()

    # ---------- generic ----------
    def q(self, sql, args=()):
        with self.db.lock:  # run and read the query as one step
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.db.lock:
            r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def commit(self):
        self.db.commit()

    # ---------- business rows with change capture ----------
    def get_row(self, table, key):
        pk = BUSINESS_TABLES[table]
        return self.one(f"SELECT * FROM {table} WHERE {pk}=?", (key,))

    def write_row(self, table, key, values, session_id, event_id, ts):
        """Insert or update a row and record a CDF-style change. Returns the change version."""
        with self.db.lock:  # read-modify-write plus its change record, as one step
            return self._write_row(table, key, values, session_id, event_id, ts)

    def _write_row(self, table, key, values, session_id, event_id, ts):
        pk = BUSINESS_TABLES[table]
        before = self.get_row(table, key)
        if before is None:
            row = {pk: key, **values}
            cols = ",".join(row)
            self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
            op = "insert"
        else:
            sets = ",".join(f"{c}=?" for c in values)
            self.db.execute(f"UPDATE {table} SET {sets} WHERE {pk}=?", [*values.values(), key])
            op = "update"
        after = self.get_row(table, key)
        return self._log_change(table, key, op, before, after, session_id, event_id, ts)

    def delete_row(self, table, key, session_id, event_id, ts):
        pk = BUSINESS_TABLES[table]
        before = self.get_row(table, key)
        self.db.execute(f"DELETE FROM {table} WHERE {pk}=?", (key,))
        return self._log_change(table, key, "delete", before, None, session_id, event_id, ts)

    def _log_change(self, table, key, op, before, after, session_id, event_id, ts):
        cur = self.db.execute(
            "INSERT INTO row_changes (table_name,row_key,op,before_json,after_json,session_id,event_id,ts) VALUES (?,?,?,?,?,?,?,?)",
            (table, key, op, json.dumps(before) if before else None, json.dumps(after) if after else None, session_id, event_id, ts),
        )
        return cur.lastrowid

    # ---------- time travel (Delta-style reads as of a point in time) ----------
    time_travel = None  # optional hook (Databricks): read items as of a timestamp with Delta time travel

    def table_as_of(self, table, ts):
        """Reconstruct every row of a business table as it was just before `ts`, from the change feed."""
        if table == "items" and self.time_travel:
            return self.time_travel(ts)
        rows = {}
        for c in self.q("SELECT row_key, after_json FROM row_changes WHERE table_name=? AND ts<? ORDER BY version", (table, ts)):
            if c["after_json"] is None:
                rows.pop(c["row_key"], None)
            else:
                rows[c["row_key"]] = json.loads(c["after_json"])
        return rows
