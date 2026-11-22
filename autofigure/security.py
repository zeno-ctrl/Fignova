"""Password hashing, opaque tokens, TOTP (RFC 6238), rate limiting, audit chain."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db
from .config import get_config

# --- passwords (scrypt, stdlib) -------------------------------------------
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: Optional[str]) -> bool:
    if not stored:
        # constant-ish work to avoid timing differences when no password exists
        hashlib.scrypt(b"x", salt=b"0" * 16, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=128 * 1024 * 1024, dklen=32)
        return False
    _, n, r, p, salt, dk = stored.split("$")
    test = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                          maxmem=128 * 1024 * 1024, dklen=32)
    return hmac.compare_digest(test, base64.b64decode(dk))


def password_problems(pw: str, email: str = "") -> list[str]:
    probs = []
    if len(pw) < 14:
        probs.append("Use at least 14 characters.")
    if pw.lower() in (email or "").lower() or (email and email.split("@")[0].lower() in pw.lower()):
        probs.append("Do not include your email address.")
    if len(set(pw)) < 6:
        probs.append("Use a less repetitive password.")
    return probs


# --- tokens ----------------------------------------------------------------
def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(("afs-token:" + token).encode()).hexdigest()


def ip_hash(ip: Optional[str]) -> str:
    key = get_config().encryption_key
    return hmac.new(key, (ip or "?").encode(), hashlib.sha256).hexdigest()[:16]


# --- secret encryption (TOTP seed at rest) ---------------------------------
def seal(data: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(get_config().encryption_key).encrypt(nonce, data, aad)


def unseal(blob: bytes, aad: bytes) -> bytes:
    return AESGCM(get_config().encryption_key).decrypt(blob[:12], blob[12:], aad)


# --- TOTP --------------------------------------------------------------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_at(secret: str, t: float, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(t // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return False
    now = time.time()
    return any(hmac.compare_digest(totp_at(secret, now + i * 30), code) for i in range(-window, window + 1))


def otpauth_uri(secret: str, email: str, issuer: str = "AutoFigure Studio") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def recovery_codes(n: int = 8) -> tuple[list[str], list[str]]:
    codes = ["-".join(secrets.token_hex(2) for _ in range(3)) for _ in range(n)]
    return codes, [token_hash(c) for c in codes]


# --- rate limiting (fixed window, DB-backed so it works across workers) --------
def rate_limit(bucket: str, limit: int, window_s: int) -> bool:
    """Return True if the call is allowed."""
    now = time.time()
    with db.tx() as c:
        row = c.execute("SELECT window_start, count FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()
        if row is None or now - row["window_start"] >= window_s:
            c.execute("INSERT OR REPLACE INTO rate_limits(bucket, window_start, count) VALUES (?,?,1)", (bucket, now))
            return True
        if row["count"] >= limit:
            return False
        c.execute("UPDATE rate_limits SET count=count+1 WHERE bucket=?", (bucket,))
        return True


# --- audit log: append-only hash chain -------------------------------------
def audit(actor: str, action: str, target_upload_id: Optional[str] = None, detail: Optional[dict] = None,
          ip: Optional[str] = None) -> None:
    with db.tx() as c:
        prev = c.execute("SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = prev["hash"] if prev else "genesis"
        at = time.time()
        body = db.jdump({"at": at, "actor": actor, "action": action, "target": target_upload_id, "detail": detail or {}})
        h = hashlib.sha256((prev_hash + body).encode()).hexdigest()
        c.execute("INSERT INTO audit_events(at, actor, action, target_upload_id, detail, ip_hash, prev_hash, hash) "
                  "VALUES (?,?,?,?,?,?,?,?)", (at, actor, action, target_upload_id, db.jdump(detail or {}),
                                               ip_hash(ip) if ip else None, prev_hash, h))


def verify_audit_chain() -> tuple[bool, int]:
    prev_hash = "genesis"
    rows = db.q("SELECT * FROM audit_events ORDER BY id")
    for r in rows:
        body = db.jdump({"at": r["at"], "actor": r["actor"], "action": r["action"], "target": r["target_upload_id"],
                         "detail": db.jload(r["detail"], {})})
        if r["prev_hash"] != prev_hash or hashlib.sha256((prev_hash + body).encode()).hexdigest() != r["hash"]:
            return False, r["id"]
        prev_hash = r["hash"]
    return True, len(rows)


def security_event(kind: str, detail: Optional[dict] = None, ip: Optional[str] = None) -> None:
    db.ex("INSERT INTO security_events(at, kind, detail, ip_hash) VALUES (?,?,?,?)",
          time.time(), kind, db.jdump(detail or {}), ip_hash(ip) if ip else None)
