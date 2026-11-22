# AutoFigure Studio — Windows laptop setup. Run in PowerShell from this folder:
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Need($cmd, $wingetId, $name) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    Write-Host "Installing $name ..." -ForegroundColor Cyan
    winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements
    Write-Host "$name installed. Close this window, open a NEW PowerShell window and run the script again." -ForegroundColor Yellow
    if ($name -eq "Docker Desktop") { Write-Host "Start Docker Desktop once and wait until it says 'Engine running' (it may ask to restart Windows)." -ForegroundColor Yellow }
    exit 0
  }
}
Need docker "Docker.DockerDesktop" "Docker Desktop"
Need cloudflared "Cloudflare.cloudflared" "Cloudflare Tunnel"

docker info *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "Docker is not running. Open Docker Desktop, wait for 'Engine running', then run this again." -ForegroundColor Red; exit 1 }

if (-not (Test-Path .env)) {
  $email = Read-Host "Owner email (your email for the private owner console)"
  $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $key = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
@"
AUTOFIGURE_ENCRYPTION_KEY=$key
AUTOFIGURE_OWNER_EMAIL=$email
AUTOFIGURE_OWNER_NAME=Zaw Naing Oo
AUTOFIGURE_SECURE_COOKIES=true
AUTOFIGURE_PUBLIC_URL=http://localhost:8000
AUTOFIGURE_WORKER_CONCURRENCY=3
AUTOFIGURE_AI_PROVIDER=autofigure
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
"@ | Set-Content -Encoding ASCII .env
  Copy-Item .env "$env:USERPROFILE\Documents\autofigure-env-BACKUP.txt"
  Write-Host "Created .env. A backup copy was saved to Documents\autofigure-env-BACKUP.txt — keep it safe: without the encryption key stored files cannot be read." -ForegroundColor Green
}

Write-Host "Building and starting AutoFigure Studio (first time takes 5-15 minutes) ..." -ForegroundColor Cyan
docker compose -f docker-compose.laptop.yml up -d --build
for ($i = 0; $i -lt 60; $i++) {
  try { if ((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/healthz).StatusCode -eq 200) { break } } catch {}
  Start-Sleep 3
}
Write-Host "AutoFigure Studio is running at http://localhost:8000" -ForegroundColor Green
Write-Host ""
Write-Host "Next: owner account -> run:  .\owner-invite.ps1" -ForegroundColor Cyan
Write-Host "Next: public link  -> run:  .\start-tunnel.ps1" -ForegroundColor Cyan
