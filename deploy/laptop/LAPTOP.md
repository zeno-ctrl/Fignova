# Run AutoFigure Studio on your laptop (Windows)

Your laptop (i5 10th gen, 12 GB RAM, 150 GB free) runs the **full version**: real cover-page detection, true preview,
AutoFigure AI, database and owner console. Everything lives in `deploy/laptop/data` on your disk.

## 1. Prepare the laptop (once)
* Settings → System → Power: **Sleep = Never** when plugged in. Control Panel → Power Options → "When I close the lid" = **Do nothing**.
* Turn on **BitLocker** (Settings → Privacy & security → Device encryption / BitLocker) so users' files are encrypted on disk.
* In the BIOS, enable **Virtualization** (Intel VT-x) if Docker asks for it.
* If the battery looks swollen, stop and remove it; run on the charger only.

## 2. Download the project
Install **GitHub Desktop**, sign in, clone `zeno-ctrl/Fignova`, and switch to branch `claude/practical-allen-qzoss9`.
(Or on github.com: Code → Download ZIP for that branch, and unzip it.)

## 3. Install and start (one command, run it until it says "running")
Open **PowerShell** in the `deploy\laptop` folder (Shift + right-click the folder → "Open PowerShell window here") and run:

```
powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
```

The first run installs **Docker Desktop** and **Cloudflare Tunnel**, then asks you to open a new window and run it again.
Start Docker Desktop once (Settings → General → "Start Docker Desktop when you sign in" = on). The second run asks for
your owner email, creates the secret key (a backup goes to `Documents\autofigure-env-BACKUP.txt` — keep it safe),
builds the app (5–15 minutes the first time) and starts it at **http://localhost:8000**. It restarts automatically
after a reboot as long as Docker Desktop starts.

## 4. Create your owner login
```
.\owner-invite.ps1
```
Open the printed link on the laptop, choose a password (14+ characters) and scan the QR code with Google or
Microsoft Authenticator. Save the recovery codes. Your console: `http://localhost:8000/owner/login`.

## 5. Put it on the internet
```
.\start-tunnel.ps1
```
It prints an address like `https://quiet-river-1234.trycloudflare.com` — that's your public website, with HTTPS.
Keep that window open. The free address changes each time you restart the tunnel.

**Permanent address (recommended later):** buy a domain (~$10/year), add it to a free Cloudflare account, then in
Cloudflare Zero Trust → Networks → Tunnels → Create tunnel, choose Windows, run the install command it shows, and add a
public hostname (e.g. `autofigure.yourdomain.com`) pointing to `http://localhost:8000`. That runs as a Windows service.
Your Vercel site can then simply link or redirect to that address.

## Everyday commands (in `deploy\laptop`)
| Task | Command |
|---|---|
| Stop | `docker compose -f docker-compose.laptop.yml down` |
| Start | `docker compose -f docker-compose.laptop.yml up -d` |
| Update after new code | `git pull` (or re-download), then `docker compose -f docker-compose.laptop.yml up -d --build` |
| See logs | `docker compose -f docker-compose.laptop.yml logs -f` |
| Backup | copy the `data` folder **and** `.env` to an external drive |

Optional: add `OPENAI_API_KEY=` / `ANTHROPIC_API_KEY=` values in `.env` (with account credits) and run the Start command
again to let AutoFigure AI also use ChatGPT/Claude.
