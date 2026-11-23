"""User (guest) API: upload, job status, figures, caption edits, preview, download, delete."""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import Response as RawResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import db, queue
from ..ai.captioner import get_captioner, suggest_with_fallback
from ..config import get_config
from ..engine.numbering import STATUS_LABELS
from ..engine.process import media_for_ai
from ..engine.settings import CaptionSettings, FigureOverride
from ..engine.validation import Limits, output_filename, safe_display_name, validate_docx
from ..pipeline import (FAILURE_MESSAGES, PIPELINE, USER_STEPS, _context, delete_upload_content, load_figures,
                        save_figures, uses_models)
from ..security import audit
from ..storage import store
from .common import (ApiError, check_csrf, client_ip, ensure_csrf_cookie, guest_job, guest_session, limit,
                     not_found)

router = APIRouter(prefix="/api")
CONSENT_VERSION = "2026-11-22"
CHUNK_BYTES = 3 * 1024 * 1024  # below Vercel's 4.5 MB request/response limit


def chunked_or_full(data: bytes, chunk: Optional[int], media_type: str, filename: Optional[str] = None) -> RawResponse:
    """Large files travel in 3 MB pieces on serverless hosts; the browser reassembles them."""
    total = max(1, -(-len(data) // CHUNK_BYTES))
    if chunk is None:
        return docx_response(data, filename) if filename else RawResponse(data, media_type=media_type)
    if chunk < 0 or chunk >= total:
        raise ApiError(416, "bad_chunk", "Invalid chunk.")
    return RawResponse(data[chunk * CHUNK_BYTES:(chunk + 1) * CHUNK_BYTES], media_type="application/octet-stream",
                       headers={"X-Total-Chunks": str(total), "X-Total-Size": str(len(data)), "Cache-Control": "no-store",
                                "Access-Control-Expose-Headers": "X-Total-Chunks, X-Total-Size"})


def storage_used_mb() -> float:
    return store().usage_bytes() / (1024 * 1024)


def check_capacity(svc: dict, incoming_mb: float = 0) -> None:
    """Refuse new work politely before storage is full or the daily cap is reached (protects the database)."""
    used = storage_used_mb()
    if used + incoming_mb > svc["storage_limit_mb"] * 0.95:
        raise ApiError(507, "storage_full", "The service is at capacity right now. Please try again later.")
    today = db.q1("SELECT COUNT(*) n FROM uploads WHERE created_at > ?", db.now() - 86400)["n"]
    if today >= svc["max_uploads_per_day"]:
        raise ApiError(429, "daily_limit", "Today's upload limit for the service has been reached. Please try again tomorrow.")


@router.post("/uploads/chunk")
async def upload_chunk(request: Request, response: Response) -> dict:
    """Receive one ≤3 MB piece of a Word file (serverless request-size limit)."""
    check_csrf(request)
    limit("upload-chunk", 400, 3600, request)
    gid = guest_session(request, response, create=True)
    token = request.headers.get("x-upload-token", "")
    idx = request.headers.get("x-chunk-index", "")
    if not gid or not (16 <= len(token) <= 64) or not token.replace("-", "").isalnum() or not idx.isdigit() or int(idx) > 100:
        raise ApiError(400, "bad_chunk", "Invalid upload piece.")
    if idx == "0":
        check_capacity(db.service_settings())
    data = await request.body()
    if len(data) > CHUNK_BYTES or not data:
        raise ApiError(413, "chunk_too_large", "Upload piece too large.")
    db.ex("INSERT INTO upload_chunks(token, idx, data, created_at) VALUES (?,?,?,?) ON CONFLICT(token, idx) DO UPDATE "
          "SET data=excluded.data", f"{gid}:{token}", int(idx), data, db.now())
    return {"ok": True}


def _assemble_chunks(gid: str, token: str, count: int, max_bytes: int) -> bytes:
    key = f"{gid}:{token}"
    rows = db.q("SELECT idx, data FROM upload_chunks WHERE token=? ORDER BY idx", key)
    if [r["idx"] for r in rows] != list(range(count)):
        raise ApiError(400, "upload_incomplete", "Part of the upload is missing. Please try again.")
    raw = b"".join(bytes(r["data"]) for r in rows)
    db.ex("DELETE FROM upload_chunks WHERE token=?", key)
    if len(raw) > max_bytes:
        raise ApiError(400, "too_large", "The file is larger than the allowed limit.")
    return raw


@router.get("/session")
def session_info(request: Request, response: Response) -> dict:
    tok = ensure_csrf_cookie(request, response)
    cfg = get_config()
    svc = db.service_settings()
    return {
        "csrf": tok,
        "limits": {"max_upload_mb": svc["max_upload_mb"], "formats": [".docx"]},
        "retention_days": svc["retention_days"],
        "owner_name": cfg.owner_display_name,
        "ai_provider": provider_label(cfg.ai_provider),
        "maintenance": svc["maintenance_mode"],
        "maintenance_message": svc["maintenance_message"],
        "lite": cfg.lite,
        "chunk_bytes": CHUNK_BYTES,
    }


def external_ai_providers() -> list[str]:
    """Third parties that actually receive image content in this deployment (for honest disclosure)."""
    import os
    p = get_config().ai_provider
    out = []
    if p in ("autofigure", "anthropic") and os.environ.get("ANTHROPIC_API_KEY") and \
            "anthropic" not in os.environ.get("AUTOFIGURE_AI_DISABLE", ""):
        out.append("Anthropic (Claude)")
    if p in ("autofigure", "openai") and os.environ.get("OPENAI_API_KEY") and \
            "openai" not in os.environ.get("AUTOFIGURE_AI_DISABLE", ""):
        out.append("OpenAI (ChatGPT)")
    return out


def provider_label(p: str) -> Optional[str]:
    names = external_ai_providers()
    return " and ".join(names) if names else None


@router.post("/uploads", status_code=202)
async def create_upload(request: Request, response: Response, file: Optional[UploadFile] = File(None),
                        consent: str = Form(""), settings: str = Form("{}"), chunk_token: str = Form(""),
                        chunk_count: int = Form(0), filename: str = Form("")) -> dict:
    check_csrf(request)
    svc = db.service_settings()
    if svc["maintenance_mode"]:
        raise ApiError(503, "maintenance", svc["maintenance_message"] or "Uploads are paused for maintenance.")
    limit("upload", 20, 3600, request)
    check_capacity(svc)
    if consent != "yes":
        raise ApiError(400, "consent_required", "Please confirm you have read how your file is stored and who can access it.")
    try:
        cs = CaptionSettings.model_validate_json(settings or "{}")
    except ValidationError:
        raise ApiError(400, "invalid_settings", "Caption settings are invalid.")
    max_bytes = svc["max_upload_mb"] * 1024 * 1024
    if file is not None:
        raw = await file.read(max_bytes + 1)
        name = safe_display_name(file.filename or "")
    elif chunk_token and 0 < chunk_count <= 100:
        gid0 = guest_session(request, response, create=True)
        raw = _assemble_chunks(gid0, chunk_token, chunk_count, max_bytes + 1)
        name = safe_display_name(filename)
    else:
        raise ApiError(400, "no_file", "Choose a Word (.docx) file first.")
    v = validate_docx(name, raw, Limits(max_bytes=max_bytes))
    if not v.ok:
        # rejected files are never stored or sent anywhere
        raise ApiError(415 if v.code.startswith(("wrong_", "legacy", "not_", "macro")) else 400, v.code, v.message)
    gid = guest_session(request, response, create=True)
    if not db.q1("SELECT 1 FROM guest_sessions WHERE id=?", gid):
        raise ApiError(400, "session_error", "Could not create a secure session.")
    upload_id = db.new_id("up")
    key = store().put(raw, "orig", upload_id)
    now = db.now()
    job_id = db.new_id("job")
    with db.tx() as c:
        c.execute("INSERT INTO uploads(id, guest_session_id, original_name, file_type, byte_size, object_key, sha256, "
                  "consent_version, created_at, retention_expires_at, validation_warnings) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (upload_id, gid, name, "docx", len(raw), key, hashlib.sha256(raw).hexdigest(), CONSENT_VERSION, now,
                   now + svc["retention_days"] * 86400, db.jdump(v.warnings)))
        c.execute("INSERT INTO jobs(id, upload_id, state, settings, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                  (job_id, upload_id, "uploaded", cs.model_dump_json(), now, now))
        c.execute("INSERT INTO job_events(job_id, state, at, detail) VALUES (?,?,?,?)", (job_id, "uploaded", now, None))
    queue.enqueue("analyze", {"job_id": job_id}, dedupe_key=f"analyze:{job_id}")
    return {"upload_id": upload_id, "job_id": job_id, "status_url": f"/api/jobs/{job_id}",
            "review_url": f"/app/jobs/{job_id}", "warnings": v.warnings}


@router.post("/session/transfer")
def create_transfer(request: Request) -> dict:
    """Single-use, 15-minute link that opens this browser's documents on another device."""
    import io
    import secrets as _s
    import qrcode
    import qrcode.image.svg
    from ..security import token_hash as _th
    check_csrf(request)
    limit("transfer", 10, 3600, request)
    gid = guest_session(request)
    if not gid:
        raise ApiError(400, "no_session", "Upload a document first.")
    code = _s.token_urlsafe(24)
    db.ex("INSERT INTO session_transfers(code_hash, guest_session_id, expires_at) VALUES (?,?,?)", _th("xfer:" + code), gid,
          db.now() + 900)
    url = f"{str(request.base_url).rstrip('/')}/resume#{code}"
    buf = io.BytesIO()
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2).save(buf)
    return {"url": url, "qr_svg": buf.getvalue().decode(), "expires_in": 900}


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=20, max_length=80)


@router.post("/session/resume")
def resume_session(body: ResumeBody, request: Request, response: Response) -> dict:
    import secrets as _s
    from ..security import token_hash as _th
    from .common import GUEST_COOKIE, GUEST_TTL, set_cookie
    check_csrf(request)
    limit("resume", 20, 3600, request)
    ch = _th("xfer:" + body.code)
    with db.tx() as c:
        row = c.execute("SELECT guest_session_id, expires_at, used_at FROM session_transfers WHERE code_hash=?", (ch,)).fetchone()
        if row is None or row["used_at"] is not None or row["expires_at"] < db.now():
            raise ApiError(400, "link_invalid", "This link has expired or was already used. Create a new one on your other device.")
        c.execute("UPDATE session_transfers SET used_at=? WHERE code_hash=?", (db.now(), ch))
        tok = _s.token_urlsafe(32)
        c.execute("INSERT INTO guest_tokens(token_hash, guest_session_id, created_at) VALUES (?,?,?)",
                  (_th(tok), row["guest_session_id"], db.now()))
    set_cookie(response, GUEST_COOKIE, tok, GUEST_TTL, httponly=True, samesite="lax")
    return {"ok": True}


@router.get("/uploads")
def my_uploads(request: Request) -> dict:
    gid = guest_session(request)
    if not gid:
        return {"uploads": []}
    rows = db.q("SELECT u.id, u.original_name, u.byte_size, u.created_at, u.retention_expires_at, j.id AS job_id, j.state "
                "FROM uploads u JOIN jobs j ON j.upload_id=u.id WHERE u.guest_session_id=? AND u.deletion_state='active' "
                "ORDER BY u.created_at DESC LIMIT 50", gid)
    return {"uploads": [dict(r) for r in rows]}


def _steps(state: str) -> list[dict]:
    idx = PIPELINE.index(state) if state in PIPELINE else -1
    out = []
    for label, states in USER_STEPS:
        positions = [PIPELINE.index(s) for s in states]
        if state == "regenerating":
            st = "done"
        elif idx > max(positions) or (state == "review_ready" and "review_ready" in states):
            st = "done"
        elif idx >= min(positions):
            st = "active"
        else:
            st = "pending"
        out.append({"label": label, "state": st})
    if state == "failed":
        for s in out:
            if s["state"] != "done":
                s["state"] = "failed"
                break
    return out


def job_payload(job: dict) -> dict[str, Any]:
    summary = db.jload(job.get("summary"), {}) or {}
    analysis = db.jload(job.get("analysis"), {}) or {}
    events = db.q("SELECT state, at, detail FROM job_events WHERE job_id=? ORDER BY id", job["id"])
    rev = None
    if job.get("ready_revision"):
        r = db.q1("SELECT revision, created_at, byte_size, page_count, verification, preview_state FROM output_revisions "
                  "WHERE job_id=? AND revision=?", job["id"], job["ready_revision"])
        if r:
            rev = {"revision": r["revision"], "created_at": r["created_at"], "byte_size": r["byte_size"],
                   "page_count": r["page_count"], "verification": db.jload(r["verification"], {}),
                   "preview_state": r["preview_state"]}
    failure = None
    if job.get("failure_code"):
        what, nxt = FAILURE_MESSAGES.get(job["failure_code"], FAILURE_MESSAGES["internal_error"])
        failure = {"code": job["failure_code"], "message": what, "next_step": nxt,
                   "preserved": "Your original Word file was not modified.",
                   "support_id": job["id"][-10:], "regeneration_only": job.get("failure_detail") == "regeneration_failed"}
    pending = job["edits_version"] != (job["rendered_edits_version"] if job["rendered_edits_version"] is not None else 0)
    return {
        "id": job["id"], "state": job["state"], "steps": _steps(job["state"]),
        "queue_position": queue.position(job["id"]) if job["state"] == "uploaded" else None,
        "created_at": job["created_at"], "updated_at": job["updated_at"],
        "summary": summary, "headings": analysis.get("headings", []), "page_count": analysis.get("page_count"),
        "unsupported": analysis.get("unsupported", []),
        "settings": db.jload(job["settings"], {}),
        "ready_revision": rev, "current_revision": job["current_revision"],
        "edits_version": job["edits_version"], "rendered_edits_version": job["rendered_edits_version"],
        "has_unrendered_changes": bool(pending and job["ready_revision"]),
        "failure": failure,
        "events": [{"state": e["state"], "at": e["at"]} for e in events][-40:],
        "status_labels": STATUS_LABELS,
        "vision_check": bool(external_ai_providers()),
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    job = guest_job(request, job_id)
    p = job_payload(job)
    p["file"] = {"name": job["original_name"], "byte_size": job["byte_size"],
                 "retention_expires_at": job["retention_expires_at"], "upload_id": job["upload_id"]}
    p["download_name"] = output_filename(job["original_name"])
    return p


def figure_payload(job_id: str, base: str) -> list[dict]:
    rows = db.q("SELECT data, thumb_key FROM figures WHERE job_id=? ORDER BY logical_order", job_id)
    out = []
    for r in rows:
        d = db.jload(r["data"])
        out.append({
            "figure_id": d["figure_id"], "order": d["occurrence_index"] + 1, "page": d["page"],
            "page_confident": d["page_confident"], "page_evidence": d["page_evidence"],
            "kind": d["kind"], "content_type": d["content_type"], "in_table": d["in_table"],
            "width_cm": d["width_cm"], "height_cm": d["height_cm"],
            "chapter_number": d["chapter_number"], "chapter_title": d["chapter_title"], "label": d["label"],
            "status": d["status"], "status_label": STATUS_LABELS.get(d["status"], d["status"]), "reason": d["reason"],
            "eligible": d["eligible"], "caption_text": d["caption_text"], "caption_source": d["caption_source"],
            "confidence_band": d["confidence_band"], "needs_review": d["needs_review"], "category": d["category"],
            "warnings": d["warnings"], "existing": d["existing"], "alt_text": d["alt_text"],
            "alternatives": d.get("alternatives", []), "engine": d.get("engine", ""),
            "mistakes": d.get("mistakes", []), "correction": d.get("correction", ""),
            "thumb_url": f"{base}/figures/{d['figure_id']}/thumb" if r["thumb_key"] else None,
        })
    return out


@router.get("/jobs/{job_id}/figures")
def get_figures(job_id: str, request: Request) -> dict:
    guest_job(request, job_id)
    return {"figures": figure_payload(job_id, f"/api/jobs/{job_id}")}


@router.get("/jobs/{job_id}/figures/{figure_id}/thumb")
def figure_thumb(job_id: str, figure_id: str, request: Request) -> RawResponse:
    guest_job(request, job_id)
    row = db.q1("SELECT thumb_key FROM figures WHERE job_id=? AND id=?", job_id, figure_id)
    if not row or not row["thumb_key"]:
        raise not_found()
    return RawResponse(store().get(row["thumb_key"]), media_type="image/png",
                       headers={"Cache-Control": "private, max-age=300"})


class FigurePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption_text: Optional[str] = Field(default=None, max_length=300)
    accept: Optional[bool] = None
    override: Optional[FigureOverride] = None


def _bump(job_id: str) -> int:
    with db.tx() as c:
        c.execute("UPDATE jobs SET edits_version=edits_version+1, updated_at=? WHERE id=?", (db.now(), job_id))
        return c.execute("SELECT edits_version FROM jobs WHERE id=?", (job_id,)).fetchone()["edits_version"]


def _require_reviewable(job: dict) -> None:
    if job["state"] not in ("review_ready", "regenerating"):
        raise ApiError(409, "not_ready", "The document is still processing.")


@router.patch("/jobs/{job_id}/figures/{figure_id}")
def patch_figure(job_id: str, figure_id: str, body: FigurePatch, request: Request) -> dict:
    check_csrf(request)
    job = guest_job(request, job_id)
    _require_reviewable(job)
    figs = load_figures(job_id)
    fig = next((f for f in figs if f.figure_id == figure_id), None)
    if fig is None:
        raise not_found()
    if body.caption_text is not None:
        from ..engine.writer import clean_text
        text = clean_text(body.caption_text, 240)
        fig.caption_text, fig.caption_source, fig.needs_review = text, "manual", False
        fig.confidence_band, fig.engine = "verified", ""
        db.ex("INSERT INTO caption_revisions(job_id, figure_id, caption_text, source, created_at) VALUES (?,?,?,?,?)",
              job_id, figure_id, text, "manual", db.now())
    if body.accept:
        fig.needs_review = False
    if body.override is not None:
        st = job_settings_obj(job)
        st.figure_overrides[figure_id] = body.override
        db.ex("UPDATE jobs SET settings=? WHERE id=?", st.model_dump_json(), job_id)
    save_figures(job_id, [fig])
    ev = _bump(job_id)
    return {"ok": True, "edits_version": ev}


def job_settings_obj(job: dict) -> CaptionSettings:
    return CaptionSettings.model_validate(db.jload(job["settings"], {}))


@router.get("/jobs/{job_id}/figures/{figure_id}/history")
def figure_history(job_id: str, figure_id: str, request: Request) -> dict:
    guest_job(request, job_id)
    rows = db.q("SELECT caption_text, source, created_at FROM caption_revisions WHERE job_id=? AND figure_id=? "
                "ORDER BY id DESC LIMIT 20", job_id, figure_id)
    return {"history": [dict(r) for r in rows]}


class SuggestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: str = Field(default="regenerate", pattern="^(regenerate|shorter|more_technical|more_formal)$")


@router.post("/jobs/{job_id}/figures/{figure_id}/suggest")
def suggest(job_id: str, figure_id: str, body: SuggestBody, request: Request) -> dict:
    check_csrf(request)
    job = guest_job(request, job_id)
    _require_reviewable(job)
    svc = db.service_settings()
    if job["ai_calls"] >= svc["max_ai_suggestions_per_job"]:
        raise ApiError(429, "ai_budget_reached", "The suggestion limit for this document has been reached. Edit the caption manually.")
    limit("suggest", 60, 3600, request)
    figs = load_figures(job_id)
    fig = next((f for f in figs if f.figure_id == figure_id), None)
    if fig is None:
        raise not_found()
    cfg = get_config()
    cap = get_captioner(cfg.ai_provider, cfg.ai_model)
    original = store().get(job["source_key"])
    meta = db.jload(job["analysis"], {})
    s = suggest_with_fallback(cap, media_for_ai(original, fig.media_part),
                              _context(fig, job_settings_obj(job), meta.get("language_hint", "en"), body.variant))
    if uses_models(cap):
        db.ex("UPDATE jobs SET ai_calls=ai_calls+1 WHERE id=?", job_id)
    return {"suggestion": s.caption, "confidence_band": s.confidence_band, "needs_review": s.needs_review,
            "source": s.source, "uncertain": s.uncertain_details, "alternatives": s.alternatives, "engine": s.engine}


@router.patch("/jobs/{job_id}/settings")
def patch_settings(job_id: str, body: dict, request: Request) -> dict:
    check_csrf(request)
    job = guest_job(request, job_id)
    _require_reviewable(job)
    current = db.jload(job["settings"], {})
    body.pop("figure_overrides", None)
    heading_overrides = body.pop("heading_overrides", None)
    current.update(body)
    if heading_overrides is not None:
        current["heading_overrides"] = heading_overrides
    try:
        st = CaptionSettings.model_validate(current)
    except ValidationError as e:
        raise ApiError(400, "invalid_settings", "; ".join(err["msg"] for err in e.errors()[:3]))
    db.ex("UPDATE jobs SET settings=? WHERE id=?", st.model_dump_json(), job_id)
    ev = _bump(job_id)
    return {"ok": True, "settings": st.model_dump(), "edits_version": ev}


@router.post("/jobs/{job_id}/accept-confident")
def accept_confident(job_id: str, request: Request) -> dict:
    check_csrf(request)
    job = guest_job(request, job_id)
    _require_reviewable(job)
    figs = load_figures(job_id)
    changed = [f for f in figs if f.needs_review and f.confidence_band in ("high", "medium") and f.caption_source == "ai"]
    for f in changed:
        f.needs_review = False
    save_figures(job_id, changed)
    return {"accepted": len(changed)}


class CorrectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    figure_ids: Optional[list[str]] = Field(default=None, max_length=2000)  # None = correct every mistake
    dismiss: bool = False  # "Keep mine": ignore the suggested correction


@router.post("/jobs/{job_id}/correct", status_code=202)
def correct_mistakes(job_id: str, body: CorrectBody, request: Request) -> dict:
    """Apply the proposed fixes to the author's existing captions and regenerate the Word file."""
    check_csrf(request)
    job = guest_job(request, job_id)
    _require_reviewable(job)
    figs = load_figures(job_id)
    targets = [f for f in figs if f.mistakes and (body.figure_ids is None or f.figure_id in body.figure_ids)]
    if body.figure_ids is not None and len(targets) != len(set(body.figure_ids)):
        raise not_found()
    st = job_settings_obj(job)
    corrected = []
    for f in targets:
        if body.dismiss:
            f.mistakes = [m for m in f.mistakes if m["kind"] != "description"]
            f.correction = ""
            if "dismissed_correction" not in f.warnings:
                f.warnings.append("dismissed_correction")
            continue
        if f.correction:
            f.caption_text, f.caption_source = f.correction, "corrected"
            f.needs_review, f.confidence_band = False, "verified"
            db.ex("INSERT INTO caption_revisions(job_id, figure_id, caption_text, source, created_at) VALUES (?,?,?,?,?)",
                  job_id, f.figure_id, f.correction, "ai_correction", db.now())
        elif f.existing and not f.caption_text:
            f.caption_text = f.existing.description
        ov = st.figure_overrides.get(f.figure_id) or FigureOverride()
        ov.rewrite = True
        st.figure_overrides[f.figure_id] = ov
        f.mistakes = []
        corrected.append(f.figure_id)
    save_figures(job_id, targets)
    db.ex("UPDATE jobs SET settings=? WHERE id=?", st.model_dump_json(), job_id)
    ev = _bump(job_id)
    if corrected:
        queue.enqueue("regenerate", {"job_id": job_id, "edits_version": ev}, dedupe_key=f"regen:{job_id}:{ev}")
    return {"corrected": len(corrected), "dismissed": len(targets) - len(corrected), "regenerating": bool(corrected)}


@router.post("/jobs/{job_id}/render", status_code=202)
def apply_changes(job_id: str, request: Request) -> dict:
    check_csrf(request)
    job = guest_job(request, job_id)
    _require_reviewable(job)
    limit("render", 40, 3600, request)
    queue.enqueue("regenerate", {"job_id": job_id, "edits_version": job["edits_version"]},
                  dedupe_key=f"regen:{job_id}:{job['edits_version']}")
    return {"queued": True, "edits_version": job["edits_version"]}


def preview_manifest_for(job_id: str, kind: str, revision: Optional[int], base: str) -> dict:
    if kind == "source":
        job = db.q1("SELECT upload_id FROM jobs WHERE id=?", job_id)
        row = db.q1("SELECT state, manifest FROM source_previews WHERE upload_id=?", job["upload_id"])
        if row and row["state"] == "client":
            return {"state": "client", "kind": "source", "file": f"{base}/file?kind=source"}
        if not row or row["state"] != "ready":
            return {"state": row["state"] if row else "missing", "pages": []}
        man = db.jload(row["manifest"])
        rev_label = "source"
    else:
        if revision is None:
            r = db.q1("SELECT ready_revision FROM jobs WHERE id=?", job_id)
            revision = r["ready_revision"] if r else None
        row = db.q1("SELECT preview_state, preview_manifest, revision FROM output_revisions WHERE job_id=? AND revision=?",
                    job_id, revision)
        if not row or row["preview_state"] != "ready":
            return {"state": row["preview_state"] if row else "missing", "pages": []}
        man = db.jload(row["preview_manifest"])
        if man.get("client"):
            return {"state": "client", "kind": "output", "revision": revision, "labels": man.get("labels", {}),
                    "file": f"{base}/file?kind=output&revision={revision}"}
        rev_label = str(revision)
    pages = [{"n": p["n"], "w": p["w"], "h": p["h"],
              "src": f"{base}/pages/{kind}/{rev_label}/{p['n']}", "thumb": f"{base}/pages/{kind}/{rev_label}/{p['n']}?thumb=1"}
             for p in man["pages"]]
    return {"state": "ready", "kind": kind, "revision": revision, "page_count": man["page_count"], "pages": pages,
            "highlights": man.get("highlights", {}), "texts": man.get("texts", []),
            "missing_highlights": man.get("missing_highlights", [])}


@router.get("/jobs/{job_id}/preview")
def get_preview(job_id: str, request: Request, kind: str = "output", revision: Optional[int] = None) -> dict:
    guest_job(request, job_id)
    if kind not in ("output", "source"):
        raise ApiError(400, "bad_kind", "Unknown preview kind.")
    return preview_manifest_for(job_id, kind, revision, f"/api/jobs/{job_id}")


def page_object(job_id: str, kind: str, rev: str, n: int, thumb: bool) -> bytes:
    if kind == "source":
        job = db.q1("SELECT upload_id FROM jobs WHERE id=?", job_id)
        row = db.q1("SELECT manifest, state FROM source_previews WHERE upload_id=?", job["upload_id"])
        man = db.jload(row["manifest"]) if row and row["state"] == "ready" else None
    else:
        if not rev.isdigit():
            raise not_found()
        row = db.q1("SELECT preview_manifest, preview_state FROM output_revisions WHERE job_id=? AND revision=?",
                    job_id, int(rev))
        man = db.jload(row["preview_manifest"]) if row and row["preview_state"] == "ready" else None
    if not man:
        raise not_found()
    page = next((p for p in man["pages"] if p["n"] == n), None)
    if page is None:
        raise not_found()
    try:
        return store().get(page["thumb"] if thumb else page["obj"])
    except FileNotFoundError:
        raise not_found()


@router.get("/jobs/{job_id}/pages/{kind}/{rev}/{n}")
def get_page(job_id: str, kind: str, rev: str, n: int, request: Request, thumb: int = 0) -> RawResponse:
    guest_job(request, job_id)
    if kind not in ("output", "source"):
        raise not_found()
    return RawResponse(page_object(job_id, kind, rev, n, bool(thumb)), media_type="image/png",
                       headers={"Cache-Control": "private, max-age=3600"})


def file_bytes(job_id: str, kind: str, revision: Optional[int]) -> bytes:
    if kind == "source":
        row = db.q1("SELECT u.object_key FROM jobs j JOIN uploads u ON u.id=j.upload_id WHERE j.id=?", job_id)
        key = row["object_key"] if row else None
    else:
        if revision is None:
            r = db.q1("SELECT ready_revision FROM jobs WHERE id=?", job_id)
            revision = r["ready_revision"] if r else None
        row = db.q1("SELECT docx_key FROM output_revisions WHERE job_id=? AND revision=?", job_id, revision)
        key = row["docx_key"] if row else None
    if not key:
        raise not_found()
    return store().get(key)


@router.get("/jobs/{job_id}/file")
def job_file(job_id: str, request: Request, kind: str = "output", revision: Optional[int] = None,
             chunk: Optional[int] = None) -> RawResponse:
    """Word bytes for the in-browser preview (serverless mode)."""
    guest_job(request, job_id)
    if kind not in ("source", "output"):
        raise not_found()
    return chunked_or_full(file_bytes(job_id, kind, revision), chunk if chunk is not None else 0, DOCX_MEDIA)


@router.post("/jobs/{job_id}/process")
def process_now(job_id: str, request: Request) -> dict:
    """Serverless hosts have no background worker: the browser asks the server to run this job's queued work."""
    check_csrf(request)
    guest_job(request, job_id)
    limit("process", 300, 3600, request)
    from ..pipeline import run_job_tasks
    import os
    n = run_job_tasks(job_id, max_seconds=40 if os.environ.get("VERCEL") else 240)
    return {"ran": n, "state": db.q1("SELECT state FROM jobs WHERE id=?", job_id)["state"]}


@router.get("/jobs/{job_id}/revisions")
def revisions(job_id: str, request: Request) -> dict:
    guest_job(request, job_id)
    rows = db.q("SELECT revision, edits_version, created_at, preview_state, byte_size, page_count FROM output_revisions "
                "WHERE job_id=? ORDER BY revision DESC", job_id)
    return {"revisions": [dict(r) for r in rows]}


DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_response(data: bytes, filename: str) -> RawResponse:
    from urllib.parse import quote
    ascii_name = filename.encode("ascii", "ignore").decode() or "document_AutoFigure.docx"
    ascii_name = ascii_name.replace('"', "").replace("\r", "").replace("\n", "")
    return RawResponse(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                       headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
                                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/jobs/{job_id}/download")
def download(job_id: str, request: Request, revision: Optional[int] = None, chunk: Optional[int] = None) -> RawResponse:
    job = guest_job(request, job_id)
    limit("download", 120, 3600, request)
    rev = revision or job["ready_revision"]
    if not rev:
        raise ApiError(409, "not_ready", "There is no verified Word file yet.")
    row = db.q1("SELECT docx_key, sha256, preview_state FROM output_revisions WHERE job_id=? AND revision=?", job_id, rev)
    if not row or not row["docx_key"] or row["preview_state"] != "ready":
        raise ApiError(409, "not_ready", "This revision is not verified and rendered yet.")
    data = store().get(row["docx_key"])
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ApiError(500, "integrity_error", "The stored file failed its integrity check.")
    return chunked_or_full(data, chunk, DOCX_MEDIA, output_filename(job["original_name"]))


@router.delete("/uploads/{upload_id}")
def delete_upload(upload_id: str, request: Request) -> dict:
    check_csrf(request)
    gid = guest_session(request)
    row = db.q1("SELECT guest_session_id, deletion_state FROM uploads WHERE id=?", upload_id)
    if not gid or row is None or row["guest_session_id"] != gid or row["deletion_state"] != "active":
        raise not_found()
    n = delete_upload_content(upload_id, "user", "user_deleted")
    audit("user", "upload_deleted_by_user", upload_id, {"objects_removed": n}, client_ip(request))
    return {"deleted": True, "objects_removed": n}
