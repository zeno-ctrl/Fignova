# Deploying AutoFigure Studio

**Why not Vercel?** Vercel runs short serverless functions with no LibreOffice, no background worker and no
permanent disk, so the app cannot work there (you'd see `404 NOT_FOUND`, or this repo's `DEPLOY.html` notice).

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

## Own server (VPS)
`docker compose up -d --build` with a `.env` copied from `.env.example`, behind `deploy/nginx.conf` for HTTPS.
