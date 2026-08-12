"""DB connection + schema management for the Garmin tracker.

Two backends, chosen by config.TURSO_DATABASE_URL:
- Local SQLite (default) - used for all local/manual runs.
- Turso (hosted libSQL), via its plain HTTP API - used when running in the
  cloud (GitHub Actions writer, Streamlit Cloud reader), so both share one
  database without either needing a local file.

The Turso client is a small hand-rolled wrapper (see TursoConnection below)
rather than the official `libsql-experimental` package: that package is
explicitly labeled experimental/not-production-grade and only ships
prebuilt wheels for Linux/macOS, so it can't be verified from this Windows
dev environment. Turso's HTTP API is documented and stable, and small enough
to implement directly against the small subset of DB-API this project uses.
"""
import base64
import contextlib
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

import requests

from garmin_tracker import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    distance_m REAL,
    total_calories INTEGER,
    active_calories INTEGER,
    resting_hr INTEGER,
    hrv_status TEXT,
    body_battery_wake INTEGER,
    body_battery_min INTEGER,
    sleep_minutes INTEGER,
    sleep_score INTEGER,
    weight_lb REAL,
    stress_avg INTEGER,
    respiration_avg REAL,
    spo2_avg REAL
);

CREATE TABLE IF NOT EXISTS activities (
    garmin_id INTEGER PRIMARY KEY,
    date TEXT,
    start_time TEXT,
    activity_type TEXT,
    bucket TEXT,
    name TEXT,
    duration_min REAL,
    distance_m REAL,
    calories REAL,
    avg_hr REAL,
    max_hr REAL,
    training_load REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS training_status (
    date TEXT PRIMARY KEY,
    vo2max_running REAL,
    vo2max_cycling REAL,
    training_status TEXT,
    training_load REAL
);

CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    metric TEXT,
    value REAL
);

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    kind TEXT,
    body_markdown TEXT,
    metrics_snapshot_json TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at TEXT,
    kind TEXT,
    predictor TEXT,
    outcome TEXT,
    lag_days INTEGER,
    effect_size REAL,
    ci_low REAL,
    ci_high REAL,
    q_value REAL,
    n_effective INTEGER,
    status TEXT,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    source TEXT,
    status TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT PRIMARY KEY,
    last_synced_date TEXT
);

CREATE TABLE IF NOT EXISTS intraday_steps (
    date TEXT,
    hour INTEGER,
    steps INTEGER,
    PRIMARY KEY (date, hour)
);

-- Populated from a manually-exported MyFitnessPal CSV (see
-- scripts/import_myfitnesspal.py) - MyFitnessPal has no public API, so this
-- has no live sync counterpart and only updates when the user re-exports.
CREATE TABLE IF NOT EXISTS nutrition_daily (
    date TEXT PRIMARY KEY,
    calories REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    saturated_fat_g REAL,
    sodium_mg REAL,
    sugar_g REAL,
    fiber_g REAL,
    potassium_mg REAL
);

CREATE TABLE IF NOT EXISTS nutrition_meals (
    date TEXT,
    meal TEXT,
    calories REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    PRIMARY KEY (date, meal)
);
"""


# ---- Turso (hosted libSQL) HTTP client --------------------------------

class TursoRow:
    """Mimics sqlite3.Row: supports row["col"], row[0], dict(row), iteration."""

    __slots__ = ("_cols", "_values")

    def __init__(self, cols, values):
        self._cols = cols
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._cols.index(key)]

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._values)

    def __repr__(self):
        return repr(dict(zip(self._cols, self._values)))


def _to_turso_arg(value):
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        # Turso's real server wants a JSON number here despite what its docs
        # say ("encoded as a string") - confirmed directly against the API:
        # a string value returns 400 "invalid type: string, expected f64".
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    return {"type": "text", "value": str(value)}


def _from_turso_cell(cell: dict):
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        return base64.b64decode(cell["base64"])
    return cell.get("value")


class TursoCursor:
    def __init__(self, cols, rows, last_insert_rowid=None):
        self._rows = [TursoRow(cols, r) for r in rows]
        self._idx = 0
        self.description = [(c, None, None, None, None, None, None) for c in cols]
        self.lastrowid = last_insert_rowid

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self):
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rows


class TursoConnection:
    """Minimal DB-API-ish connection backed by Turso's HTTP pipeline endpoint.
    Implements only what this project's db.py/coach.py/dashboard.py use:
    execute, executescript, commit, close. Every execute() is its own HTTP
    request (auto-committed on Turso's side) - fine for this project's
    low-volume, no-multi-statement-transaction access pattern.
    """

    def __init__(self, database_url: str, auth_token: str):
        http_url = database_url.replace("libsql://", "https://", 1)
        if not http_url.startswith("https://"):
            http_url = "https://" + http_url
        self._endpoint = http_url.rstrip("/") + "/v2/pipeline"
        self._headers = {"Authorization": f"Bearer {auth_token}"}

    def _pipeline(self, stmt_requests):
        body = {"requests": stmt_requests + [{"type": "close"}]}
        resp = requests.post(self._endpoint, json=body, headers=self._headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data["results"]:
            if r["type"] == "error":
                raise RuntimeError(f"Turso query error: {r.get('error')}")
            if r["response"]["type"] == "execute":
                results.append(r["response"]["result"])
        return results

    def execute(self, sql: str, params: Iterable = ()) -> TursoCursor:
        args = [_to_turso_arg(p) for p in params]
        stmt = {"sql": sql}
        if args:
            stmt["args"] = args
        result = self._pipeline([{"type": "execute", "stmt": stmt}])[0]
        cols = [c["name"] for c in result.get("cols", [])]
        rows = [[_from_turso_cell(cell) for cell in row] for row in result.get("rows", [])]
        return TursoCursor(cols, rows, result.get("last_insert_rowid"))

    def executescript(self, script: str) -> None:
        statements = [s.strip() for s in script.split(";") if s.strip()]
        reqs = [{"type": "execute", "stmt": {"sql": s}} for s in statements]
        self._pipeline(reqs)

    def commit(self) -> None:
        pass  # each execute() is already committed server-side

    def close(self) -> None:
        pass


# ---- Backend-agnostic helpers -----------------------------------------

def get_connection():
    if config.TURSO_DATABASE_URL:
        return TursoConnection(config.TURSO_DATABASE_URL, config.TURSO_AUTH_TOKEN)

    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL mode allows concurrent readers (dashboard) while syncs write, and is
    # required for Litestream's continuous backup replication to work at all.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert(conn, table: str, row: Mapping) -> None:
    """INSERT OR REPLACE a single row (dict of column -> value)."""
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)
    sql = f"INSERT OR REPLACE INTO {table} ({column_list}) VALUES ({placeholders})"
    conn.execute(sql, [row[c] for c in columns])


def upsert_many(conn, table: str, rows: Iterable[Mapping]) -> int:
    count = 0
    for row in rows:
        upsert(conn, table, row)
        count += 1
    return count


def log_sync(conn, source: str, status: str, error: str | None = None) -> None:
    """Record one sync attempt (status: 'ok' or 'error') so later reads can
    tell whether the data on hand is fresh before coaching on it."""
    from datetime import datetime, timezone

    conn.execute(
        "INSERT INTO sync_log (timestamp, source, status, error) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), source, status, error),
    )
    conn.commit()


def get_last_synced(conn, source: str) -> str | None:
    cur = conn.execute(
        "SELECT last_synced_date FROM sync_state WHERE source = ?", (source,)
    )
    row = cur.fetchone()
    return row["last_synced_date"] if row else None


def set_last_synced(conn, source: str, date_str: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (source, last_synced_date)
        VALUES (?, ?)
        ON CONFLICT(source) DO UPDATE SET last_synced_date = excluded.last_synced_date
        """,
        (source, date_str),
    )
    conn.commit()


def fetch_all_dicts(conn, sql: str, params: Iterable = ()) -> list[dict]:
    """Run a query and return rows as plain dicts - works identically for
    both the local sqlite3 backend and the Turso HTTP backend, so callers
    (e.g. the dashboard) don't need pandas' connection-type detection."""
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


@contextlib.contextmanager
def connect():
    conn = get_connection()
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
