"""Background worker process entry point.

Lives in its own importable module so it can be started with the "spawn" method (a fresh interpreter):
forking from a thread while the web server runs can copy a held lock into the child and freeze it.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time


def _exit_with_parent(parent: int) -> None:
    """A worker started by `serve --with-worker` must never outlive the server process."""
    def watch():
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


def run_worker(parent_pid: int | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if parent_pid:
        _exit_with_parent(parent_pid)
    from . import db
    from .config import get_config
    from .pipeline import worker_loop

    log = logging.getLogger("autofigure.worker")
    db.migrate()
    n = max(1, get_config().worker_concurrency)

    def safe_loop():
        while True:
            try:
                worker_loop()
            except Exception:  # keep the worker alive whatever happens in one iteration
                log.exception("worker_loop_crashed")
                time.sleep(1)

    for _ in range(n - 1):
        threading.Thread(target=safe_loop, daemon=True).start()
    log.info("worker_started concurrency=%d", n)
    safe_loop()


def start(stop: threading.Event) -> None:
    """Supervise a spawned worker process: restart it if it dies, never during shutdown."""
    log = logging.getLogger("autofigure.supervisor")
    ctx = multiprocessing.get_context("spawn")

    def loop():
        restarts = 0
        while not stop.is_set():
            p = ctx.Process(target=run_worker, args=(os.getpid(),), daemon=True, name="autofigure-worker")
            p.start()
            while p.is_alive() and not stop.is_set():
                p.join(timeout=1.0)
            if stop.is_set():
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
                return
            restarts += 1
            log.warning("worker_exited code=%s restarts=%d — restarting", p.exitcode, restarts)
            stop.wait(min(30, 2 * restarts))

    threading.Thread(target=loop, daemon=True, name="worker-supervisor").start()
