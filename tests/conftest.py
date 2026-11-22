import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="afs-test-")
os.environ["AUTOFIGURE_DATA_DIR"] = _tmp
os.environ["AUTOFIGURE_OWNER_EMAIL"] = "owner@example.test"
os.environ["AUTOFIGURE_OWNER_NAME"] = "Zaw Naing Oo"
os.environ["AUTOFIGURE_AI_PROVIDER"] = "none"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)


@pytest.fixture(scope="session")
def app():
    from autofigure.api.app import create_app
    return create_app()


def make_client(app):
    from fastapi.testclient import TestClient
    c = TestClient(app, base_url="http://testserver")
    r = c.get("/api/session")
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c


@pytest.fixture()
def client(app):
    return make_client(app)


def run_worker():
    from autofigure.pipeline import worker_loop
    worker_loop(stop_after_idle=0.3, poll=0.05)
