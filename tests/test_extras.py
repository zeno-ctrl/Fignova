"""Open-on-another-device links and storage / daily-upload protection."""
from tests.conftest import make_client, run_worker
from tests.fixtures import cover_lo_doc

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def upload(c):
    return c.post("/api/uploads", files={"file": ("a.docx", cover_lo_doc(), DOCX)}, data={"consent": "yes"})


def test_open_on_another_device(app):
    laptop = make_client(app)
    job = upload(laptop).json()["job_id"]
    run_worker()
    link = laptop.post("/api/session/transfer").json()
    assert "/resume#" in link["url"] and "<svg" in link["qr_svg"]
    code = link["url"].split("#", 1)[1]
    phone = make_client(app)
    assert phone.get(f"/api/jobs/{job}").status_code == 404
    assert phone.post("/api/session/resume", json={"code": code}).status_code == 200
    assert phone.get(f"/api/jobs/{job}").status_code == 200
    assert laptop.get(f"/api/jobs/{job}").status_code == 200  # original device keeps access
    thief = make_client(app)
    assert thief.post("/api/session/resume", json={"code": code}).json()["error"]["code"] == "link_invalid"  # single use


def test_capacity_and_daily_limit(app):
    from autofigure import db
    c = make_client(app)
    db.ex("INSERT INTO settings_versions(data, created_at, actor) VALUES (?,?,?)",
          db.jdump({"storage_limit_mb": 50, "max_uploads_per_day": 300}), db.now(), "test")
    db._svc_cache["data"] = None
    import autofigure.api.public as pub
    orig = pub.storage_used_mb
    pub.storage_used_mb = lambda: 49.0
    try:
        r = upload(c)
        assert r.status_code == 507 and r.json()["error"]["code"] == "storage_full"
    finally:
        pub.storage_used_mb = orig
    db.ex("INSERT INTO settings_versions(data, created_at, actor) VALUES (?,?,?)",
          db.jdump({"max_uploads_per_day": 1}), db.now(), "test")
    db._svc_cache["data"] = None
    assert upload(c).json()["error"]["code"] == "daily_limit"
    db.ex("INSERT INTO settings_versions(data, created_at, actor) VALUES (?,?,?)", db.jdump({}), db.now(), "test")
    db._svc_cache["data"] = None
