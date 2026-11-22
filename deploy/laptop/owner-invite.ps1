# Prints the one-time owner enrollment link (valid 30 minutes).
Set-Location $PSScriptRoot
docker compose -f docker-compose.laptop.yml exec autofigure python -m autofigure owner-invite
Write-Host "Open the link on THIS laptop (http://localhost:8000/...) or replace 'http://localhost:8000' with your public tunnel address." -ForegroundColor Yellow
