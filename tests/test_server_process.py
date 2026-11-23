"""The real `python -m autofigure serve --with-worker` process: jobs complete, a dead worker is restarted,
and stopping the server leaves no worker behind (regression test for a fork-under-threads deadlock)."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

from tests.fixtures import cover_lo_doc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def _children(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True).stdout.splitlines()[1:]
    return [int(l.split()[0]) for l in out if int(l.split()[1]) == pid and "spawn_main" in l]


@pytest.mark.skipif(sys.platform != "linux", reason="process inspection uses ps")
def test_serve_with_worker_processes_jobs_and_supervises(tmp_path):
    port = _free_port()
    env = dict(os.environ, AUTOFIGURE_DATA_DIR=str(tmp_path), AUTOFIGURE_AI_PROVIDER="none")
    env.pop("DATABASE_URL", None)
    env.pop("AUTOFIGURE_LITE", None)
    proc = subprocess.Popen([sys.executable, "-m", "autofigure", "serve", "--port", str(port), "--with-worker"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=2); break
            except Exception:
                time.sleep(0.5)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        csrf = json.load(opener.open(base + "/api/session"))["csrf"]
        boundary = "afsboundary"
        doc = cover_lo_doc()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"consent\"\r\n\r\nyes\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.docx\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + doc + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(base + "/api/uploads", data=body, method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}", "X-CSRF-Token": csrf})
        job = json.load(opener.open(req))["job_id"]
        state = None
        for _ in range(120):
            state = json.load(opener.open(f"{base}/api/jobs/{job}"))["state"]
            if state in ("review_ready", "failed"):
                break
            time.sleep(1)
        assert state == "review_ready"
        workers = _children(proc.pid)
        assert len(workers) == 1
        os.kill(workers[0], signal.SIGKILL)
        for _ in range(30):
            time.sleep(1)
            again = _children(proc.pid)
            if again and again != workers:
                break
        assert again and again != workers, "worker was not restarted"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    time.sleep(3)
    leftover = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    assert f"--port {port}" not in leftover
    for w in again:
        assert not os.path.exists(f"/proc/{w}") or open(f"/proc/{w}/status").read().find("Z (zombie)") >= 0
