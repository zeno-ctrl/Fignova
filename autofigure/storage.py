"""Private, encrypted object store (AES-256-GCM, per-object nonce).

Objects are addressed by opaque random keys stored in the database; there is
no public static route to this directory, and every read goes through an
authorised API handler. A deployment can swap this class for an S3-compatible
backend implementing the same four methods (put/get/delete/exists).
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db
from .config import get_config

MAGIC = b"AFS1"


class ObjectStore:
    def __init__(self, root: Path, key: bytes):
        self.root = root
        self.aes = AESGCM(key)

    def _path(self, key: str) -> Path:
        if not key or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in key):
            raise ValueError("bad_object_key")
        return self.root / key[:2] / key

    def put(self, data: bytes, kind: str, upload_id: Optional[str]) -> str:
        key = f"{kind[:4]}-{secrets.token_hex(20)}".lower()
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_bytes(12)
        blob = MAGIC + nonce + self.aes.encrypt(nonce, data, key.encode())
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, p)
        db.ex("INSERT INTO objects(key, upload_id, kind, byte_size, sha256, created_at) VALUES (?,?,?,?,?,?)",
              key, upload_id, kind, len(data), hashlib.sha256(data).hexdigest(), db.now())
        return key

    def get(self, key: str) -> bytes:
        row = db.q1("SELECT deleted_at FROM objects WHERE key=?", key)
        if row is None or row["deleted_at"] is not None:
            raise FileNotFoundError(key)
        blob = self._path(key).read_bytes()
        if not blob.startswith(MAGIC):
            raise ValueError("corrupt_object")
        return self.aes.decrypt(blob[4:16], blob[16:], key.encode())

    def exists(self, key: Optional[str]) -> bool:
        if not key:
            return False
        row = db.q1("SELECT deleted_at FROM objects WHERE key=?", key)
        return bool(row) and row["deleted_at"] is None and self._path(key).exists()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        finally:
            db.ex("UPDATE objects SET deleted_at=? WHERE key=? AND deleted_at IS NULL", db.now(), key)

    def delete_upload(self, upload_id: str) -> int:
        rows = db.q("SELECT key FROM objects WHERE upload_id=? AND deleted_at IS NULL", upload_id)
        for r in rows:
            self.delete(r["key"])
        return len(rows)

    def usage_bytes(self) -> int:
        row = db.q1("SELECT COALESCE(SUM(byte_size),0) AS s FROM objects WHERE deleted_at IS NULL")
        return int(row["s"]) if row else 0


_store: Optional[ObjectStore] = None


def store() -> ObjectStore:
    global _store
    cfg = get_config()
    if _store is None or _store.root != cfg.objects_dir:
        _store = ObjectStore(cfg.objects_dir, cfg.encryption_key)
    return _store
