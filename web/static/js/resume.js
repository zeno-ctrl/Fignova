(function () {
  const code = location.hash.slice(1);
  history.replaceState(null, "", "/resume");
  const msg = document.getElementById("resume-msg");
  if (!code) { msg.textContent = "This link is incomplete. Create a new one on your other device."; return; }
  AF.api("/api/session").then(() => AF.api("/api/session/resume", { method: "POST", json: { code } }))
    .then(() => { msg.textContent = "Done! Taking you to your documents…"; setTimeout(() => (location.href = "/app#docs"), 700); })
    .catch((e) => { msg.innerHTML = AF.esc(e.message) + '<br><br><a class="btn btn-primary" href="/app">Go to workspace</a>'; });
})();
