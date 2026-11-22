"""Durable DB-backed task queue with leases, retries, backoff and dead-lettering.

A worker crash leaves a lease that simply expires; the task becomes available
again and — because every stage is idempotent and always rebuilds from the
immutable original — re-running it cannot duplicate captions or records.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from . import db

import os as _os

# short leases on serverless hosts so a timed-out request's work is retried quickly
LEASE_SECONDS = 90 if _os.environ.get("VERCEL") else 600


def enqueue(kind: str, payload: dict, dedupe_key: Optional[str] = None, delay: float = 0, max_attempts: int = 3) -> None:
    try:
        db.ex("INSERT INTO queue(kind, payload, dedupe_key, available_at, max_attempts, created_at) VALUES (?,?,?,?,?,?)",
              kind, db.jdump(payload), dedupe_key, db.now() + delay, max_attempts, db.now())
    except Exception as e:  # sqlite3.IntegrityError / psycopg UniqueViolation
        if "unique" not in (type(e).__name__ + str(e)).lower():
            raise
        # identical pending task already queued; make it ready again if it had finished
        db.ex("UPDATE queue SET state='ready', attempts=0, payload=?, available_at=?, last_error=NULL "
              "WHERE dedupe_key=? AND state IN ('done','dead')", db.jdump(payload), db.now() + delay, dedupe_key)


def lease(job_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    now = db.now()
    lock = " FOR UPDATE SKIP LOCKED" if db.is_postgres() else ""
    extra, args = "", [now, now]
    if job_id:
        extra = " AND payload LIKE ?"
        args.append(f'%"{job_id}"%')
    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM queue WHERE ((state='ready' AND available_at<=?) OR (state='leased' AND leased_until<?))"
            + extra + " ORDER BY available_at, id LIMIT 1" + lock, tuple(args)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE queue SET state='leased', leased_until=?, attempts=attempts+1 WHERE id=?",
                  (now + LEASE_SECONDS, row["id"]))
    d = dict(row)
    d["attempts"] += 1
    d["payload"] = db.jload(d["payload"], {})
    return d


def complete(task_id: int) -> None:
    db.ex("UPDATE queue SET state='done', leased_until=NULL WHERE id=?", task_id)


def fail(task: dict, error_code: str, retryable: bool) -> bool:
    """Return True if the task is dead (no more retries)."""
    dead = (not retryable) or task["attempts"] >= task["max_attempts"]
    if dead:
        db.ex("UPDATE queue SET state='dead', last_error=?, leased_until=NULL WHERE id=?", error_code, task["id"])
    else:
        backoff = 5 * (2 ** (task["attempts"] - 1))
        db.ex("UPDATE queue SET state='ready', last_error=?, available_at=?, leased_until=NULL WHERE id=?",
              error_code, db.now() + backoff, task["id"])
    return dead


def depth() -> dict[str, int]:
    return {r["state"]: r["n"] for r in db.q("SELECT state, COUNT(*) n FROM queue GROUP BY state")}


def position(job_id: str) -> Optional[int]:
    row = db.q1("SELECT id FROM queue WHERE state='ready' AND payload LIKE ?", f'%"{job_id}"%')
    if row is None:
        return None
    return db.q1("SELECT COUNT(*) n FROM queue WHERE state IN ('ready','leased') AND id < ?", row["id"])["n"]
