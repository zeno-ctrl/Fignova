"""Serverless (Vercel + Neon) mode, end to end.

Run separately because configuration is process-wide:
  AUTOFIGURE_LITE=1 AUTOFIGURE_STORAGE=database AUTOFIGURE_DISABLE_OCR=1 DATABASE_URL=postgresql://... pytest tests/test_lite.py
"""
import io
import os
import zipfile

import pytest

from tests.conftest import make_client
from tests import fixtures as fx

pytestmark = pytest.mark.skipif(not os.environ.get("AUTOFIGURE_LITE"), reason="serverless-mode test (set AUTOFIGURE_LITE=1)")
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def chunked_upload(c, data: bytes, name: str, piece: int = 40_000) -> dict:
    token = "t" + os.urandom(12).hex()
    parts = [data[i:i + piece] for i in range(0, len(data), piece)]
    for i, p in enumerate(parts):
        r = c.post("/api/uploads/chunk", content=p, headers={"X-Upload-Token": token, "X-Chunk-Index": str(i),
                                                             "Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text
    r = c.post("/api/uploads", data={"chunk_token": token, "chunk_count": str(len(parts)), "filename": name,
                                     "consent": "yes", "settings": "{}"})
    assert r.status_code == 202, r.text
    return r.json()


def fetch_chunks(c, url: str) -> bytes:
    sep = "&" if "?" in url else "?"
    first = c.get(url + sep + "chunk=0")
    assert first.status_code == 200, first.text
    total = int(first.headers["x-total-chunks"])
    return first.content + b"".join(c.get(url + sep + f"chunk={i}").content for i in range(1, total))


def captions(docx: bytes) -> list[str]:
    from docx import Document
    return [p.text for p in Document(io.BytesIO(docx)).paragraphs if p.style.name == "Caption"]


def test_serverless_flow(app):
    from autofigure import db
    from autofigure.config import get_config
    cfg = get_config()
    assert cfg.lite and cfg.storage_backend == "database" and db.is_postgres()
    c = make_client(app)
    assert c.get("/api/session").json()["lite"] is True
    up = chunked_upload(c, fx.cover_lo_doc(), "coursework.docx")
    job = up["job_id"]
    assert c.get(f"/api/jobs/{job}").json()["state"] == "uploaded"  # nothing runs without the browser asking
    assert c.post(f"/api/jobs/{job}/process").json()["state"] == "review_ready"
    figs = c.get(f"/api/jobs/{job}/figures").json()["figures"]
    assert figs[0]["status"] == "excluded_first_page"
    assert [f["label"] for f in figs[1:]] == ["1.1", "1.2", "2.1", "2.2", "2.3"]
    assert figs[2]["caption_text"] == "Network topology used in the lab"
    man = c.get(f"/api/jobs/{job}/preview").json()
    assert man["state"] == "client" and len(man["labels"]) == 5
    out = fetch_chunks(c, man["file"])
    assert captions(out)[2] == "Figure 2.1 Chart or diagram" or captions(out)[2].startswith("Figure 2.1")
    assert fetch_chunks(c, f"/api/jobs/{job}/download") == out
    src = c.get(f"/api/jobs/{job}/preview?kind=source").json()
    assert src["state"] == "client" and zipfile.ZipFile(io.BytesIO(fetch_chunks(c, src["file"]))).testzip() is None
    # edit + regenerate through the browser-driven processing
    fid = figs[1]["figure_id"]
    c.patch(f"/api/jobs/{job}/figures/{fid}", json={"caption_text": "A cat staring"})
    c.post(f"/api/jobs/{job}/render")
    c.post(f"/api/jobs/{job}/process")
    assert captions(fetch_chunks(c, f"/api/jobs/{job}/download"))[0] == "Figure 1.1 A cat staring"
    # objects live in Postgres, encrypted
    row = db.q1("SELECT COUNT(*) n FROM object_blobs")
    assert row["n"] > 0
    raw = bytes(db.q1("SELECT data FROM object_blobs LIMIT 1")["data"])
    assert raw.startswith(b"AFS1") and b"word/document.xml" not in raw
    # another browser session still cannot touch it
    other = make_client(app)
    assert other.post(f"/api/jobs/{job}/process").status_code == 404
    assert other.get(man["file"] + "&chunk=0").status_code == 404
    # delete removes the stored bytes from the database
    c.delete(f"/api/uploads/{up['upload_id']}")
    assert db.q1("SELECT COUNT(*) n FROM objects WHERE upload_id=? AND deleted_at IS NULL", up["upload_id"])["n"] == 0


def test_serverless_mistakes_and_cover_detection(app):
    c = make_client(app)
    job = chunked_upload(c, fx.wrong_caption_doc(), "w.docx")["job_id"]
    c.post(f"/api/jobs/{job}/process")
    a, b = c.get(f"/api/jobs/{job}/figures").json()["figures"]
    assert b["mistakes"] and b["mistakes"][0]["kind"] == "number"
    job2 = chunked_upload(c, fx.auto_pagination_doc(), "a.docx")["job_id"]
    c.post(f"/api/jobs/{job2}/process")
    f = c.get(f"/api/jobs/{job2}/figures").json()["figures"]
    assert f[0]["status"] == "excluded_first_page" and f[1]["label"] == "1.1"


def test_owner_setup_page_without_command_line(app, monkeypatch):
    import time
    from autofigure import db
    from autofigure.security import totp_at
    db.ex("DELETE FROM owner_accounts")
    c = make_client(app)
    monkeypatch.setenv("AUTOFIGURE_OWNER_SETUP_KEY", "a-long-random-setup-key-123")
    assert c.post("/api/owner/bootstrap", json={"setup_key": "wrong"}).status_code == 403
    url = c.post("/api/owner/bootstrap", json={"setup_key": "a-long-random-setup-key-123"}).json()["enroll_url"]
    tok = url.split("token=")[1]
    secret = c.post("/api/owner/enroll/start", json={"token": tok}).json()["secret"]
    r = c.post("/api/owner/enroll/finish", json={"token": tok, "password": "correct horse battery staple 42",
                                                 "totp": totp_at(secret, time.time())})
    assert r.status_code == 200
    assert c.post("/api/owner/bootstrap", json={"setup_key": "a-long-random-setup-key-123"}).status_code == 409


def test_survives_database_dropping_idle_connections(app):
    """Neon closes idle connections while Vercel freezes the function; requests must reconnect, not 500."""
    import psycopg
    from autofigure import db
    c = make_client(app)
    assert c.get("/static/css/app.css").status_code == 200
    with psycopg.connect(db.database_url(), autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
    for path in ("/static/css/app.css", "/how-it-works", "/api/session", "/owner/login"):
        assert c.get(path).status_code == 200, path


def test_browser_ocr_improves_captions(app):
    """Vercel has no OCR engine: text read by the visitor's browser upgrades generic captions and regenerates."""
    c = make_client(app)
    job = chunked_upload(c, fx.cover_lo_doc(), "x.docx")["job_id"]
    c.post(f"/api/jobs/{job}/process")
    assert c.get(f"/api/jobs/{job}").json()["needs_browser_ocr"] is True
    figs = c.get(f"/api/jobs/{job}/figures").json()["figures"]
    shot = figs[3]  # the interface-status screenshot (LO2, first figure)
    assert c.get(f"/api/jobs/{job}/figures/{shot['figure_id']}/image").content[:2] == b"\xff\xd8"
    lines = [{"text": f"Interface Gi0/{i} status up vlan {10 + i}", "x": .3, "y": .1 + i * .12, "h": .04, "conf": 88}
             for i in range(6)]
    r = c.post(f"/api/jobs/{job}/ocr", json={"results": [{"figure_id": shot["figure_id"], "lines": lines}]}).json()
    assert r["updated"] == 1 and r["regenerating"] is True
    c.post(f"/api/jobs/{job}/process")
    assert c.get(f"/api/jobs/{job}").json()["needs_browser_ocr"] is False  # only once per document
    assert "Figure 2.1 Screenshot of interface status with VLAN assignments" in captions(fetch_chunks(c, f"/api/jobs/{job}/download"))
    # a second submission is ignored
    assert c.post(f"/api/jobs/{job}/ocr", json={"results": []}).json()["updated"] == 0
