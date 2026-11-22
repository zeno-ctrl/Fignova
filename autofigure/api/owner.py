"""Owner-only API (directives 19–23, 33, 34, 69–74).

Every handler calls ``require_owner`` which validates a server-side session
bound to the single owner principal. Sensitive content actions additionally
require a recent second-factor confirmation (step-up) and are audited.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import secrets
from typing import Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import Response as RawResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import db, queue
from ..config import get_config
from ..engine.validation import output_filename
from ..pipeline import delete_upload_content, retention_sweep
from ..security import (audit, hash_password, new_token, new_totp_secret, otpauth_uri, password_problems, rate_limit,
                        recovery_codes, seal, security_event, token_hash, unseal, verify_audit_chain, verify_password,
                        verify_totp, ip_hash)
from ..storage import store
from .common import (OWNER_ABSOLUTE, OWNER_COOKIE, ApiError, check_csrf, client_ip, limit, not_found, require_owner,
                     require_step_up, set_cookie, owner_session)
from .public import docx_response, figure_payload, job_payload, page_object, preview_manifest_for

router = APIRouter(prefix="/api/owner")
TOTP_AAD = b"owner-totp"


def _qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


# --- enrollment (one-time invitation created by the operator CLI) -----------
class EnrollStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)


def _valid_invite(token: str) -> Optional[dict]:
    row = db.q1("SELECT * FROM owner_invites WHERE token_hash=?", token_hash(token))
    if not row or row["used_at"] is not None or row["expires_at"] < db.now():
        return None
    return dict(row)


@router.post("/enroll/start")
def enroll_start(body: EnrollStart, request: Request) -> dict:
    check_csrf(request)
    limit("owner-enroll", 10, 900, request)
    inv = _valid_invite(body.token)
    acct = db.q1("SELECT status FROM owner_accounts WHERE id=1")
    if inv is None or acct is None or acct["status"] != "invited":
        security_event("owner_enroll_rejected", None, client_ip(request))
        raise ApiError(400, "invalid_invite", "This invitation is invalid or has expired.")
    secret = new_totp_secret()
    db.ex("UPDATE owner_accounts SET totp_secret_enc=?, updated_at=? WHERE id=1", seal(secret.encode(), TOTP_AAD), db.now())
    uri = otpauth_uri(secret, inv["email"])
    return {"email": inv["email"], "secret": secret, "otpauth_uri": uri, "qr_svg": _qr_svg(uri)}


class EnrollFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=256)
    totp: str = Field(min_length=6, max_length=10)


@router.post("/enroll/finish")
def enroll_finish(body: EnrollFinish, request: Request) -> dict:
    check_csrf(request)
    limit("owner-enroll", 10, 900, request)
    inv = _valid_invite(body.token)
    acct = db.q1("SELECT * FROM owner_accounts WHERE id=1")
    if inv is None or acct is None or acct["status"] != "invited" or not acct["totp_secret_enc"]:
        raise ApiError(400, "invalid_invite", "This invitation is invalid or has expired.")
    probs = password_problems(body.password, inv["email"])
    if probs:
        raise ApiError(400, "weak_password", " ".join(probs))
    secret = unseal(acct["totp_secret_enc"], TOTP_AAD).decode()
    if not verify_totp(secret, body.totp):
        raise ApiError(400, "bad_totp", "That authenticator code is not valid. Check your device clock and try again.")
    codes, hashes = recovery_codes()
    with db.tx() as c:
        c.execute("UPDATE owner_accounts SET password_hash=?, recovery_codes=?, status='active', failed_attempts=0, "
                  "updated_at=? WHERE id=1", (hash_password(body.password), db.jdump(hashes), db.now()))
        c.execute("UPDATE owner_invites SET used_at=? WHERE token_hash=?", (db.now(), token_hash(body.token)))
    audit("owner", "owner_enrolled", None, {"mfa": "totp"}, client_ip(request))
    return {"ok": True, "recovery_codes": codes}


# --- login / logout / step-up -------------------------------------------------
class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    totp: str = Field(default="", max_length=20)
    recovery_code: str = Field(default="", max_length=40)


GENERIC_LOGIN_ERROR = "Sign-in failed. Check your details and authenticator code."


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> dict:
    check_csrf(request)
    ip = client_ip(request)
    if not rate_limit(f"owner-login:{ip_hash(ip)}", 8, 900) or not rate_limit("owner-login-global", 60, 900):
        security_event("owner_login_rate_limited", None, ip)
        raise ApiError(429, "rate_limited", "Too many sign-in attempts. Wait 15 minutes.")
    acct = db.q1("SELECT * FROM owner_accounts WHERE id=1")
    ok = False
    used_recovery = False
    if acct and acct["status"] == "active" and (acct["locked_until"] or 0) < db.now():
        email_ok = hmac.compare_digest(acct["email"].lower(), body.email.strip().lower())
        pw_ok = verify_password(body.password, acct["password_hash"])
        mfa_ok = False
        if body.totp:
            mfa_ok = verify_totp(unseal(acct["totp_secret_enc"], TOTP_AAD).decode(), body.totp)
        elif body.recovery_code:
            hashes = db.jload(acct["recovery_codes"], [])
            h = token_hash(body.recovery_code.strip().lower())
            if h in hashes:
                mfa_ok, used_recovery = True, True
                hashes.remove(h)
                db.ex("UPDATE owner_accounts SET recovery_codes=? WHERE id=1", db.jdump(hashes))
        ok = email_ok and pw_ok and mfa_ok
    else:
        verify_password(body.password, None)  # equalise timing
    if not ok:
        if acct and acct["status"] == "active":
            fails = acct["failed_attempts"] + 1
            locked = db.now() + 900 if fails >= 10 else None
            db.ex("UPDATE owner_accounts SET failed_attempts=?, locked_until=COALESCE(?, locked_until) WHERE id=1",
                  0 if locked else fails, locked)
        security_event("owner_login_failed", None, ip)
        audit("anonymous", "owner_login_failed", None, None, ip)
        raise ApiError(401, "login_failed", GENERIC_LOGIN_ERROR)
    db.ex("UPDATE owner_accounts SET failed_attempts=0, locked_until=NULL WHERE id=1")
    tok = new_token()
    sid = db.new_id("os")
    now = db.now()
    db.ex("INSERT INTO owner_sessions(id, token_hash, principal_id, created_at, last_seen_at, reauth_at, expires_at, "
          "user_agent, ip_hash) VALUES (?,?,?,?,?,?,?,?,?)", sid, token_hash(tok), acct["principal_id"], now, now, now,
          now + OWNER_ABSOLUTE, (request.headers.get("user-agent") or "")[:200], ip_hash(ip))
    set_cookie(response, OWNER_COOKIE, tok, OWNER_ABSOLUTE, httponly=True, samesite="strict")
    audit("owner", "owner_login", None, {"recovery_code_used": used_recovery}, ip)
    return {"ok": True, "recovery_code_used": used_recovery}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    check_csrf(request)
    s = owner_session(request)
    if s:
        db.ex("UPDATE owner_sessions SET revoked_at=? WHERE id=?", db.now(), s["id"])
        audit("owner", "owner_logout", None, None, client_ip(request))
    response.delete_cookie(OWNER_COOKIE, path="/")
    return {"ok": True}


class Reauth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    totp: str = Field(min_length=6, max_length=10)


@router.post("/reauth")
def reauth(body: Reauth, request: Request) -> dict:
    check_csrf(request)
    s = require_owner(request)
    if not rate_limit(f"owner-reauth:{s['id']}", 6, 300):
        raise ApiError(429, "rate_limited", "Too many attempts.")
    acct = db.q1("SELECT totp_secret_enc FROM owner_accounts WHERE id=1")
    if not verify_totp(unseal(acct["totp_secret_enc"], TOTP_AAD).decode(), body.totp):
        security_event("owner_reauth_failed", None, client_ip(request))
        raise ApiError(401, "bad_totp", "That code is not valid.")
    db.ex("UPDATE owner_sessions SET reauth_at=? WHERE id=?", db.now(), s["id"])
    audit("owner", "owner_step_up", None, None, client_ip(request))
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    s = require_owner(request)
    return {"display_name": s["display_name"], "email": s["email"], "session_id": s["id"],
            "step_up_valid_for": max(0, int(600 - (db.now() - s["reauth_at"])))}


# --- dashboard metrics ---------------------------------------------------------
@router.get("/metrics")
def metrics(request: Request, days: int = 30) -> dict:
    require_owner(request)
    days = max(1, min(days, 365))
    since = db.now() - days * 86400
    one = lambda sql, *a: db.q1(sql, *a)[0]  # noqa: E731
    retained = one("SELECT COUNT(*) FROM uploads WHERE deletion_state='active'")
    new = one("SELECT COUNT(*) FROM uploads WHERE created_at>=?", since)
    states = {r["state"]: r["n"] for r in db.q("SELECT state, COUNT(*) n FROM jobs WHERE created_at>=? GROUP BY state", since)}
    figs = one("SELECT COUNT(*) FROM figures f JOIN jobs j ON j.id=f.job_id WHERE j.created_at>=? AND f.status IN "
               "('insert','renumber','replace','preserve')", since)
    avg = db.q1("SELECT AVG(finished_at-created_at) a FROM jobs WHERE finished_at IS NOT NULL AND created_at>=? "
                "AND state='review_ready'", since)["a"]
    series = db.q("SELECT CAST((created_at-?)/86400 AS INTEGER) d, COUNT(*) n FROM uploads WHERE created_at>=? GROUP BY d",
                  since, since)
    buckets = [0] * days
    for r in series:
        if 0 <= r["d"] < days:
            buckets[r["d"]] += r["n"]
    stuck = db.q("SELECT id, state, updated_at FROM jobs WHERE state NOT IN ('review_ready','failed','canceled','expired','deleted') "
                 "AND updated_at < ?", db.now() - 900)
    render_fail = one("SELECT COUNT(*) FROM output_revisions WHERE preview_state='failed' AND created_at>=?", since)
    soon = one("SELECT COUNT(*) FROM uploads WHERE deletion_state='active' AND retention_expires_at < ?", db.now() + 3 * 86400)
    cfg = get_config()
    return {
        "days": days, "retained_uploads": retained, "new_uploads": new, "jobs_by_state": states,
        "completed_jobs": states.get("review_ready", 0), "failed_jobs": states.get("failed", 0),
        "storage_bytes": store().usage_bytes(), "captioned_figures": figs,
        "avg_processing_seconds": round(avg, 1) if avg else None, "uploads_per_day": buckets,
        "stuck_jobs": [dict(r) for r in stuck], "preview_failures": render_fail, "expiring_within_3_days": soon,
        "queue": queue.depth(), "ai_provider": cfg.ai_provider, "ai_calls": one("SELECT COALESCE(SUM(ai_calls),0) FROM jobs WHERE created_at>=?", since),
    }


# --- uploads library ----------------------------------------------------------------
SORTS = {"newest": "u.created_at DESC", "oldest": "u.created_at ASC", "size": "u.byte_size DESC", "status": "j.state ASC"}


@router.get("/uploads")
def list_uploads(request: Request, search: str = "", status: str = "", sort: str = "newest", page: int = 1,
                 date_from: Optional[float] = None, date_to: Optional[float] = None) -> dict:
    require_owner(request)
    where, args = ["1=1"], []
    if search:
        where.append("(u.original_name LIKE ? OR u.id LIKE ? OR u.guest_session_id LIKE ?)")
        like = f"%{search[:100]}%"
        args += [like, like, like]
    if status == "processing":
        where.append("j.state NOT IN ('review_ready','failed','canceled','expired','deleted') AND u.deletion_state='active'")
    elif status == "success":
        where.append("j.state='review_ready' AND u.deletion_state='active'")
    elif status == "failed":
        where.append("j.state='failed' AND u.deletion_state='active'")
    elif status == "expired":
        where.append("u.deletion_state='expired'")
    elif status == "deleted":
        where.append("u.deletion_state IN ('user_deleted','owner_deleted')")
    elif status == "retained":
        where.append("u.deletion_state='active'")
    if date_from:
        where.append("u.created_at>=?")
        args.append(date_from)
    if date_to:
        where.append("u.created_at<=?")
        args.append(date_to)
    page = max(1, page)
    sql_where = " AND ".join(where)
    total = db.q1(f"SELECT COUNT(*) FROM uploads u JOIN jobs j ON j.upload_id=u.id WHERE {sql_where}", *args)[0]
    rows = db.q(f"SELECT u.*, j.id AS job_id, j.state, j.ready_revision, j.failure_code, "
                f"(SELECT COUNT(*) FROM figures f WHERE f.job_id=j.id AND f.status IN ('insert','renumber','replace','preserve')) AS captions "
                f"FROM uploads u JOIN jobs j ON j.upload_id=u.id WHERE {sql_where} ORDER BY {SORTS.get(sort, SORTS['newest'])} "
                f"LIMIT 25 OFFSET ?", *args, (page - 1) * 25)
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "job_id": r["job_id"], "original_name": r["original_name"],
            "uploader": "Anonymous guest · " + r["guest_session_id"][-6:], "created_at": r["created_at"],
            "byte_size": r["byte_size"], "page_count": r["page_count"], "figure_count": r["figure_count"],
            "captions": r["captions"], "state": r["state"], "deletion_state": r["deletion_state"],
            "retention_expires_at": r["retention_expires_at"], "failure_code": r["failure_code"],
            "preview_available": bool(r["ready_revision"]) and r["deletion_state"] == "active",
            "original_available": bool(r["object_key"]) and r["deletion_state"] == "active",
        })
    return {"total": total, "page": page, "page_size": 25, "items": items}


def _owner_upload(upload_id: str) -> dict:
    row = db.q1("SELECT u.*, j.id AS job_id FROM uploads u JOIN jobs j ON j.upload_id=u.id WHERE u.id=?", upload_id)
    if row is None:
        raise not_found()
    return dict(row)


@router.get("/uploads/{upload_id}")
def upload_detail(upload_id: str, request: Request) -> dict:
    require_owner(request)
    up = _owner_upload(upload_id)
    job = dict(db.q1("SELECT * FROM jobs WHERE id=?", up["job_id"]))
    revs = db.q("SELECT revision, edits_version, created_at, preview_state, byte_size, page_count, sha256 FROM output_revisions "
                "WHERE job_id=? ORDER BY revision DESC", job["id"])
    events = db.q("SELECT at, actor, action, detail FROM audit_events WHERE target_upload_id=? ORDER BY id DESC LIMIT 50", upload_id)
    figs = figure_payload(job["id"], f"/api/owner/uploads/{upload_id}") if up["deletion_state"] == "active" else []
    return {
        "upload": {"id": up["id"], "original_name": up["original_name"], "file_type": up["file_type"],
                   "byte_size": up["byte_size"], "sha256_prefix": up["sha256"][:16], "created_at": up["created_at"],
                   "retention_expires_at": up["retention_expires_at"], "deletion_state": up["deletion_state"],
                   "deleted_at": up["deleted_at"], "uploader": "Anonymous guest · " + up["guest_session_id"][-6:],
                   "page_count": up["page_count"], "figure_count": up["figure_count"],
                   "validation_warnings": db.jload(up["validation_warnings"], []),
                   "original_available": bool(up["object_key"]) and up["deletion_state"] == "active"},
        "job": job_payload(job), "figures": figs,
        "revisions": [dict(r) for r in revs],
        "activity": [dict(e) for e in events],
    }


@router.get("/uploads/{upload_id}/figures/{figure_id}/thumb")
def owner_thumb(upload_id: str, figure_id: str, request: Request) -> RawResponse:
    require_owner(request)
    up = _owner_upload(upload_id)
    row = db.q1("SELECT thumb_key FROM figures WHERE job_id=? AND id=?", up["job_id"], figure_id)
    if not row or not row["thumb_key"]:
        raise not_found()
    return RawResponse(store().get(row["thumb_key"]), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/uploads/{upload_id}/preview")
def owner_preview(upload_id: str, request: Request, kind: str = "source", revision: Optional[int] = None) -> dict:
    s = require_owner(request)
    up = _owner_upload(upload_id)
    if up["deletion_state"] != "active":
        return {"state": "deleted", "pages": []}
    if kind not in ("source", "output"):
        raise ApiError(400, "bad_kind", "Unknown preview kind.")
    man = preview_manifest_for(up["job_id"], kind, revision, f"/api/owner/uploads/{upload_id}")
    if man.get("state") == "ready":
        audit("owner", "original_preview_opened" if kind == "source" else "processed_preview_opened", upload_id,
              {"revision": man.get("revision"), "session": s["id"][-8:]}, client_ip(request))
    return man


@router.get("/uploads/{upload_id}/pages/{kind}/{rev}/{n}")
def owner_page(upload_id: str, kind: str, rev: str, n: int, request: Request, thumb: int = 0) -> RawResponse:
    require_owner(request)
    up = _owner_upload(upload_id)
    if up["deletion_state"] != "active" or kind not in ("source", "output"):
        raise not_found()
    return RawResponse(page_object(up["job_id"], kind, rev, n, bool(thumb)), media_type="image/png",
                       headers={"Cache-Control": "no-store"})


@router.get("/uploads/{upload_id}/download")
def owner_download(upload_id: str, request: Request, version: str = "processed", revision: Optional[int] = None) -> RawResponse:
    s = require_owner(request)
    require_step_up(s)
    up = _owner_upload(upload_id)
    if up["deletion_state"] != "active":
        raise ApiError(410, "deleted", "This upload's files have been deleted.")
    if version == "original":
        data = store().get(up["object_key"])
        if hashlib.sha256(data).hexdigest() != up["sha256"]:
            raise ApiError(500, "integrity_error", "Stored original failed its integrity check.")
        audit("owner", "original_downloaded", upload_id, None, client_ip(request))
        return docx_response(data, up["original_name"])
    job = db.q1("SELECT ready_revision FROM jobs WHERE id=?", up["job_id"])
    rev = revision or (job["ready_revision"] if job else None)
    row = db.q1("SELECT docx_key FROM output_revisions WHERE job_id=? AND revision=? AND preview_state='ready'",
                up["job_id"], rev)
    if not row or not row["docx_key"]:
        raise ApiError(409, "no_output", "There is no verified processed version for this upload.")
    audit("owner", "output_downloaded", upload_id, {"revision": rev}, client_ip(request))
    return docx_response(store().get(row["docx_key"]), output_filename(up["original_name"]))


@router.delete("/uploads/{upload_id}")
def owner_delete(upload_id: str, request: Request) -> dict:
    check_csrf(request)
    s = require_owner(request)
    require_step_up(s)
    up = _owner_upload(upload_id)
    if up["deletion_state"] != "active":
        return {"deleted": False, "already": up["deletion_state"]}
    n = delete_upload_content(upload_id, "owner", "owner_deleted")
    audit("owner", "upload_deleted_by_owner", upload_id, {"objects_removed": n}, client_ip(request))
    return {"deleted": True, "objects_removed": n}


class BulkDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_ids: list[str] = Field(min_length=1, max_length=200)
    confirm: str


@router.post("/uploads/bulk-delete")
def bulk_delete(body: BulkDelete, request: Request) -> dict:
    check_csrf(request)
    s = require_owner(request)
    require_step_up(s)
    if body.confirm != f"DELETE {len(body.upload_ids)}":
        raise ApiError(400, "confirm_mismatch", f"Type DELETE {len(body.upload_ids)} to confirm.")
    total = 0
    for uid in body.upload_ids:
        row = db.q1("SELECT deletion_state FROM uploads WHERE id=?", uid)
        if row and row["deletion_state"] == "active":
            total += delete_upload_content(uid, "owner", "owner_deleted")
            audit("owner", "upload_deleted_by_owner", uid, {"bulk": True}, client_ip(request))
    return {"deleted": len(body.upload_ids), "objects_removed": total}


@router.get("/uploads/{upload_id}/delete-impact")
def delete_impact(upload_id: str, request: Request) -> dict:
    require_owner(request)
    _owner_upload(upload_id)
    rows = db.q("SELECT kind, COUNT(*) n FROM objects WHERE upload_id=? AND deleted_at IS NULL GROUP BY kind", upload_id)
    return {"objects": {r["kind"]: r["n"] for r in rows}, "total": sum(r["n"] for r in rows)}


# --- jobs, audit, security, settings, retention --------------------------------------
@router.get("/jobs")
def jobs(request: Request, state: str = "") -> dict:
    require_owner(request)
    args = []
    where = "1=1"
    if state:
        where = "j.state=?"
        args.append(state)
    rows = db.q(f"SELECT j.id, j.upload_id, j.state, j.failure_code, j.created_at, j.updated_at, j.current_revision, "
                f"j.ready_revision, j.ai_calls, u.original_name FROM jobs j JOIN uploads u ON u.id=j.upload_id WHERE {where} "
                f"ORDER BY j.created_at DESC LIMIT 100", *args)
    return {"jobs": [dict(r) for r in rows], "queue": queue.depth(),
            "dead_tasks": [dict(r) for r in db.q("SELECT id, kind, last_error, attempts, created_at FROM queue WHERE state='dead' ORDER BY id DESC LIMIT 20")]}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request) -> dict:
    check_csrf(request)
    require_owner(request)
    job = db.q1("SELECT j.state, u.deletion_state FROM jobs j JOIN uploads u ON u.id=j.upload_id WHERE j.id=?", job_id)
    if not job or job["deletion_state"] != "active":
        raise not_found()
    if job["state"] != "failed":
        raise ApiError(409, "not_failed", "Only failed jobs can be retried.")
    db.ex("UPDATE jobs SET failure_code=NULL, state='uploaded', stage_index=0 WHERE id=?", job_id)
    queue.enqueue("analyze", {"job_id": job_id}, dedupe_key=f"analyze:{job_id}")
    audit("owner", "job_retried", None, {"job": job_id}, client_ip(request))
    return {"queued": True}


@router.get("/audit")
def audit_log(request: Request, page: int = 1) -> dict:
    require_owner(request)
    page = max(1, page)
    rows = db.q("SELECT id, at, actor, action, target_upload_id, detail FROM audit_events ORDER BY id DESC LIMIT 50 OFFSET ?",
                (page - 1) * 50)
    ok, n = verify_audit_chain()
    return {"events": [dict(r, detail=db.jload(r["detail"], {})) for r in rows], "chain_valid": ok, "chain_length": n,
            "page": page}


@router.get("/security")
def security(request: Request) -> dict:
    s = require_owner(request)
    sessions = db.q("SELECT id, created_at, last_seen_at, expires_at, revoked_at, user_agent FROM owner_sessions "
                    "WHERE expires_at > ? ORDER BY created_at DESC LIMIT 30", db.now())
    events = db.q("SELECT at, kind, detail FROM security_events ORDER BY id DESC LIMIT 60")
    acct = db.q1("SELECT recovery_codes, updated_at, created_at FROM owner_accounts WHERE id=1")
    return {"current_session": s["id"], "sessions": [dict(r) for r in sessions],
            "events": [dict(e, detail=db.jload(e["detail"], {})) for e in events],
            "mfa": {"type": "TOTP authenticator", "recovery_codes_remaining": len(db.jload(acct["recovery_codes"], []))},
            "account_updated_at": acct["updated_at"]}


@router.post("/sessions/{session_id}/revoke")
def revoke(session_id: str, request: Request) -> dict:
    check_csrf(request)
    require_owner(request)
    db.ex("UPDATE owner_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", db.now(), session_id)
    audit("owner", "session_revoked", None, {"session": session_id[-8:]}, client_ip(request))
    return {"ok": True}


@router.post("/sessions/revoke-others")
def revoke_others(request: Request) -> dict:
    check_csrf(request)
    s = require_owner(request)
    db.ex("UPDATE owner_sessions SET revoked_at=? WHERE id<>? AND revoked_at IS NULL", db.now(), s["id"])
    audit("owner", "other_sessions_revoked", None, None, client_ip(request))
    return {"ok": True}


class MfaRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=256)
    new_secret: str = Field(default="", max_length=64)
    new_totp: str = Field(default="", max_length=10)


_pending_rotation: dict[str, str] = {}


@router.post("/mfa/rotate")
def rotate_mfa(body: MfaRotate, request: Request) -> dict:
    """Two-step: (1) password + step-up → new secret issued; (2) code from the new secret confirms it."""
    check_csrf(request)
    s = require_owner(request)
    require_step_up(s)
    acct = db.q1("SELECT * FROM owner_accounts WHERE id=1")
    if not verify_password(body.password, acct["password_hash"]):
        security_event("mfa_rotate_bad_password", None, client_ip(request))
        raise ApiError(401, "bad_password", "Password is incorrect.")
    if not body.new_totp:
        secret = new_totp_secret()
        _pending_rotation[s["id"]] = secret
        uri = otpauth_uri(secret, acct["email"])
        return {"step": "confirm", "secret": secret, "otpauth_uri": uri, "qr_svg": _qr_svg(uri)}
    secret = _pending_rotation.get(s["id"])
    if not secret or not verify_totp(secret, body.new_totp):
        raise ApiError(400, "bad_totp", "The code from the new authenticator entry is not valid.")
    codes, hashes = recovery_codes()
    db.ex("UPDATE owner_accounts SET totp_secret_enc=?, recovery_codes=?, updated_at=? WHERE id=1",
          seal(secret.encode(), TOTP_AAD), db.jdump(hashes), db.now())
    db.ex("UPDATE owner_sessions SET revoked_at=? WHERE id<>? AND revoked_at IS NULL", db.now(), s["id"])
    _pending_rotation.pop(s["id"], None)
    audit("owner", "mfa_rotated", None, None, client_ip(request))
    return {"step": "done", "recovery_codes": codes}


class ServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_upload_mb: int = Field(ge=1, le=200)
    retention_days: int = Field(ge=1, le=365)
    max_figures: int = Field(ge=1, le=2000)
    max_pages: int = Field(ge=1, le=2000)
    max_ai_suggestions_per_job: int = Field(ge=0, le=1000)
    maintenance_mode: bool = False
    maintenance_message: str = Field(default="", max_length=240)


@router.get("/settings")
def get_settings(request: Request) -> dict:
    require_owner(request)
    cfg = get_config()
    from ..engine.render import renderer_status
    versions = db.q("SELECT version, created_at, actor FROM settings_versions ORDER BY version DESC LIMIT 20")
    return {"settings": db.service_settings(), "versions": [dict(v) for v in versions],
            "environment": {"ai_provider": cfg.ai_provider, "ai_model": cfg.ai_model if cfg.ai_provider == "anthropic" else None,
                            "renderer": renderer_status(), "storage": "encrypted local object store (AES-256-GCM)",
                            "secure_cookies": cfg.secure_cookies, "version": cfg.version}}


@router.patch("/settings")
def patch_settings(body: ServiceSettings, request: Request) -> dict:
    check_csrf(request)
    s = require_owner(request)
    require_step_up(s)
    cur = db.service_settings()
    data = body.model_dump()
    data["caption_defaults"] = cur.get("caption_defaults", {})
    db.ex("INSERT INTO settings_versions(data, created_at, actor) VALUES (?,?,?)", db.jdump(data), db.now(), "owner")
    changed = {k: [cur.get(k), v] for k, v in data.items() if cur.get(k) != v}
    audit("owner", "settings_changed", None, {"changed": changed}, client_ip(request))
    return {"ok": True, "settings": db.service_settings()}


@router.post("/settings/restore/{version}")
def restore_settings(version: int, request: Request) -> dict:
    check_csrf(request)
    s = require_owner(request)
    require_step_up(s)
    row = db.q1("SELECT data FROM settings_versions WHERE version=?", version)
    if not row:
        raise not_found()
    db.ex("INSERT INTO settings_versions(data, created_at, actor) VALUES (?,?,?)", row["data"], db.now(), "owner")
    audit("owner", "settings_restored", None, {"from_version": version}, client_ip(request))
    return {"ok": True}


@router.get("/retention")
def retention(request: Request) -> dict:
    require_owner(request)
    upcoming = db.q("SELECT id, original_name, retention_expires_at, byte_size FROM uploads WHERE deletion_state='active' "
                    "ORDER BY retention_expires_at ASC LIMIT 50")
    deleted = db.q("SELECT id, original_name, deletion_state, deleted_at FROM uploads WHERE deletion_state<>'active' "
                   "ORDER BY deleted_at DESC LIMIT 50")
    leftover = db.q1("SELECT COUNT(*) n FROM objects o JOIN uploads u ON u.id=o.upload_id WHERE u.deletion_state<>'active' "
                     "AND o.deleted_at IS NULL")["n"]
    return {"retention_days": db.service_settings()["retention_days"], "upcoming": [dict(r) for r in upcoming],
            "recently_deleted": [dict(r) for r in deleted], "orphaned_objects": leftover}


@router.post("/retention/run")
def run_retention(request: Request) -> dict:
    check_csrf(request)
    require_owner(request)
    n = retention_sweep()
    audit("owner", "retention_sweep_run", None, {"expired": n}, client_ip(request))
    return {"expired": n}
