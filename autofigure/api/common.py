"""Shared request helpers: errors, guest capability sessions, CSRF, owner auth."""
from __future__ import annotations

import hmac
import secrets
import uuid
from typing import Optional

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import db
from ..config import get_config
from ..security import ip_hash, rate_limit, security_event, token_hash

GUEST_COOKIE = "afs_guest"
CSRF_COOKIE = "afs_csrf"
OWNER_COOKIE = "afs_owner"
GUEST_TTL = 60 * 60 * 24 * 45
OWNER_IDLE = 30 * 60
OWNER_ABSOLUTE = 12 * 60 * 60
STEP_UP_WINDOW = 10 * 60


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(status_code=status, detail={"code": code, "message": message})


def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    return JSONResponse({"error": {"code": code, "message": message, "request_id": rid}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


NOT_FOUND = ("not_found", "Not found, or you do not have access to it.")


def not_found() -> ApiError:
    # Same answer for "does not exist" and "not yours" — no existence leak (BOLA protection).
    return ApiError(404, *NOT_FOUND)


def client_ip(request: Request) -> str:
    # Behind a trusted proxy/tunnel (e.g. Cloudflare Tunnel with the app bound to 127.0.0.1) the real visitor
    # address arrives in a header. Only honoured when the operator names that header explicitly.
    import os
    # Vercel's edge always sets x-real-ip to the visitor's address (clients cannot spoof it there).
    header = os.environ.get("AUTOFIGURE_CLIENT_IP_HEADER", "x-real-ip" if os.environ.get("VERCEL") else "").strip().lower()
    if header:
        v = request.headers.get(header, "").split(",")[0].strip()
        if v:
            return v[:64]
    return request.client.host if request.client else "?"


def set_cookie(resp: Response, name: str, value: str, max_age: int, httponly: bool = True, samesite: str = "lax",
               path: str = "/") -> None:
    cfg = get_config()
    resp.set_cookie(name, value, max_age=max_age, httponly=httponly, secure=cfg.secure_cookies, samesite=samesite,
                    path=path)


# --- CSRF (double submit + Origin check) ------------------------------------
def ensure_csrf_cookie(request: Request, response: Response) -> str:
    tok = request.cookies.get(CSRF_COOKIE)
    if not tok or len(tok) < 20:
        tok = secrets.token_urlsafe(24)
        set_cookie(response, CSRF_COOKIE, tok, GUEST_TTL, httponly=False, samesite="strict")
    return tok


def check_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        allowed = {get_config().public_base_url, f"http://{host}", f"https://{host}"}
        if origin.rstrip("/") not in allowed:
            raise ApiError(403, "bad_origin", "Cross-origin request blocked.")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not hmac.compare_digest(cookie, header):
        raise ApiError(403, "csrf_failed", "Your session security token is missing. Reload the page and try again.")


# --- guest capability session ---------------------------------------------
def guest_session(request: Request, response: Optional[Response] = None, create: bool = False) -> Optional[str]:
    tok = request.cookies.get(GUEST_COOKIE)
    if tok:
        th = token_hash(tok)
        row = db.q1("SELECT id, expires_at FROM guest_sessions WHERE token_hash=?", th) or \
            db.q1("SELECT g.id, g.expires_at FROM guest_tokens t JOIN guest_sessions g ON g.id=t.guest_session_id "
                  "WHERE t.token_hash=?", th)
        if row and row["expires_at"] > db.now():
            db.ex("UPDATE guest_sessions SET last_seen_at=? WHERE id=?", db.now(), row["id"])
            return row["id"]
    if not create or response is None:
        return None
    tok = secrets.token_urlsafe(32)
    gid = db.new_id("gs")
    db.ex("INSERT INTO guest_sessions(id, token_hash, created_at, last_seen_at, expires_at) VALUES (?,?,?,?,?)",
          gid, token_hash(tok), db.now(), db.now(), db.now() + GUEST_TTL)
    set_cookie(response, GUEST_COOKIE, tok, GUEST_TTL, httponly=True, samesite="lax")
    return gid


def guest_job(request: Request, job_id: str) -> dict:
    gid = guest_session(request)
    if not gid:
        raise not_found()
    row = db.q1("SELECT j.*, u.guest_session_id, u.deletion_state, u.original_name, u.object_key AS source_key, "
                "u.id AS upload_id_, u.retention_expires_at, u.byte_size FROM jobs j JOIN uploads u ON u.id=j.upload_id "
                "WHERE j.id=?", job_id)
    if row is None or row["guest_session_id"] != gid or row["deletion_state"] != "active":
        raise not_found()
    return dict(row)


# --- owner session ----------------------------------------------------------
def owner_session(request: Request) -> Optional[dict]:
    tok = request.cookies.get(OWNER_COOKIE)
    if not tok:
        return None
    row = db.q1("SELECT s.*, o.status AS owner_status, o.principal_id AS owner_principal, o.display_name, o.email "
                "FROM owner_sessions s JOIN owner_accounts o ON o.id=1 WHERE s.token_hash=?", token_hash(tok))
    now = db.now()
    if row is None or row["revoked_at"] is not None or row["expires_at"] < now or now - row["last_seen_at"] > OWNER_IDLE:
        return None
    # Authorization is bound to the immutable principal id of the single owner row, never to a name or a client flag.
    if row["owner_status"] != "active" or not hmac.compare_digest(row["principal_id"], row["owner_principal"]):
        return None
    db.ex("UPDATE owner_sessions SET last_seen_at=? WHERE id=?", now, row["id"])
    return dict(row)


def require_owner(request: Request) -> dict:
    s = owner_session(request)
    if s is None:
        security_event("owner_access_denied", {"path": request.url.path[:120]}, client_ip(request))
        raise ApiError(401, "owner_auth_required", "Owner sign-in required.")
    if not rate_limit(f"owner-api:{s['id']}", 600, 60):
        raise ApiError(429, "rate_limited", "Too many requests. Slow down.")
    return s


def require_step_up(session: dict) -> None:
    if db.now() - session["reauth_at"] > STEP_UP_WINDOW:
        raise ApiError(403, "reauth_required", "Confirm your authenticator code to continue.")


def limit(bucket: str, n: int, window: int, request: Request) -> None:
    if not rate_limit(f"{bucket}:{ip_hash(client_ip(request))}", n, window):
        raise ApiError(429, "rate_limited", "Too many requests. Please wait a moment and try again.")
