/* Owner sign-in and one-time enrollment (token in URL fragment, never sent in a request line or logs). */
(function () {
  const $ = (id) => document.getElementById(id);
  const login = $("login-form");
  if (login) {
    $("use-rc").addEventListener("click", () => {
      const rc = $("rc-wrap").classList.toggle("hidden");
      $("totp-wrap").classList.toggle("hidden", !rc);
      $("use-rc").textContent = rc ? "Use a recovery code instead" : "Use authenticator code";
    });
    login.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = login.querySelector("button[type=submit]");
      $("login-err").classList.add("hidden");
      AF.busy(btn, true, "Signing in…");
      try {
        const d = await AF.api("/api/owner/login", { method: "POST", json: {
          email: $("email").value, password: $("password").value, totp: $("totp").value, recovery_code: $("rc").value } });
        if (d.recovery_code_used) alert("You signed in with a recovery code. That code is now used up — rotate your authenticator in Security.");
        location.href = "/owner/dashboard";
      } catch (err) {
        $("login-err").textContent = err.message;
        $("login-err").classList.remove("hidden");
        AF.busy(btn, false);
      }
    });
  }
  const enroll = $("enroll-body");
  if (enroll) {
    const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
    history.replaceState(null, "", location.pathname);
    if (!token) { enroll.innerHTML = '<div class="notice err">' + AF.icon("i-alert") + "<span>Missing invitation. Ask the operator to run <span class=\"mono\">python -m autofigure owner-invite</span>.</span></div>"; return; }
    (async () => {
      try {
        await AF.api("/api/session");
        const d = await AF.api("/api/owner/enroll/start", { method: "POST", json: { token } });
        enroll.innerHTML =
          '<p class="small">Enrolling <b>' + AF.esc(d.email) + "</b>. Scan this QR code with an authenticator app (e.g. Microsoft Authenticator, Google Authenticator, 1Password), or enter the key manually.</p>" +
          '<div class="qr">' + d.qr_svg + "</div><p class=\"xs muted\" style=\"text-align:center\">Manual key: <span class=\"mono\">" + AF.esc(d.secret.replace(/(.{4})/g, "$1 ")) + "</span></p>" +
          '<form id="enroll-form" class="stack" style="--gap:12px"><div><label class="lbl" for="pw1">New password (14+ characters)</label><input type="password" id="pw1" autocomplete="new-password" minlength="14" required></div>' +
          '<div><label class="lbl" for="pw2">Repeat password</label><input type="password" id="pw2" autocomplete="new-password" required></div>' +
          '<div><label class="lbl" for="code">6-digit code from the app</label><input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" required></div>' +
          '<p class="field-error hidden" id="enroll-err" role="alert"></p><button class="btn btn-primary" type="submit">Activate owner access</button></form>';
        document.getElementById("enroll-form").addEventListener("submit", async (e) => {
          e.preventDefault();
          const err = document.getElementById("enroll-err");
          if ($("pw1").value !== $("pw2").value) { err.textContent = "Passwords do not match."; err.classList.remove("hidden"); return; }
          try {
            const r = await AF.api("/api/owner/enroll/finish", { method: "POST", json: { token, password: $("pw1").value, totp: $("code").value } });
            enroll.innerHTML = '<div class="notice ok">' + AF.icon("i-check") + "<span>Owner access is active.</span></div>" +
              '<p class="small"><b>Save these recovery codes</b> somewhere safe. Each can be used once if you lose your authenticator. They will not be shown again.</p>' +
              '<div class="codes">' + r.recovery_codes.map((c) => "<span>" + AF.esc(c) + "</span>").join("") + "</div>" +
              '<a class="btn btn-primary" href="/owner/login">Continue to sign-in</a>';
          } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
        });
      } catch (err) {
        enroll.innerHTML = '<div class="notice err">' + AF.icon("i-alert") + "<span>" + AF.esc(err.message) + "</span></div>";
      }
    })();
  }
})();
