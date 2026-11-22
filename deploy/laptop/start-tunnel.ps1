# Publishes the site on the internet with a free Cloudflare quick tunnel (HTTPS, no router changes).
# The address (https://<random>.trycloudflare.com) changes every time this runs; for a permanent address
# use a named tunnel with your own domain (see LAPTOP.md).
Write-Host "Starting tunnel... look for the https://....trycloudflare.com address below. Keep this window open." -ForegroundColor Cyan
cloudflared tunnel --url http://127.0.0.1:8000
