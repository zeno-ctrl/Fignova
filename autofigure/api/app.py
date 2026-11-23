"""FastAPI application: middleware, security headers, page routes, error handling."""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import db
from ..config import get_config
from . import owner, public
from .common import ApiError, ensure_csrf_cookie, error_response, owner_session

WEB = Path(__file__).resolve().parents[2] / "web"
log = logging.getLogger("autofigure.http")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")

env = Environment(loader=FileSystemLoader(str(WEB / "templates")), autoescape=select_autoescape(["html"]))


def create_app() -> FastAPI:
    cfg = get_config()
    db.migrate()
    app = FastAPI(title="AutoFigure Studio", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
    app.include_router(public.router)
    app.include_router(owner.router)

    @app.middleware("http")
    async def security_mw(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex[:12]
        start = time.time()
        path = request.url.path
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and not path.startswith("/static/"):
            try:
                max_body = (db.service_settings()["max_upload_mb"] + 2) * 1024 * 1024
            except Exception:
                max_body = 52 * 1024 * 1024
            if int(cl) > max_body:
                return error_response(request, 413, "too_large", "The upload is larger than the allowed limit.")
        try:
            response: Response = await call_next(request)
        except Exception as exc:  # never fall through to a bare "Internal Server Error"
            log.exception("request_failed id=%s type=%s", request.state.request_id, type(exc).__name__)
            if path.startswith("/api"):
                response = error_response(request, 500, "internal_error", "Something went wrong on our side. Please try again.")
            else:
                response = HTMLResponse("<!doctype html><meta charset=utf-8><title>Please try again</title>"
                                        "<body style='font-family:system-ui;max-width:560px;margin:15vh auto;padding:0 16px'>"
                                        "<h1>Please try again</h1><p>The server had a temporary problem. Reload the page in a few seconds.</p>"
                                        "<p><a href='/'>Go home</a></p></body>", status_code=503)
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), interest-cohort=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["X-Request-ID"] = request.state.request_id
        if path.startswith("/static/"):
            h["Cache-Control"] = "public, max-age=3600"
        if cfg.secure_cookies:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if path.startswith("/api") or path.startswith("/owner"):
            h.setdefault("Cache-Control", "no-store")
        if path.startswith("/owner"):
            h["X-Robots-Tag"] = "noindex, nofollow"
        # structured access log without query strings, filenames or document content
        log.info("req id=%s m=%s p=%s s=%s ms=%d", request.state.request_id, request.method,
                 path if not path.startswith("/api/") else "/".join(path.split("/")[:4]), response.status_code,
                 (time.time() - start) * 1000)
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return error_response(request, exc.status_code, exc.detail["code"], exc.detail["message"])

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(request, 422, "invalid_request", "The request was not valid.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api"):
            return error_response(request, exc.status_code, "not_found" if exc.status_code == 404 else "http_error",
                                  "Not found." if exc.status_code == 404 else "Request failed.")
        return render(request, "404.html", {"title": "Page not found"}, status=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled id=%s type=%s", getattr(request.state, "request_id", "?"), type(exc).__name__)
        if request.url.path.startswith("/api"):
            return error_response(request, 500, "internal_error", "Something went wrong on our side.")
        return render(request, "error.html", {"title": "Something went wrong"}, status=500)

    def render(request: Request, template: str, ctx: dict | None = None, status: int = 200) -> HTMLResponse:
        ctx = dict(ctx or {})
        try:
            svc = db.service_settings()
        except Exception:
            svc = {"retention_days": cfg.retention_days, "max_upload_mb": cfg.max_upload_mb, "maintenance_mode": False,
                   "maintenance_message": ""}
        ctx.update({
            "request": request, "owner_name": cfg.owner_display_name, "version": cfg.version,
            "retention_days": svc["retention_days"], "max_upload_mb": svc["max_upload_mb"],
            "ai_providers": public.external_ai_providers(), "support_contact": cfg.support_contact,
            "operator_name": cfg.operator_name, "storage_region": cfg.storage_region, "path": request.url.path,
            "maintenance": svc["maintenance_mode"], "maintenance_message": svc["maintenance_message"],
        })
        resp = HTMLResponse(env.get_template(template).render(**ctx), status_code=status)
        ensure_csrf_cookie(request, resp)
        return resp

    pages = {
        "/": ("index.html", "AutoFigure Studio"),
        "/app": ("app.html", "Workspace"),
        "/features": ("features.html", "Features"),
        "/how-it-works": ("how.html", "How it works"),
        "/about": ("about.html", "About"),
        "/privacy": ("privacy.html", "Privacy"),
        "/terms": ("terms.html", "Terms"),
        "/help": ("help.html", "Help"),
    }

    def page_route(template: str, title: str):
        async def handler(request: Request):
            return render(request, template, {"title": title})
        return handler

    for path, (tpl, title) in pages.items():
        app.add_api_route(path, page_route(tpl, title), methods=["GET"], response_class=HTMLResponse)

    @app.get("/resume", response_class=HTMLResponse)
    async def resume_page(request: Request):
        return render(request, "resume.html", {"title": "Open your documents"})

    @app.get("/app/jobs/{job_id}", response_class=HTMLResponse)
    async def job_page(job_id: str, request: Request):
        return render(request, "review.html", {"title": "Review document", "job_id": job_id})

    # --- owner surface: every page is gated server-side --------------------------------------
    @app.get("/owner/login", response_class=HTMLResponse)
    async def owner_login(request: Request):
        if owner_session(request):
            return RedirectResponse("/owner/dashboard", status_code=303)
        return render(request, "owner/login.html", {"title": "Owner sign-in"})

    @app.get("/owner/setup", response_class=HTMLResponse)
    async def owner_setup(request: Request):
        return render(request, "owner/setup.html", {"title": "Owner setup"})

    @app.get("/owner/enroll", response_class=HTMLResponse)
    async def owner_enroll(request: Request):
        return render(request, "owner/enroll.html", {"title": "Owner enrollment"})

    owner_pages = {
        "dashboard": "Overview", "uploads": "Uploads", "jobs": "Jobs", "settings": "Settings",
        "security": "Security", "audit": "Audit log", "retention": "Retention",
    }

    def owner_page(slug: str, title: str):
        async def handler(request: Request):
            s = owner_session(request)
            if s is None:
                return RedirectResponse("/owner/login", status_code=303)
            return render(request, f"owner/{slug}.html", {"title": title, "owner": s, "section": slug})
        return handler

    for slug, title in owner_pages.items():
        app.add_api_route(f"/owner/{slug}", owner_page(slug, title), methods=["GET"], response_class=HTMLResponse)

    @app.get("/owner", response_class=HTMLResponse)
    async def owner_root(request: Request):
        return RedirectResponse("/owner/dashboard" if owner_session(request) else "/owner/login", status_code=303)

    @app.get("/owner/uploads/{upload_id}", response_class=HTMLResponse)
    async def owner_upload_page(upload_id: str, request: Request):
        s = owner_session(request)
        if s is None:
            return RedirectResponse("/owner/login", status_code=303)
        return render(request, "owner/upload.html", {"title": "Upload details", "owner": s, "section": "uploads",
                                                      "upload_id": upload_id})

    @app.get("/healthz")
    async def healthz():
        from ..engine.render import soffice_path
        db.q1("SELECT 1")
        return {"ok": True, "renderer": bool(soffice_path()), "version": cfg.version}

    @app.get("/api/cron/retention")
    async def cron_retention(request: Request):
        """Vercel Cron entry point (Authorization: Bearer $CRON_SECRET)."""
        import hmac, os
        from ..pipeline import recover_stalled, retention_sweep
        secret = os.environ.get("CRON_SECRET", "")
        if not secret or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}"):
            return error_response(request, 401, "unauthorized", "Unauthorized.")
        n = retention_sweep()
        recover_stalled()
        return {"expired": n}

    @app.get("/robots.txt")
    async def robots():
        return Response("User-agent: *\nDisallow: /owner\nDisallow: /api\nDisallow: /app/jobs\n", media_type="text/plain")

    @app.get("/favicon.ico")
    async def favicon():
        return RedirectResponse("/static/img/favicon.svg", status_code=301)

    return app
