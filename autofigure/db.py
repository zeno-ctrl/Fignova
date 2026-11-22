"""SQLite metadata store with versioned migrations (WAL mode, foreign keys on).

Only metadata lives here; document bytes are encrypted in the object store.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .config import get_config

MIGRATIONS: list[str] = [
    # 1 — core schema
    """
    CREATE TABLE owner_accounts (
        id INTEGER PRIMARY KEY CHECK (id = 1),          -- exactly one owner principal
        principal_id TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL,
        display_name TEXT NOT NULL,
        password_hash TEXT,
        totp_secret_enc BLOB,
        recovery_codes TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'invited',          -- invited | active | locked
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE owner_invites (
        token_hash TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        expires_at REAL NOT NULL,
        used_at REAL
    );
    CREATE TABLE owner_sessions (
        id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        principal_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        reauth_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        revoked_at REAL,
        user_agent TEXT,
        ip_hash TEXT
    );
    CREATE TABLE guest_sessions (
        id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE TABLE uploads (
        id TEXT PRIMARY KEY,
        guest_session_id TEXT NOT NULL REFERENCES guest_sessions(id),
        original_name TEXT NOT NULL,
        file_type TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        object_key TEXT,
        sha256 TEXT NOT NULL,
        consent_version TEXT NOT NULL,
        created_at REAL NOT NULL,
        retention_expires_at REAL NOT NULL,
        deletion_state TEXT NOT NULL DEFAULT 'active',   -- active | user_deleted | owner_deleted | expired
        deleted_at REAL,
        page_count INTEGER,
        figure_count INTEGER,
        validation_warnings TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX uploads_session ON uploads(guest_session_id, created_at);
    CREATE INDEX uploads_created ON uploads(created_at);
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        upload_id TEXT NOT NULL REFERENCES uploads(id),
        state TEXT NOT NULL,
        stage_index INTEGER NOT NULL DEFAULT 0,
        failure_code TEXT,
        failure_detail TEXT,
        settings TEXT NOT NULL,
        analysis TEXT,
        summary TEXT,
        current_revision INTEGER NOT NULL DEFAULT 0,
        ready_revision INTEGER,
        edits_version INTEGER NOT NULL DEFAULT 0,
        rendered_edits_version INTEGER,
        ai_calls INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL
    );
    CREATE INDEX jobs_upload ON jobs(upload_id);
    CREATE INDEX jobs_state ON jobs(state, updated_at);
    CREATE TABLE job_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        state TEXT NOT NULL,
        at REAL NOT NULL,
        detail TEXT
    );
    CREATE TABLE figures (
        id TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        logical_order INTEGER NOT NULL,
        source_page_number INTEGER,
        chapter_index INTEGER,
        figure_index INTEGER,
        status TEXT NOT NULL,
        caption_source TEXT,
        caption_text TEXT,
        confidence_band TEXT,
        needs_review INTEGER NOT NULL DEFAULT 0,
        thumb_key TEXT,
        data TEXT NOT NULL,
        PRIMARY KEY (job_id, id)
    );
    CREATE TABLE caption_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        figure_id TEXT NOT NULL,
        caption_text TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (job_id, figure_id) REFERENCES figures(job_id, id)
    );
    CREATE TABLE output_revisions (
        job_id TEXT NOT NULL REFERENCES jobs(id),
        revision INTEGER NOT NULL,
        edits_version INTEGER NOT NULL,
        docx_key TEXT,
        sha256 TEXT,
        byte_size INTEGER,
        manifest TEXT NOT NULL,
        verification TEXT,
        preview_state TEXT NOT NULL DEFAULT 'pending',   -- pending | ready | failed | deleted
        preview_manifest TEXT,
        preview_prefix TEXT,
        page_count INTEGER,
        created_at REAL NOT NULL,
        PRIMARY KEY (job_id, revision)
    );
    CREATE TABLE source_previews (
        upload_id TEXT PRIMARY KEY REFERENCES uploads(id),
        state TEXT NOT NULL,
        prefix TEXT,
        manifest TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE objects (
        key TEXT PRIMARY KEY,
        upload_id TEXT,
        kind TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        created_at REAL NOT NULL,
        deleted_at REAL
    );
    CREATE INDEX objects_upload ON objects(upload_id);
    CREATE TABLE queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        dedupe_key TEXT UNIQUE,
        state TEXT NOT NULL DEFAULT 'ready',             -- ready | leased | done | dead
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        available_at REAL NOT NULL,
        leased_until REAL,
        last_error TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX queue_ready ON queue(state, available_at);
    CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        target_upload_id TEXT,
        detail TEXT,
        ip_hash TEXT,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
    );
    CREATE TABLE security_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT,
        ip_hash TEXT
    );
    CREATE TABLE settings_versions (
        version INTEGER PRIMARY KEY AUTOINCREMENT,
        data TEXT NOT NULL,
        created_at REAL NOT NULL,
        actor TEXT NOT NULL
    );
    CREATE TABLE rate_limits (
        bucket TEXT PRIMARY KEY,
        window_start REAL NOT NULL,
        count INTEGER NOT NULL
    );
    """,
]

# Migration 2 — objects stored in the database (used on hosts without a persistent disk, e.g. Vercel + Neon)
MIGRATIONS.append("""
    CREATE TABLE object_blobs (
        key TEXT PRIMARY KEY,
        data BLOB NOT NULL
    );
    CREATE TABLE upload_chunks (
        token TEXT NOT NULL,
        idx INTEGER NOT NULL,
        data BLOB NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (token, idx)
    );
""")

_local = threading.local()


def database_url() -> str | None:
    import os
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or None


def is_postgres() -> bool:
    url = database_url()
    return bool(url) and url.startswith(("postgres://", "postgresql://"))


class Row(dict):
    """Row usable by column name (row["id"]) and by position (row[0]), like sqlite3.Row."""

    def __init__(self, cols: list[str], values: tuple):
        super().__init__(zip(cols, values))
        self._values = values

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._values[k]
        return super().__getitem__(k)

    def keys(self):
        return list(super().keys())


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur

    def _cols(self):
        return [d.name for d in self._cur.description] if self._cur.description else []

    def fetchone(self):
        r = self._cur.fetchone()
        return Row(self._cols(), r) if r is not None else None

    def fetchall(self):
        cols = self._cols()
        return [Row(cols, r) for r in self._cur.fetchall()]

    @property
    def lastrowid(self):
        return None


class PgConn:
    """sqlite3-compatible facade over psycopg (autocommit; explicit BEGIN/COMMIT in tx()).

    Serverless hosts freeze the process between requests and Neon closes idle connections meanwhile, so a dead
    connection is detected and replaced transparently (only outside an open transaction, where retrying is safe).
    """

    def __init__(self, url: str):
        self.url = url
        self.in_tx = False
        self._c = None
        self._open()

    def _open(self):
        import psycopg
        if self._c is not None:
            try:
                self._c.close()
            except Exception:
                pass
        self._c = psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=15,
                                  keepalives=1, keepalives_idle=30)

    @property
    def closed(self) -> bool:
        return self._c is None or self._c.closed

    @staticmethod
    def _sql(sql: str) -> str:
        sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, params=()):
        import psycopg
        stmt = sql.strip().upper()
        args = tuple(bytes(p) if isinstance(p, memoryview) else p for p in params) if params else None
        attempts = 1 if self.in_tx and not stmt.startswith("BEGIN") else 2
        for attempt in range(attempts):
            try:
                if self.closed:
                    self._open()
                cur = self._c.cursor()
                cur.execute(self._sql(sql), args)
                break
            except (psycopg.OperationalError, psycopg.InterfaceError):
                if attempt + 1 >= attempts:
                    self.in_tx = False
                    raise
                self._open()
        if stmt.startswith("BEGIN"):
            self.in_tx = True
        elif stmt in ("COMMIT", "ROLLBACK"):
            self.in_tx = False
        return _PgCursor(cur)

    def executescript(self, script: str):
        import psycopg
        try:
            self._c.execute(script)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            self._open()
            self._c.execute(script)


def _pg_schema(sql: str) -> str:
    import re as _re
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    sql = _re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    return _re.sub(r"\bBLOB\b", "BYTEA", sql)


def connect():
    cfg = get_config()
    key = database_url() or str(cfg.db_path)
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == key and not getattr(conn, "closed", False):
        return conn
    if is_postgres():
        conn = PgConn(database_url())
    else:
        conn = sqlite3.connect(cfg.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
    _local.conn, _local.path = conn, key
    return conn


def migrate() -> int:
    conn = connect()
    pg = is_postgres()
    if pg:
        conn.execute("SELECT pg_advisory_lock(424242)")  # concurrent cold starts migrate one at a time
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        cur = row["version"] if row else 0
        if row is None:
            conn.execute("INSERT INTO schema_version VALUES (0)")
        for i, sql in enumerate(MIGRATIONS[cur:], start=cur + 1):
            if pg:
                conn.executescript("BEGIN;" + _pg_schema(sql) + f"; UPDATE schema_version SET version={i}; COMMIT;")
            else:
                conn.executescript("BEGIN;" + sql + f"; UPDATE schema_version SET version={i}; COMMIT;")
    finally:
        if pg:
            conn.execute("SELECT pg_advisory_unlock(424242)")
    return len(MIGRATIONS)


@contextmanager
def tx() -> Iterator[Any]:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass  # connection already gone; the server rolls back on its own
        raise


def q(sql: str, *args: Any) -> list:
    return connect().execute(sql, args).fetchall()


def q1(sql: str, *args: Any):
    return connect().execute(sql, args).fetchone()


def ex(sql: str, *args: Any):
    cur = connect().execute(sql, args)
    return cur.lastrowid


def new_id(prefix: str) -> str:
    # time-ordered, opaque, unguessable
    return f"{prefix}_{int(time.time() * 1000):x}{uuid.uuid4().hex[:20]}"


def now() -> float:
    return time.time()


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def jload(v: str | None, default: Any = None) -> Any:
    if v is None:
        return default
    return json.loads(v)


DEFAULT_SERVICE_SETTINGS = {
    "max_upload_mb": None,  # None → environment default
    "retention_days": None,
    "max_figures": 400,
    "max_pages": 400,
    "max_ai_suggestions_per_job": 60,
    "maintenance_mode": False,
    "maintenance_message": "",
    "caption_defaults": {},
}


_svc_cache: dict = {"at": 0.0, "data": None}


def service_settings() -> dict:
    if _svc_cache["data"] is not None and time.time() - _svc_cache["at"] < 20:
        return dict(_svc_cache["data"])
    data = _service_settings_uncached()
    _svc_cache.update(at=time.time(), data=dict(data))
    return data


def _service_settings_uncached() -> dict:
    row = q1("SELECT data FROM settings_versions ORDER BY version DESC LIMIT 1")
    data = dict(DEFAULT_SERVICE_SETTINGS)
    if row:
        data.update(jload(row["data"], {}))
    cfg = get_config()
    if not data.get("max_upload_mb"):
        data["max_upload_mb"] = cfg.max_upload_mb
    if not data.get("retention_days"):
        data["retention_days"] = cfg.retention_days
    return data
