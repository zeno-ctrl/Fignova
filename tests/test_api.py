"""End-to-end acceptance + authorization tests (directives 35, 36, 85, 91)."""
import io
import re
import zipfile

import pytest

from tests.conftest import make_client, run_worker
from tests.fixtures import cover_lo_doc, existing_captions_doc

DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def upload(client, data: bytes, name="assignment.docx", settings="{}"):
    return client.post("/api/uploads", files={"file": (name, data, DOCX_CT)}, data={"consent": "yes", "settings": settings})


def doc_text(docx: bytes) -> str:
    xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))


@pytest.fixture(scope="module")
def processed(app):
    guest = make_client(app)
    r = upload(guest, cover_lo_doc())
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    run_worker()
    return guest, job_id, r.json()["upload_id"]


def test_upload_reaches_review_with_correct_numbering(processed):
    guest, job_id, _ = processed
    job = guest.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "review_ready", job.get("failure")
    figs = guest.get(f"/api/jobs/{job_id}/figures").json()["figures"]
    # Test A/B: cover photo protected, LO1 → 1.1, 1.2 ; LO2 → 2.1, 2.2, 2.3
    assert figs[0]["status"] == "excluded_first_page" and figs[0]["label"] is None
    assert [f["label"] for f in figs[1:]] == ["1.1", "1.2", "2.1", "2.2", "2.3"]
    assert all(f["page"] and f["page"] > 1 for f in figs[1:])
    # AutoFigure AI used the author's intro sentence for the topology figure
    assert figs[2]["caption_text"] == "Network topology used in the lab"
    assert job["ready_revision"]["verification"]["verified_captions"] == 5


def test_preview_is_rendered_from_output_and_served(processed):
    guest, job_id, _ = processed
    man = guest.get(f"/api/jobs/{job_id}/preview?kind=output").json()
    assert man["state"] == "ready" and man["page_count"] >= 4
    assert len(man["highlights"]) == 5  # every caption located on a rendered page
    assert any("Figure 2.1" in t for t in man["texts"])
    img = guest.get(man["pages"][1]["src"])
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    src = guest.get(f"/api/jobs/{job_id}/preview?kind=source").json()
    assert src["state"] == "ready" and not any("Figure 1.1" in t for t in src["texts"])


def test_edit_caption_regenerates_docx_and_preview(processed):
    """Test D: edited caption appears in the regenerated Word file and its preview."""
    guest, job_id, _ = processed
    figs = guest.get(f"/api/jobs/{job_id}/figures").json()["figures"]
    cat = figs[1]
    r = guest.patch(f"/api/jobs/{job_id}/figures/{cat['figure_id']}", json={"caption_text": "A gray cat looking toward the camera"})
    assert r.status_code == 200
    assert guest.get(f"/api/jobs/{job_id}").json()["has_unrendered_changes"] is True
    assert guest.post(f"/api/jobs/{job_id}/render").status_code == 202
    run_worker()
    job = guest.get(f"/api/jobs/{job_id}").json()
    assert job["ready_revision"]["revision"] == 2 and job["has_unrendered_changes"] is False
    dl = guest.get(f"/api/jobs/{job_id}/download")
    assert dl.status_code == 200 and dl.headers["content-type"] == DOCX_CT
    assert "assignment_AutoFigure.docx" in dl.headers["content-disposition"]
    text = doc_text(dl.content)
    assert "A gray cat looking toward the camera" in text and "Figure 1." in text
    assert text.count("A gray cat looking toward the camera") == 1
    man = guest.get(f"/api/jobs/{job_id}/preview").json()
    assert any("Figure 1.1 A gray cat looking toward the camera" in t for t in man["texts"])


def test_rejects_non_word_files(client):
    """Test E: PDF / JPG / PNG / .doc / .docm / random zip are rejected before processing."""
    cases = [
        ("report.pdf", b"%PDF-1.7\n...", "wrong_type_pdf"),
        ("photo.jpg", b"\xff\xd8\xff\xe0" + b"0" * 50, "wrong_type_jpg"),
        ("image.png", b"\x89PNG\r\n\x1a\n" + b"0" * 50, "wrong_type_png"),
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 50, "legacy_doc"),
        ("macro.docm", cover_lo_doc(), "macro_enabled"),
        ("renamed.pdf.docx", b"%PDF-1.4 fake", "wrong_type_pdf"),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "not word")
    cases.append(("archive.docx", buf.getvalue(), "not_ooxml"))
    before = client.get("/api/uploads").json()["uploads"]
    for name, data, code in cases:
        r = upload(client, data, name)
        assert r.status_code in (400, 415), (name, r.text)
        assert r.json()["error"]["code"] == code, (name, r.json())
    assert client.get("/api/uploads").json()["uploads"] == before


def test_consent_and_csrf_required(app):
    c = make_client(app)
    r = c.post("/api/uploads", files={"file": ("a.docx", cover_lo_doc(), DOCX_CT)}, data={"settings": "{}"})
    assert r.json()["error"]["code"] == "consent_required"
    c.headers.pop("X-CSRF-Token")
    r = upload(c, cover_lo_doc())
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = c.post("/api/uploads", files={"file": ("a.docx", b"x", DOCX_CT)}, data={"consent": "yes"},
               headers={"Origin": "https://evil.example", "X-CSRF-Token": c.cookies.get("afs_csrf")})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"


def test_other_guest_cannot_access_job(app, processed):
    """Test H: a second uploader cannot list, preview, edit, download or delete another user's job."""
    guest, job_id, upload_id = processed
    other = make_client(app)
    fid = guest.get(f"/api/jobs/{job_id}/figures").json()["figures"][1]["figure_id"]
    probes = [
        other.get(f"/api/jobs/{job_id}"), other.get(f"/api/jobs/{job_id}/figures"),
        other.get(f"/api/jobs/{job_id}/preview"), other.get(f"/api/jobs/{job_id}/pages/output/1/1"),
        other.get(f"/api/jobs/{job_id}/download"), other.get(f"/api/jobs/{job_id}/figures/{fid}/thumb"),
        other.patch(f"/api/jobs/{job_id}/figures/{fid}", json={"caption_text": "hacked"}),
        other.delete(f"/api/uploads/{upload_id}"),
    ]
    for r in probes:
        assert r.status_code == 404, r.request.url
        assert r.json()["error"]["code"] == "not_found"
    assert other.get("/api/uploads").json()["uploads"] == []
    # nonexistent ids give the identical answer (no existence oracle)
    assert other.get("/api/jobs/job_doesnotexist").json()["error"]["code"] == "not_found"


def test_anonymous_cannot_reach_owner(app, processed):
    """Test G."""
    _, _, upload_id = processed
    anon = make_client(app)
    for path in ["/api/owner/me", "/api/owner/uploads", f"/api/owner/uploads/{upload_id}",
                 f"/api/owner/uploads/{upload_id}/download?version=original", "/api/owner/metrics", "/api/owner/audit"]:
        r = anon.get(path)
        assert r.status_code == 401, path
    # spoofed headers / fake cookies do not help
    anon.cookies.set("afs_owner", "forged")
    anon.cookies.set("role", "owner")
    r = anon.get("/api/owner/uploads", headers={"X-Role": "owner", "X-Forwarded-User": "owner@example.test"})
    assert r.status_code == 401
    page = anon.get("/owner/uploads", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/owner/login"
    assert anon.delete(f"/api/owner/uploads/{upload_id}").status_code in (401, 403)


def enroll_owner(app):
    import subprocess, sys, os, re as _re
    from autofigure.security import totp_at, new_token, token_hash
    from autofigure import db
    import secrets, time
    if not db.q1("SELECT 1 FROM owner_accounts WHERE id=1"):
        db.ex("INSERT INTO owner_accounts(id, principal_id, email, display_name, status, created_at, updated_at) VALUES (1,?,?,?,?,?,?)",
              "own_" + secrets.token_hex(8), "owner@example.test", "Zaw Naing Oo", "invited", time.time(), time.time())
    tok = new_token()
    db.ex("INSERT INTO owner_invites(token_hash, email, expires_at) VALUES (?,?,?)", token_hash(tok), "owner@example.test", time.time() + 600)
    c = make_client(app)
    start = c.post("/api/owner/enroll/start", json={"token": tok}).json()
    secret = start["secret"]
    weak = c.post("/api/owner/enroll/finish", json={"token": tok, "password": "short", "totp": totp_at(secret, time.time())})
    assert weak.json()["error"]["code"] == "weak_password"
    fin = c.post("/api/owner/enroll/finish", json={"token": tok, "password": "correct horse battery staple 42", "totp": totp_at(secret, time.time())})
    assert fin.status_code == 200 and len(fin.json()["recovery_codes"]) == 8
    # invitation is single use
    again = c.post("/api/owner/enroll/start", json={"token": tok})
    assert again.status_code == 400
    return secret


def owner_login(app, secret):
    import time
    from autofigure.security import totp_at
    c = make_client(app)
    bad = c.post("/api/owner/login", json={"email": "owner@example.test", "password": "correct horse battery staple 42", "totp": ""})
    assert bad.status_code == 401  # password alone is not enough (MFA required)
    ok = c.post("/api/owner/login", json={"email": "owner@example.test", "password": "correct horse battery staple 42",
                                          "totp": totp_at(secret, time.time())})
    assert ok.status_code == 200, ok.text
    c.headers["X-CSRF-Token"] = c.cookies.get("afs_csrf")
    return c


def test_owner_sees_all_uploads_with_mfa_and_audit(app, processed):
    """Test I: verified founder with MFA sees every retained upload across users, previews and downloads them."""
    import time
    from autofigure.security import totp_at
    guest, job_id, upload_id = processed
    second = make_client(app)
    r2 = upload(second, existing_captions_doc(False), "second.docx")
    run_worker()
    secret = enroll_owner(app)
    owner = owner_login(app, secret)
    me = owner.get("/api/owner/me").json()
    assert me["display_name"] == "Zaw Naing Oo"
    lib = owner.get("/api/owner/uploads?status=retained").json()
    ids = {i["id"] for i in lib["items"]}
    assert upload_id in ids and r2.json()["upload_id"] in ids
    det = owner.get(f"/api/owner/uploads/{upload_id}").json()
    assert det["upload"]["original_available"] and det["figures"]
    pv = owner.get(f"/api/owner/uploads/{upload_id}/preview?kind=source").json()
    assert pv["state"] == "ready"
    assert owner.get(pv["pages"][0]["src"]).status_code == 200
    # downloads require step-up once the 10 minute window has passed
    from autofigure import db
    db.ex("UPDATE owner_sessions SET reauth_at=reauth_at-3600")
    r = owner.get(f"/api/owner/uploads/{upload_id}/download?version=original")
    assert r.status_code == 403 and r.json()["error"]["code"] == "reauth_required"
    assert owner.post("/api/owner/reauth", json={"totp": totp_at(secret, time.time())}).status_code == 200
    orig = owner.get(f"/api/owner/uploads/{upload_id}/download?version=original")
    import hashlib
    assert orig.status_code == 200
    stored_sha = db.q1("SELECT sha256 FROM uploads WHERE id=?", upload_id)["sha256"]
    assert hashlib.sha256(orig.content).hexdigest() == stored_sha  # byte-for-byte original
    out = owner.get(f"/api/owner/uploads/{upload_id}/download?version=processed")
    assert out.status_code == 200 and "Figure 2.3" in doc_text(out.content)
    audit = owner.get("/api/owner/audit").json()
    actions = [e["action"] for e in audit["events"]]
    assert audit["chain_valid"] is True
    for a in ("owner_login", "original_preview_opened", "original_downloaded", "output_downloaded", "owner_step_up"):
        assert a in actions, a
    metrics = owner.get("/api/owner/metrics").json()
    assert metrics["retained_uploads"] >= 2
    # a normal guest still cannot call owner endpoints even after the owner exists
    assert guest.get("/api/owner/uploads").status_code == 401


def test_owner_session_revocation_and_logout(app):
    import time
    from autofigure.security import totp_at
    from autofigure import db
    acct = db.q1("SELECT totp_secret_enc FROM owner_accounts WHERE id=1")
    if acct is None:
        pytest.skip("owner not enrolled in this run")
    from autofigure.security import unseal
    secret = unseal(acct["totp_secret_enc"], b"owner-totp").decode()
    a = owner_login(app, secret)
    b = owner_login(app, secret)
    assert a.post("/api/owner/sessions/revoke-others").status_code == 200
    assert b.get("/api/owner/me").status_code == 401
    assert a.post("/api/owner/logout").status_code == 200
    assert a.get("/api/owner/me").status_code == 401


def test_user_delete_removes_access(app):
    """Test J."""
    c = make_client(app)
    r = upload(c, cover_lo_doc(), "todelete.docx")
    run_worker()
    job_id, upload_id = r.json()["job_id"], r.json()["upload_id"]
    assert c.get(f"/api/jobs/{job_id}/download").status_code == 200
    d = c.delete(f"/api/uploads/{upload_id}")
    assert d.status_code == 200 and d.json()["objects_removed"] > 0
    assert c.get(f"/api/jobs/{job_id}").status_code == 404
    assert c.get(f"/api/jobs/{job_id}/download").status_code == 404
    assert all(u["id"] != upload_id for u in c.get("/api/uploads").json()["uploads"])
    from autofigure import db
    assert db.q1("SELECT COUNT(*) n FROM objects WHERE upload_id=? AND deleted_at IS NULL", upload_id)["n"] == 0


def test_retention_expiry(app):
    from autofigure import db
    from autofigure.pipeline import retention_sweep
    c = make_client(app)
    r = upload(c, cover_lo_doc(), "old.docx")
    run_worker()
    db.ex("UPDATE uploads SET retention_expires_at=? WHERE id=?", 1.0, r.json()["upload_id"])
    assert retention_sweep() >= 1
    assert c.get(f"/api/jobs/{r.json()['job_id']}").status_code == 404
    row = db.q1("SELECT deletion_state FROM uploads WHERE id=?", r.json()["upload_id"])
    assert row["deletion_state"] == "expired"


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    # company ownership shown publicly; the owner's personal name is not
    assert "© " in r.text and "Zeno Group. All rights reserved." in r.text
    assert "Zeno Group" in client.get("/about").text
    for page in ("/", "/about", "/privacy", "/app", "/features"):
        assert "Zaw Naing Oo" not in client.get(page).text, page
