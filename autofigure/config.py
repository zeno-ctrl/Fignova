"""Runtime configuration from environment variables (see .env.example)."""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    data_dir: Path
    db_path: Path
    objects_dir: Path
    encryption_key: bytes
    owner_email: str | None
    owner_display_name: str
    public_base_url: str
    secure_cookies: bool
    max_upload_mb: int
    retention_days: int
    ai_provider: str  # "autofigure" (default) | "anthropic" | "openai" | "none"
    ai_model: str
    ai_max_figures: int
    render_timeout: int
    worker_concurrency: int
    support_contact: str
    operator_name: str
    storage_region: str
    lite: bool = False  # serverless host (Vercel): no LibreOffice, no background worker, no persistent disk
    storage_backend: str = "disk"  # disk | database
    version: str = "1.2.0"

    @property
    def dev_mode(self) -> bool:
        return not self.secure_cookies


def _load_key(data_dir: Path) -> bytes:
    env = os.environ.get("AUTOFIGURE_ENCRYPTION_KEY")
    if env:
        key = base64.urlsafe_b64decode(env + "=" * (-len(env) % 4))
        if len(key) != 32:
            raise RuntimeError("AUTOFIGURE_ENCRYPTION_KEY must be 32 bytes, base64url encoded")
        return key
    # Development only: generate a local key file outside the repository.
    kf = data_dir / "dev-encryption.key"
    if not kf.exists():
        kf.write_bytes(base64.urlsafe_b64encode(secrets.token_bytes(32)))
        os.chmod(kf, 0o600)
    return base64.urlsafe_b64decode(kf.read_bytes())


@lru_cache(maxsize=1)
def get_config() -> Config:
    on_vercel = bool(os.environ.get("VERCEL"))
    lite = _bool(os.environ.get("AUTOFIGURE_LITE"), on_vercel)
    default_dir = "/tmp/autofigure" if on_vercel else str(Path.cwd() / "data")
    data_dir = Path(os.environ.get("AUTOFIGURE_DATA_DIR", default_dir)).resolve()
    has_pg = bool(os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL"))
    storage_backend = os.environ.get("AUTOFIGURE_STORAGE", "database" if (on_vercel or (lite and has_pg)) else "disk")
    if on_vercel and not os.environ.get("AUTOFIGURE_ENCRYPTION_KEY"):
        raise RuntimeError("Set AUTOFIGURE_ENCRYPTION_KEY in Vercel → Settings → Environment Variables")
    if on_vercel and not has_pg:
        raise RuntimeError("Connect a Neon Postgres database in Vercel → Storage (sets DATABASE_URL)")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "objects").mkdir(exist_ok=True)
    ai_provider = os.environ.get("AUTOFIGURE_AI_PROVIDER")
    ai_provider = ai_provider or "autofigure"
    return Config(
        data_dir=data_dir,
        db_path=data_dir / "autofigure.sqlite3",
        objects_dir=data_dir / "objects",
        encryption_key=_load_key(data_dir),
        owner_email=(os.environ.get("AUTOFIGURE_OWNER_EMAIL") or "").strip().lower() or None,
        owner_display_name=os.environ.get("AUTOFIGURE_OWNER_NAME", "Zaw Naing Oo"),
        public_base_url=(os.environ.get("AUTOFIGURE_PUBLIC_URL") or
                         (f"https://{os.environ['VERCEL_PROJECT_PRODUCTION_URL']}" if os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
                          else "http://localhost:8000")).rstrip("/"),
        secure_cookies=_bool(os.environ.get("AUTOFIGURE_SECURE_COOKIES"), on_vercel),
        max_upload_mb=int(os.environ.get("AUTOFIGURE_MAX_UPLOAD_MB", "50")),
        retention_days=int(os.environ.get("AUTOFIGURE_RETENTION_DAYS", "30")),
        ai_provider=ai_provider,
        ai_model=os.environ.get("AUTOFIGURE_AI_MODEL", "claude-opus-5-5"),
        ai_max_figures=int(os.environ.get("AUTOFIGURE_AI_MAX_FIGURES", "150")),
        render_timeout=int(os.environ.get("AUTOFIGURE_RENDER_TIMEOUT", "180")),
        worker_concurrency=int(os.environ.get("AUTOFIGURE_WORKER_CONCURRENCY", "2")),
        support_contact=os.environ.get("AUTOFIGURE_SUPPORT_CONTACT", ""),
        operator_name=os.environ.get("AUTOFIGURE_OPERATOR_NAME", ""),
        storage_region=os.environ.get("AUTOFIGURE_STORAGE_REGION", "the operator's server"),
        lite=lite,
        storage_backend=storage_backend,
    )
