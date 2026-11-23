"""Job state machine and worker task handlers (directives 25, 61, 77)."""
from __future__ import annotations

import hashlib
import re
import logging
import time
from typing import Optional

from . import db, queue
from .ai.captioner import CaptionContext, get_captioner, suggest_with_fallback
from .config import get_config
from .engine.model import FigureOccurrence
from .engine.process import (analyze, build_revision, figure_from_dict, media_for_ai, media_thumbnail, plan,
                             render_preview)
from .engine.render import RenderError, rasterize
from .engine.settings import CaptionSettings
from .engine.validation import Limits, validate_docx
from .engine.verify import OutputInvalid
from .security import audit
from .storage import store

log = logging.getLogger("autofigure.pipeline")

PIPELINE = [
    "uploaded", "validating", "structure_analysis", "page_mapping", "figure_detection", "chapter_mapping",
    "caption_generation", "ooxml_mutation", "document_validation", "preview_rendering", "review_ready",
]
TERMINAL = {"failed", "canceled", "expired", "deleted"}
USER_STEPS = [
    ("Checking Word file", {"uploaded", "validating"}),
    ("Analyzing document structure", {"structure_analysis"}),
    ("Finding eligible figures", {"page_mapping", "figure_detection", "chapter_mapping"}),
    ("Understanding images", {"caption_generation"}),
    ("Inserting captions", {"ooxml_mutation", "document_validation"}),
    ("Rendering preview", {"preview_rendering"}),
    ("Ready for review", {"review_ready"}),
]

FAILURE_MESSAGES = {
    "renderer_not_installed": ("The document renderer is not installed on this server, so pages cannot be verified.",
                               "Ask the operator to install LibreOffice; your original file is unchanged."),
    "render_failed": ("The renderer could not open this Word file.",
                      "Open it in Word, use File → Save As .docx, and upload the new copy."),
    "render_timeout": ("Rendering took longer than the processing limit.",
                       "Try a smaller document (fewer pages or lower-resolution images)."),
    "figure_identity_mismatch": ("The document changed unexpectedly between analysis and writing.",
                                 "Start a new job from the original upload."),
    "output_invalid": ("The generated Word file failed integrity checks, so it was not published.",
                       "Your original is unchanged. Try again or report the support ID."),
    "too_many_figures": ("This document has more images than the configured limit.",
                         "Split the document into parts and process them separately."),
    "too_many_pages": ("This document has more pages than the configured limit.",
                       "Split the document into parts and process them separately."),
    "validation_failed": ("The file did not pass Word validation.", "Upload a genuine .docx file."),
    "internal_error": ("An unexpected processing error occurred.", "Try again; your original is unchanged."),
    "stalled": ("Processing stopped responding.", "Try again; your original is unchanged."),
}


class JobError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def set_state(job_id: str, state: str, detail: Optional[str] = None) -> None:
    idx = PIPELINE.index(state) if state in PIPELINE else None
    with db.tx() as c:
        if idx is not None:
            c.execute("UPDATE jobs SET state=?, stage_index=?, updated_at=? WHERE id=?", (state, idx, db.now(), job_id))
        else:
            c.execute("UPDATE jobs SET state=?, updated_at=? WHERE id=?", (state, db.now(), job_id))
        c.execute("INSERT INTO job_events(job_id, state, at, detail) VALUES (?,?,?,?)", (job_id, state, db.now(), detail))


def job_settings(job: dict) -> CaptionSettings:
    return CaptionSettings.model_validate(db.jload(job["settings"], {}))


def load_figures(job_id: str) -> list[FigureOccurrence]:
    rows = db.q("SELECT data FROM figures WHERE job_id=? ORDER BY logical_order", job_id)
    return [figure_from_dict(db.jload(r["data"])) for r in rows]


def save_figures(job_id: str, figures: list[FigureOccurrence], thumbs: Optional[dict[str, str]] = None) -> None:
    with db.tx() as c:
        for f in figures:
            data = db.jdump(f.to_dict())
            existing = c.execute("SELECT thumb_key FROM figures WHERE job_id=? AND id=?", (job_id, f.figure_id)).fetchone()
            tk = (thumbs or {}).get(f.figure_id) if existing is None else existing["thumb_key"]
            c.execute(
                "INSERT INTO figures(id, job_id, logical_order, source_page_number, chapter_index, figure_index, status, "
                "caption_source, caption_text, confidence_band, needs_review, thumb_key, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(job_id, id) DO UPDATE SET source_page_number=excluded.source_page_number, "
                "chapter_index=excluded.chapter_index, figure_index=excluded.figure_index, status=excluded.status, "
                "caption_source=excluded.caption_source, caption_text=excluded.caption_text, "
                "confidence_band=excluded.confidence_band, needs_review=excluded.needs_review, data=excluded.data",
                (f.figure_id, job_id, f.order, f.page, f.chapter_number, f.figure_index, f.status, f.caption_source,
                 f.caption_text, f.confidence_band, int(f.needs_review), tk, data))


def _context(f: FigureOccurrence, settings: CaptionSettings, language_hint: str, variant: Optional[str] = None) -> CaptionContext:
    lang = settings.language if settings.language != "auto" else language_hint
    return CaptionContext(f.figure_id, f.chapter_title or "", f.nearby_text, f.alt_text, lang, settings.tone,
                          settings.length, variant, f.preceding_text, f.own_text)


def uses_models(captioner) -> bool:
    return bool(getattr(captioner, "uses_external", captioner.name in ("anthropic", "openai")))


def _caption_candidates(figures: list[FigureOccurrence]) -> list[FigureOccurrence]:
    want = {"insert", "replace", "excluded_page_uncertain", "excluded_unassigned"}
    return [f for f in figures if f.status in want and not f.caption_text]


def generate_captions(original: bytes, figures: list[FigureOccurrence], settings: CaptionSettings,
                      language_hint: str, budget: int) -> int:
    cfg = get_config()
    captioner = get_captioner(cfg.ai_provider, cfg.ai_model)
    calls = 0
    for f in _caption_candidates(figures):
        if f.existing and f.status == "renumber":
            continue
        img = media_for_ai(original, f.media_part)
        use = captioner if calls < budget else get_captioner("none", "")
        s = suggest_with_fallback(use, img, _context(f, settings, language_hint))
        if use is captioner and uses_models(captioner):
            calls += 1
        f.caption_text, f.caption_source = s.caption, s.source
        f.confidence_band, f.needs_review, f.category = s.confidence_band, s.needs_review, s.category
        f.alternatives, f.engine = list(s.alternatives), s.engine
        if s.uncertain_details:
            f.warnings = [w for w in f.warnings if not w.startswith("ai:")] + [f"ai:{d}" for d in s.uncertain_details[:4]]
    for f in figures:
        if f.status in ("renumber", "preserve", "skip_existing") and f.existing and not f.caption_text:
            f.caption_text, f.caption_source = f.existing.description, "existing"
            f.confidence_band, f.needs_review = "high", False
    calls += detect_mistakes(original, figures, settings, language_hint, captioner, max(0, budget - calls))
    return calls


TYPE_WORDS = {
    "chart": r"\b(chart|graph|plot|histogram)\b", "screenshot": r"\b(screenshot|screen ?shot|window|interface page)\b",
    "photograph": r"\b(photo|photograph|picture of)\b", "network_diagram": r"\b(topology|network diagram)\b",
    "cli": r"\b(output|command|terminal|console)\b",
}
COMPATIBLE = {"screenshot": {"cli", "screenshot", "table"}, "cli": {"cli", "screenshot"}, "chart": {"chart"},
              "network_diagram": {"network_diagram"}, "photograph": {"photograph"}}


def offline_type_clash(original: bytes, f: FigureOccurrence, lines=None) -> Optional[tuple[str, str]]:
    """Without a vision model we can still catch a caption that names the wrong kind of image
    (e.g. "chart" under a network topology). Content mistakes inside photos need Claude/ChatGPT."""
    from .ai.local_vision import analyse
    desc = f.existing.description.lower()
    said = [k for k, rx in TYPE_WORDS.items() if re.search(rx, desc)]
    if not said:
        return None
    vis = analyse(media_for_ai(original, f.media_part), lines=lines)
    if vis.confidence != "medium" or not vis.caption:
        return None
    if any(vis.kind in COMPATIBLE.get(k, {k}) for k in said):
        return None
    names = {"cli": "command-line output", "network_diagram": "network diagram", "chart": "chart",
             "screenshot": "screenshot", "table": "table", "photograph": "photograph"}
    return (f"The caption describes a {names.get(said[0], said[0])}, but the image looks like a {names.get(vis.kind, vis.kind)}",
            vis.caption)


def detect_mistakes(original: bytes, figures: list[FigureOccurrence], settings: CaptionSettings, language_hint: str,
                    captioner, budget: int) -> int:
    """Check the author's existing captions: wrong number, wrong label format, duplicate labels and — when a
    vision model is configured — a description that does not match what the image shows (e.g. a cat captioned "dog")."""
    from .ai.autofigure_ai import agreement
    calls = 0
    seen: dict[str, str] = {}
    can_see = uses_models(captioner)
    for f in sorted(figures, key=lambda x: x.order):
        if not f.existing or not f.eligible:
            continue
        if f.status == "replace":  # being rewritten (corrected by the user or "replace" mode): nothing left to flag
            f.mistakes, f.correction = [], f.correction
            continue
        found = f.existing.text
        problems = []
        expected = f"{settings.prefix} {f.label}"
        if (f.existing.label_number or "") != (f.label or ""):
            problems.append({"kind": "number", "message": f"Numbered “{f.existing.label_number or 'none'}” but this figure is {expected}",
                             "found": found})
        elif not found.lower().startswith(settings.prefix.lower()):
            problems.append({"kind": "format", "message": f"Label should read “{expected}”", "found": found})
        if f.existing.label_number:
            if f.existing.label_number in seen:
                problems.append({"kind": "duplicate", "message": f"Number {f.existing.label_number} is used by more than one figure",
                                 "found": found})
            seen[f.existing.label_number] = f.figure_id
        already = "checked_existing" in f.warnings
        if can_see and calls < budget and f.existing.description and not already:
            f.warnings.append("checked_existing")
            img = media_for_ai(original, f.media_part)
            s = suggest_with_fallback(captioner, img, _context(f, settings, language_hint))
            calls += 1
            if s.source == "ai" and s.confidence_band in ("high", "medium") and agreement(s.caption, f.existing.description) < 0.15:
                problems.append({"kind": "description",
                                 "message": "The caption text does not seem to match what the image shows",
                                 "found": found, "suggested": s.caption})
                f.correction = s.caption
        if not can_see and f.existing.description and not any(m["kind"] == "description" for m in problems):
            clash = offline_type_clash(original, f)
            if clash:
                problems.append({"kind": "description", "message": clash[0], "found": found, "suggested": clash[1]})
                f.correction = clash[1]
        previous = [m for m in f.mistakes if m.get("kind") == "description"]
        if not any(m["kind"] == "description" for m in problems) and previous and "dismissed_correction" not in f.warnings:
            problems += previous
        f.mistakes = problems
    return calls


def _store_preview(upload_id: str, pv_pages: list[bytes], pv_thumbs: list[bytes], prefix_kind: str) -> list[dict]:
    st = store()
    out = []
    for i, (pg, th) in enumerate(zip(pv_pages, pv_thumbs), start=1):
        out.append({"n": i, "page": st.put(pg, prefix_kind + "pg", upload_id), "thumb": st.put(th, prefix_kind + "th", upload_id)})
    return out


def run_analyze(job_id: str) -> None:
    cfg = get_config()
    job = dict(db.q1("SELECT * FROM jobs WHERE id=?", job_id))
    if job["state"] in TERMINAL:
        return
    up = db.q1("SELECT * FROM uploads WHERE id=?", job["upload_id"])
    if up is None or up["deletion_state"] != "active":
        raise JobError("deleted")
    settings = job_settings(job)
    svc = db.service_settings()
    db.ex("UPDATE jobs SET started_at=COALESCE(started_at, ?) WHERE id=?", db.now(), job_id)
    set_state(job_id, "validating")
    original = store().get(up["object_key"])
    if hashlib.sha256(original).hexdigest() != up["sha256"]:
        raise JobError("checksum_mismatch")
    v = validate_docx(up["original_name"], original, Limits(max_bytes=svc["max_upload_mb"] * 1024 * 1024))
    if not v.ok:
        raise JobError("validation_failed")
    set_state(job_id, "structure_analysis")
    set_state(job_id, "page_mapping")
    try:
        ar = analyze(original, cfg.render_timeout, render=not cfg.lite)
    except RenderError as e:
        raise JobError(str(e), retryable=str(e) == "render_timeout") from e
    a = ar.analysis
    if len(a.figures) > svc["max_figures"]:
        raise JobError("too_many_figures")
    if a.page_count and a.page_count > svc["max_pages"]:
        raise JobError("too_many_pages")
    # Source preview from the untouched original render (used for compare + owner viewer).
    if cfg.lite:
        db.ex("INSERT INTO source_previews(upload_id, state, manifest, created_at) VALUES (?,?,?,?) ON CONFLICT(upload_id) "
              "DO UPDATE SET state=excluded.state, manifest=excluded.manifest, created_at=excluded.created_at",
              up["id"], "client", None, db.now())
    elif ar.original_pdf:
        try:
            pages, thumbs = rasterize(ar.original_pdf)
            stored = _store_preview(up["id"], pages, thumbs, "s")
            from .engine.render import pdf_layout
            layout = pdf_layout(ar.original_pdf)
            man = {"page_count": len(layout), "pages": [{"n": p.page, "w": p.width, "h": p.height,
                                                          "obj": s["page"], "thumb": s["thumb"]}
                                                         for p, s in zip(layout, stored)]}
            db.ex("INSERT INTO source_previews(upload_id, state, manifest, created_at) VALUES (?,?,?,?) ON CONFLICT(upload_id) "
                  "DO UPDATE SET state=excluded.state, manifest=excluded.manifest, created_at=excluded.created_at",
                  up["id"], "ready", db.jdump(man), db.now())
        except RenderError:
            db.ex("INSERT INTO source_previews(upload_id, state, manifest, created_at) VALUES (?,?,?,?) ON CONFLICT(upload_id) "
                  "DO UPDATE SET state=excluded.state, manifest=excluded.manifest, created_at=excluded.created_at",
                  up["id"], "failed", None, db.now())
    set_state(job_id, "figure_detection", f"{len(a.figures)} visual objects")
    thumbs: dict[str, str] = {}
    for f in a.figures:
        t = media_thumbnail(original, f.media_part)
        if t:
            thumbs[f.figure_id] = store().put(t, "fthm", up["id"])
    set_state(job_id, "chapter_mapping")
    summary = plan(a.figures, a.headings, settings)
    summary["page_method"] = ar.page_method
    summary["analysis_warnings"] = ar.warnings
    analysis_meta = {"headings": [h.to_dict() for h in a.headings], "page_count": a.page_count,
                     "language_hint": a.language_hint, "unsupported": a.unsupported,
                     "has_heading1": a.has_heading1, "has_lo": a.has_lo}
    db.ex("UPDATE jobs SET analysis=?, summary=? WHERE id=?", db.jdump(analysis_meta), db.jdump(summary), job_id)
    db.ex("UPDATE uploads SET page_count=?, figure_count=? WHERE id=?", a.page_count, len(a.figures), up["id"])
    set_state(job_id, "caption_generation")
    calls = generate_captions(original, a.figures, settings, a.language_hint, cfg.ai_max_figures)
    db.ex("UPDATE jobs SET ai_calls=ai_calls+? WHERE id=?", calls, job_id)
    save_figures(job_id, a.figures, thumbs)
    _build_and_render(job_id, up, original, a.figures, settings, summary, first=True)


def _build_and_render(job_id: str, up, original: bytes, figures: list[FigureOccurrence], settings: CaptionSettings,
                      summary: dict, first: bool, edits_version: Optional[int] = None) -> None:
    cfg = get_config()
    set_state(job_id, "ooxml_mutation") if first else None
    if summary.get("invariant_problems"):
        raise JobError("numbering_invariant")
    try:
        rev = build_revision(original, figures, settings)
    except OutputInvalid as e:
        log.error("output_invalid job=%s detail=%s", job_id, str(e)[:200])
        raise JobError("output_invalid") from e
    except RuntimeError as e:
        raise JobError(str(e) if str(e) in FAILURE_MESSAGES else "internal_error") from e
    if first:
        set_state(job_id, "document_validation", f"{rev.verification['verified_captions']} captions verified")
    job = db.q1("SELECT current_revision, edits_version FROM jobs WHERE id=?", job_id)
    revision = job["current_revision"] + 1
    ev = job["edits_version"] if edits_version is None else edits_version
    key = store().put(rev.docx, "out", up["id"])
    db.ex("INSERT INTO output_revisions(job_id, revision, edits_version, docx_key, sha256, byte_size, manifest, "
          "verification, preview_state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
          job_id, revision, ev, key, hashlib.sha256(rev.docx).hexdigest(), len(rev.docx), db.jdump(rev.manifest),
          db.jdump(rev.verification), "pending", db.now())
    db.ex("UPDATE jobs SET current_revision=? WHERE id=?", revision, job_id)
    if first:
        set_state(job_id, "preview_rendering")
    labels = {f.figure_id: f.label for f in figures if f.eligible and f.label}
    if cfg.lite:
        # serverless: no renderer — the browser draws the exact .docx revision itself
        db.ex("UPDATE output_revisions SET preview_state='ready', preview_manifest=? WHERE job_id=? AND revision=?",
              db.jdump({"client": True, "labels": labels}), job_id, revision)
        db.ex("UPDATE jobs SET ready_revision=?, rendered_edits_version=?, summary=?, finished_at=? WHERE id=?",
              revision, ev, db.jdump(summary), db.now(), job_id)
        set_state(job_id, "review_ready", f"revision {revision}")
        return
    try:
        pv = render_preview(rev.docx, labels, settings.prefix, cfg.render_timeout)
    except RenderError as e:
        db.ex("UPDATE output_revisions SET preview_state='failed' WHERE job_id=? AND revision=?", job_id, revision)
        raise JobError(str(e), retryable=True) from e
    stored = _store_preview(up["id"], pv.pages, pv.thumbs, "o")
    pm = dict(pv.manifest)
    pm["pages"] = [dict(p, obj=s["page"], thumb=s["thumb"]) for p, s in zip(pm["pages"], stored)]
    pm["missing_highlights"] = sorted(set(labels) - set(pm["highlights"]))
    db.ex("UPDATE output_revisions SET preview_state='ready', preview_manifest=?, page_count=? WHERE job_id=? AND revision=?",
          db.jdump(pm), pv.manifest["page_count"], job_id, revision)
    db.ex("UPDATE jobs SET ready_revision=?, rendered_edits_version=?, summary=?, finished_at=? WHERE id=?",
          revision, ev, db.jdump(summary), db.now(), job_id)
    set_state(job_id, "review_ready", f"revision {revision}")


def run_regenerate(job_id: str, edits_version: int) -> None:
    job = dict(db.q1("SELECT * FROM jobs WHERE id=?", job_id))
    if job["state"] in TERMINAL:
        return
    if (job["rendered_edits_version"] or -1) >= edits_version:
        return  # a newer regeneration already covered these edits
    up = db.q1("SELECT * FROM uploads WHERE id=?", job["upload_id"])
    if up is None or up["deletion_state"] != "active":
        raise JobError("deleted")
    settings = job_settings(job)
    set_state(job_id, "regenerating", f"edits {edits_version}")
    original = store().get(up["object_key"])
    figures = load_figures(job_id)
    meta = db.jload(job["analysis"], {})
    from .engine.model import Heading
    headings = [Heading(**h) for h in meta.get("headings", [])]
    summary = plan(figures, headings, settings)
    old = db.jload(job["summary"], {})
    summary["page_method"] = old.get("page_method")
    summary["analysis_warnings"] = old.get("analysis_warnings", [])
    cfg = get_config()
    svc = db.service_settings()
    remaining = max(0, svc["max_ai_suggestions_per_job"] - job["ai_calls"])
    calls = generate_captions(original, figures, settings, meta.get("language_hint", "en"), min(remaining, cfg.ai_max_figures))
    db.ex("UPDATE jobs SET ai_calls=ai_calls+? WHERE id=?", calls, job_id)
    save_figures(job_id, figures)
    _build_and_render(job_id, up, original, figures, settings, summary, first=False, edits_version=edits_version)


def handle_task(task: dict) -> None:
    kind, payload = task["kind"], task["payload"]
    job_id = payload.get("job_id")
    try:
        if kind == "analyze":
            run_analyze(job_id)
        elif kind == "regenerate":
            run_regenerate(job_id, int(payload["edits_version"]))
        elif kind == "retention":
            retention_sweep()
        queue.complete(task["id"])
    except JobError as e:
        dead = queue.fail(task, e.code, e.retryable)
        if job_id and (dead or not e.retryable):
            _mark_failed(job_id, e.code, kind)
    except Exception as e:  # never leak document content into logs
        log.exception("task_failed kind=%s job=%s type=%s", kind, job_id, type(e).__name__)
        dead = queue.fail(task, "internal_error", True)
        if job_id and dead:
            _mark_failed(job_id, "internal_error", kind)


def _mark_failed(job_id: str, code: str, kind: str) -> None:
    job = db.q1("SELECT state, ready_revision FROM jobs WHERE id=?", job_id)
    if job is None or job["state"] in ("deleted", "expired"):
        return
    if kind == "regenerate" and job["ready_revision"]:
        # keep the last known-good revision downloadable; report the failed regeneration
        db.ex("UPDATE jobs SET failure_code=?, failure_detail=? WHERE id=?", code, "regeneration_failed", job_id)
        set_state(job_id, "review_ready", f"regeneration failed: {code}")
        return
    db.ex("UPDATE jobs SET failure_code=?, finished_at=? WHERE id=?", code, db.now(), job_id)
    set_state(job_id, "failed", code)


def delete_upload_content(upload_id: str, actor: str, state: str) -> int:
    n = store().delete_upload(upload_id)
    with db.tx() as c:
        c.execute("UPDATE uploads SET deletion_state=?, deleted_at=?, object_key=NULL WHERE id=?", (state, db.now(), upload_id))
        for j in c.execute("SELECT id FROM jobs WHERE upload_id=?", (upload_id,)).fetchall():
            c.execute("UPDATE jobs SET state=?, updated_at=? WHERE id=?",
                      ("expired" if state == "expired" else "deleted", db.now(), j["id"]))
            c.execute("UPDATE output_revisions SET preview_state='deleted', docx_key=NULL, preview_manifest=NULL WHERE job_id=?", (j["id"],))
            for fr in c.execute("SELECT id, data FROM figures WHERE job_id=?", (j["id"],)).fetchall():
                d = db.jload(fr["data"], {})
                d.update({"caption_text": "", "nearby_text": "", "preceding_text": "", "own_text": "", "alt_text": None,
                          "correction": "", "alternatives": [], "mistakes": []})
                c.execute("UPDATE figures SET thumb_key=NULL, caption_text=NULL, data=? WHERE job_id=? AND id=?",
                          (db.jdump(d), j["id"], fr["id"]))
            c.execute("DELETE FROM caption_revisions WHERE job_id=?", (j["id"],))
        c.execute("UPDATE source_previews SET state='deleted', manifest=NULL WHERE upload_id=?", (upload_id,))
    return n


def retention_sweep() -> int:
    rows = db.q("SELECT id FROM uploads WHERE deletion_state='active' AND retention_expires_at < ?", db.now())
    for r in rows:
        n = delete_upload_content(r["id"], "system", "expired")
        audit("system", "retention_expired", r["id"], {"objects_removed": n})
    stale = "SELECT id FROM guest_sessions WHERE expires_at < ? AND id NOT IN (SELECT guest_session_id FROM uploads)"
    db.ex(f"DELETE FROM guest_tokens WHERE guest_session_id IN ({stale})", db.now())
    db.ex(f"DELETE FROM session_transfers WHERE guest_session_id IN ({stale})", db.now())
    db.ex("DELETE FROM session_transfers WHERE expires_at < ?", db.now() - 86400)
    db.ex(f"DELETE FROM guest_sessions WHERE id IN ({stale})", db.now())
    db.ex("DELETE FROM owner_sessions WHERE expires_at < ?", db.now() - 86400 * 7)
    db.ex("DELETE FROM upload_chunks WHERE created_at < ?", db.now() - 86400)
    return len(rows)


def recover_stalled() -> None:
    """Jobs whose queue task died or vanished must not show 'processing' forever."""
    cutoff = db.now() - 3600
    for j in db.q("SELECT id FROM jobs WHERE state NOT IN ('review_ready','failed','canceled','expired','deleted') "
                  "AND updated_at < ?", cutoff):
        pending = db.q1("SELECT 1 FROM queue WHERE state IN ('ready','leased') AND payload LIKE ?", f'%"{j["id"]}"%')
        if not pending:
            _mark_failed(j["id"], "stalled", "analyze")


GENERIC_CAPTIONS = {"Photograph", "Screenshot", "Diagram", "Chart", "Chart or diagram", "Illustration", "Image", "Table",
                    "Logo", "Command-line output"}
_RANK = {"verified": 4, "high": 3, "medium": 2, "low": 1, "": 0}


def apply_browser_ocr(job_id: str, results: list[dict], failed: bool = False) -> dict:
    """Serverless hosts cannot ship an OCR engine; the visitor's browser reads the text in each figure instead
    (Tesseract, locally) and sends only the recognised lines. They go through the same caption rules as server OCR."""
    from .ai.autofigure_ai import AutoFigureAI
    from .ai.local_vision import lines_from_client
    job = dict(db.q1("SELECT * FROM jobs WHERE id=?", job_id))
    up = db.q1("SELECT * FROM uploads WHERE id=?", job["upload_id"])
    meta = db.jload(job["analysis"], {}) or {}
    if meta.get("browser_ocr"):
        return {"updated": 0, "mistakes": 0, "regenerating": False}
    settings = job_settings(job)
    figures = load_figures(job_id)
    by_id = {r["figure_id"]: lines_from_client(r.get("lines") or []) for r in results if r.get("figure_id")}
    original = store().get(up["object_key"]) if by_id else b""
    ai = AutoFigureAI(external=[])
    updated, new_mistakes, touched = 0, 0, []
    for f in figures:
        lines = by_id.get(f.figure_id)
        if lines is None or not lines:
            continue
        img = media_for_ai(original, f.media_part)
        if f.existing and f.eligible and "dismissed_correction" not in f.warnings and \
                not any(m.get("kind") == "description" for m in f.mistakes) and f.status != "replace":
            clash = offline_type_clash(original, f, lines=lines)
            if clash:
                f.mistakes = f.mistakes + [{"kind": "description", "message": clash[0], "found": f.existing.text,
                                            "suggested": clash[1]}]
                f.correction = clash[1]
                new_mistakes += 1
                touched.append(f)
            continue
        if f.caption_source in ("manual", "corrected", "existing") or f.existing:
            continue
        if f.status not in ("insert", "replace", "excluded_page_uncertain", "excluded_unassigned"):
            continue
        s = ai.suggest_with_text(img, _context(f, settings, meta.get("language_hint", "en")), lines)
        old_generic = f.caption_text in GENERIC_CAPTIONS or not f.caption_text
        new_generic = s.caption in GENERIC_CAPTIONS
        if s.caption and s.caption != f.caption_text and not new_generic and \
                (_RANK.get(s.confidence_band, 0) > _RANK.get(f.confidence_band, 0) or old_generic):
            f.caption_text, f.caption_source, f.confidence_band = s.caption, "context", s.confidence_band
            f.needs_review, f.category, f.engine = s.needs_review, s.category, s.engine + " · text read in your browser"
            f.alternatives = [a for a in s.alternatives if a != s.caption]
            updated += 1
            touched.append(f)
    if touched:
        save_figures(job_id, touched)
    meta["browser_ocr"] = {"at": db.now(), "figures": len(by_id), "updated": updated, "failed": bool(failed)}
    db.ex("UPDATE jobs SET analysis=? WHERE id=?", db.jdump(meta), job_id)
    regenerating = False
    if updated and any(f.eligible for f in touched):
        with db.tx() as c:
            c.execute("UPDATE jobs SET edits_version=edits_version+1, updated_at=? WHERE id=?", (db.now(), job_id))
            ev = c.execute("SELECT edits_version FROM jobs WHERE id=?", (job_id,)).fetchone()["edits_version"]
        queue.enqueue("regenerate", {"job_id": job_id, "edits_version": ev}, dedupe_key=f"regen:{job_id}:{ev}")
        regenerating = True
    return {"updated": updated, "mistakes": new_mistakes, "regenerating": regenerating}


def run_job_tasks(job_id: str, max_seconds: float = 240) -> int:
    """Serverless mode: process this job's pending tasks inside the current request."""
    deadline = time.time() + max_seconds
    done = 0
    while time.time() < deadline:
        task = queue.lease(job_id=job_id)
        if task is None:
            break
        handle_task(task)
        done += 1
    return done


def worker_loop(stop_after_idle: Optional[float] = None, poll: float = 0.5) -> None:
    last_sweep = 0.0
    idle_since = time.time()
    while True:
        if time.time() - last_sweep > 300:
            try:
                retention_sweep()
                recover_stalled()
            except Exception:
                log.exception("sweep_failed")
            last_sweep = time.time()
        task = queue.lease()
        if task is None:
            if stop_after_idle is not None and time.time() - idle_since > stop_after_idle:
                return
            time.sleep(poll)
            continue
        handle_task(task)
        idle_since = time.time()
