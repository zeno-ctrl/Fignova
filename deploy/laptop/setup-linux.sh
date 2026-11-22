#!/usr/bin/env bash
# AutoFigure Studio — Linux (Ubuntu/Debian) laptop setup:  bash setup-linux.sh
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sudo sh; sudo usermod -aG docker "$USER"; echo "Docker installed. Log out and back in, then run again."; exit 0; fi
if ! command -v cloudflared >/dev/null; then
  curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  sudo dpkg -i /tmp/cloudflared.deb
fi
if [ ! -f .env ]; then
  read -rp "Owner email: " email
  key=$(python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('='))")
  printf "AUTOFIGURE_ENCRYPTION_KEY=%s\nAUTOFIGURE_OWNER_EMAIL=%s\nAUTOFIGURE_OWNER_NAME=Zaw Naing Oo\nAUTOFIGURE_SECURE_COOKIES=true\nAUTOFIGURE_PUBLIC_URL=http://localhost:8000\nAUTOFIGURE_WORKER_CONCURRENCY=3\nAUTOFIGURE_AI_PROVIDER=autofigure\n" "$key" "$email" > .env
  chmod 600 .env; cp .env ~/autofigure-env-BACKUP.txt
  echo "Created .env (backup in ~/autofigure-env-BACKUP.txt — keep it safe)."
fi
docker compose -f docker-compose.laptop.yml up -d --build
until curl -fs http://127.0.0.1:8000/healthz >/dev/null; do sleep 3; done
echo "Running at http://localhost:8000"
echo "Owner link:  docker compose -f docker-compose.laptop.yml exec autofigure python -m autofigure owner-invite"
echo "Public link: cloudflared tunnel --url http://127.0.0.1:8000"
