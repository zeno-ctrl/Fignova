# Deploying AutoFigure Studio

## Vercel + Neon (serverless "lite" mode)
Works out of the box with `vercel.json` + `api/index.py`. On Vercel the app automatically:
stores everything (database **and** encrypted files) in Neon Postgres, runs processing inside requests (no worker),
detects cover-page images from Word's saved page layout / page breaks / a layout estimate, draws the preview in the
browser, and moves files in 3 MB pieces. Not available on Vercel (size limits): LibreOffice rendering and the OCR part of
AutoFigure Vision (use the laptop/Docker setup for those).

1. Vercel project → **Storage** → connect your **Neon** database (this sets `DATABASE_URL`), or add `DATABASE_URL`
   yourself in **Settings → Environment Variables** with the Neon connection string.
2. **Settings → Environment Variables** (Production), add:
   * `AUTOFIGURE_ENCRYPTION_KEY`: 32 random bytes, base64. Keep a copy, because losing it makes stored files unreadable.
   * `AUTOFIGURE_OWNER_EMAIL`: your email
   * `AUTOFIGURE_OWNER_SETUP_KEY`: any long random password (used once to create the owner login)
   * `CRON_SECRET`: any random string (protects the daily cleanup job)
3. **Deployments → Redeploy** (or push a commit).
4. Open `https://<your-project>.vercel.app/owner/setup`, enter the setup key, set your password and scan the QR code.

Neon's free tier has 0.5 GB of storage; files are deleted after the retention period (30 days by default).

## Render (recommended, about 10 minutes)
1. Merge the work into `main` (or select the `claude/practical-allen-qzoss9` branch in Render).
2. On https://dashboard.render.com choose **New → Blueprint**, connect GitHub and pick `zeno-ctrl/Fignova`.
   Render reads `render.yaml`: one Docker web service plus a 5 GB disk at `/data` (needs the paid *Starter* plan; free
   instances have no disk and lose files on restart).
3. Fill in the secret values when asked:
   * `AUTOFIGURE_ENCRYPTION_KEY`: generate locally with
     `python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"` and keep a copy.
   * `AUTOFIGURE_OWNER_EMAIL`: the creator's email.
   * `AUTOFIGURE_PUBLIC_URL`: the URL Render gives you, e.g. `https://autofigure-studio.onrender.com`.
   * `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`: optional. AutoFigure AI works without them.
4. After the first deploy, open the service's **Shell** tab and run `python -m autofigure owner-invite`. Open the printed
   link to set up the owner password and authenticator app.

## Railway / Fly.io
Use the same `Dockerfile`. Attach a volume at `/data`, set the same environment variables, and expose port `$PORT`.

## Home laptop
See [`deploy/laptop/LAPTOP.md`](deploy/laptop/LAPTOP.md): Docker Desktop + free Cloudflare Tunnel, one setup script.

## Own server (VPS)
`docker compose up -d --build` with a `.env` copied from `.env.example`, behind `deploy/nginx.conf` for HTTPS.
