"""Command line: serve, worker, owner bootstrap, maintenance.

    python -m autofigure serve [--host 0.0.0.0] [--port 8000] [--with-worker]
    python -m autofigure worker
    python -m autofigure owner-invite            # one-time enrollment link for AUTOFIGURE_OWNER_EMAIL
    python -m autofigure owner-reset             # operator recovery: revoke sessions, re-invite
    python -m autofigure retention               # run the retention sweep now
    python -m autofigure check                   # verify renderer, database, storage
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing
import secrets
import sys
import threading


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def cmd_serve(args) -> None:
    import uvicorn

    from .api.app import create_app

    if args.with_worker:
        p = multiprocessing.Process(target=_run_worker, daemon=True)
        p.start()
    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                log_level="warning", access_log=False)


def _run_worker() -> None:
    _setup_logging()
    from . import db
    from .config import get_config
    from .pipeline import worker_loop

    db.migrate()
    n = max(1, get_config().worker_concurrency)
    threads = [threading.Thread(target=worker_loop, daemon=True) for _ in range(n - 1)]
    for t in threads:
        t.start()
    worker_loop()


def cmd_worker(args) -> None:
    _run_worker()


def cmd_owner_invite(args) -> None:
    from . import db
    from .config import get_config
    from .security import audit, new_token, token_hash

    cfg = get_config()
    db.migrate()
    email = cfg.owner_email
    if not email:
        sys.exit("Set AUTOFIGURE_OWNER_EMAIL to the creator's verified email address first.")
    acct = db.q1("SELECT status, email FROM owner_accounts WHERE id=1")
    if acct and acct["status"] == "active":
        sys.exit("An owner is already enrolled. Use `owner-reset` (operator recovery) to replace credentials.")
    principal = acct and db.q1("SELECT principal_id FROM owner_accounts WHERE id=1")["principal_id"]
    if not acct:
        db.ex("INSERT INTO owner_accounts(id, principal_id, email, display_name, status, created_at, updated_at) "
              "VALUES (1,?,?,?,?,?,?)", "own_" + secrets.token_hex(16), email, cfg.owner_display_name, "invited",
              db.now(), db.now())
    else:
        db.ex("UPDATE owner_accounts SET email=?, display_name=?, status='invited', updated_at=? WHERE id=1",
              email, cfg.owner_display_name, db.now())
    tok = new_token()
    db.ex("DELETE FROM owner_invites WHERE used_at IS NULL")
    db.ex("INSERT INTO owner_invites(token_hash, email, expires_at) VALUES (?,?,?)", token_hash(tok), email, db.now() + 1800)
    audit("operator", "owner_invite_created", None, {"principal": (principal or "new")[-6:]})
    print("One-time owner enrollment link (valid 30 minutes, single use):")
    print(f"  {cfg.public_base_url}/owner/enroll#token={tok}")
    print("Open it on the creator's device, set a strong password and scan the authenticator QR code.")


def cmd_owner_reset(args) -> None:
    from . import db
    from .security import audit

    db.migrate()
    if input("This revokes all owner sessions and requires re-enrollment. Type RESET: ").strip() != "RESET":
        sys.exit("Aborted.")
    db.ex("UPDATE owner_sessions SET revoked_at=? WHERE revoked_at IS NULL", db.now())
    db.ex("UPDATE owner_accounts SET status='invited', password_hash=NULL, totp_secret_enc=NULL, recovery_codes='[]', "
          "updated_at=? WHERE id=1", db.now())
    audit("operator", "owner_reset", None, None)
    print("Owner credentials cleared. Run `python -m autofigure owner-invite` to issue a new enrollment link.")


def cmd_retention(args) -> None:
    from . import db
    from .pipeline import retention_sweep

    db.migrate()
    print(f"Expired uploads removed: {retention_sweep()}")


def cmd_check(args) -> None:
    from . import db
    from .config import get_config
    from .engine.render import renderer_status
    from .storage import store

    cfg = get_config()
    db.migrate()
    st = renderer_status()
    k = store().put(b"healthcheck", "chk", None)
    ok_store = store().get(k) == b"healthcheck"
    store().delete(k)
    print(f"data dir        : {cfg.data_dir}")
    print(f"database        : ok ({cfg.db_path.name})")
    print(f"object store    : {'ok' if ok_store else 'FAILED'} (AES-256-GCM)")
    print(f"renderer        : {st['version'] or 'NOT INSTALLED'}")
    print(f"network sandbox : {'unshare -rn' if st['network_sandbox'] else 'not available'}")
    print(f"poppler         : {'ok' if st['pdftoppm'] and st['pdftotext'] else 'MISSING'}")
    print(f"AI captioning   : AutoFigure AI ({cfg.ai_provider}); external models: "
          f"{', '.join(__import__('autofigure.api.public', fromlist=['x']).external_ai_providers()) or 'none (document context only)'}")
    print(f"owner email set : {'yes' if cfg.owner_email else 'NO — set AUTOFIGURE_OWNER_EMAIL'}")


def main() -> None:
    _setup_logging()
    ap = argparse.ArgumentParser(prog="autofigure")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--with-worker", action="store_true")
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    sub.add_parser("owner-invite").set_defaults(fn=cmd_owner_invite)
    sub.add_parser("owner-reset").set_defaults(fn=cmd_owner_reset)
    sub.add_parser("retention").set_defaults(fn=cmd_retention)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
