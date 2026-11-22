"""Vercel entry point: the whole AutoFigure Studio app as one Python function.

If the app cannot start (usually a missing environment variable), a small page explains what to fix instead
of Vercel's generic "FUNCTION_INVOCATION_FAILED" error. No secret values are ever shown.
"""
import html
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REQUIRED = {
    "AUTOFIGURE_ENCRYPTION_KEY": "32 random bytes in base64 (generate.plus/en/base64, length 32). Keep a copy.",
    "DATABASE_URL": "Connect Neon in Vercel → Storage, or paste the Neon connection string.",
    "AUTOFIGURE_OWNER_EMAIL": "Your email (owner of the private console).",
    "AUTOFIGURE_OWNER_SETUP_KEY": "Any long password (16+ characters), used once at /owner/setup.",
}

def _setup_page(err: str):
    async def setup_app(scope, receive, send):  # minimal ASGI fallback
        if scope["type"] != "http":
            return
        has_db = bool(os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL"))
        rows = []
        for name, hint in REQUIRED.items():
            ok = has_db if name == "DATABASE_URL" else bool(os.environ.get(name))
            rows.append(f"<tr><td><code>{name}</code></td><td>{'✅ set' if ok else '❌ missing'}</td>"
                        f"<td>{html.escape(hint)}</td></tr>")
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoFigure Studio — setup needed</title><style>body{{font:16px/1.6 system-ui,sans-serif;max-width:820px;margin:8vh auto;padding:0 16px;
color:#0b1426;background:#f7fafc}}table{{border-collapse:collapse;width:100%}}td{{border-top:1px solid #dde4ec;padding:8px;vertical-align:top}}
code{{background:#e3f7fa;padding:2px 6px;border-radius:6px}}.err{{background:#fdeceb;padding:10px 12px;border-radius:10px;font-family:monospace;font-size:13px}}</style></head>
<body><h1>AutoFigure Studio needs a few settings</h1>
<p>The app is deployed but cannot start yet. In Vercel open <b>Settings → Environment Variables</b>, add what is missing for
<b>Production</b>, then <b>Deployments → ⋯ → Redeploy</b>.</p>
<table>{''.join(rows)}</table>
<p>Startup error:</p><p class="err">{html.escape(err)}</p>
<p>Created by Zaw Naing Oo.</p></body></html>""".encode()
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    return setup_app


def _build():
    try:
        from autofigure.api.app import create_app
        return create_app()
    except Exception as exc:  # pragma: no cover - only on misconfigured deployments
        traceback.print_exc()  # full detail goes to Vercel → Runtime Logs
        return _setup_page(f"{type(exc).__name__}: {exc}"[:400])


# Vercel's Python builder looks for a module-level `app`.
app = _build()
