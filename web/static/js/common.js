/* Shared helpers: CSRF-aware fetch, toasts, theme toggle, mobile nav, formatting. */
(function () {
  const AF = (window.AF = window.AF || {});

  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }
  AF.csrf = () => cookie("afs_csrf");

  AF.api = async function (url, opts = {}) {
    const o = Object.assign({ method: "GET", headers: {} }, opts);
    o.credentials = "same-origin";
    if (o.method !== "GET") o.headers["X-CSRF-Token"] = AF.csrf();
    if (o.json !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.json); delete o.json; }
    let res;
    try { res = await fetch(url, o); }
    catch (e) { const err = new Error("Network problem — check your connection and try again."); err.code = "network"; throw err; }
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : null;
    if (!res.ok) {
      const e = (data && data.error) || {};
      const err = new Error(e.message || "Request failed (" + res.status + ").");
      err.code = e.code || String(res.status); err.status = res.status; err.requestId = e.request_id;
      throw err;
    }
    return data;
  };

  AF.toast = function (msg, kind = "info", ms = 4500) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast " + (kind === "error" ? "err" : kind === "ok" ? "ok" : "");
    t.setAttribute("role", kind === "error" ? "alert" : "status");
    const icon = kind === "error" ? "i-alert" : kind === "ok" ? "i-check" : "i-info";
    t.innerHTML = '<svg class="icon"><use href="#' + icon + '"/></svg><div></div>';
    t.lastChild.textContent = msg;
    box.appendChild(t);
    setTimeout(() => t.remove(), ms);
  };

  // Files travel in pieces on serverless hosts (4.5 MB request/response limit). Works on every host.
  AF.fetchChunked = async function (url) {
    const sep = url.includes("?") ? "&" : "?";
    const get = async (i) => {
      const res = await fetch(url + sep + "chunk=" + i, { credentials: "same-origin" });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        const e = new Error((d.error && d.error.message) || "Download failed."); e.code = d.error && d.error.code; e.status = res.status; throw e;
      }
      return res;
    };
    const first = await get(0);
    const total = Number(first.headers.get("X-Total-Chunks") || 1);
    const parts = [await first.arrayBuffer()];
    for (let i = 1; i < total; i++) parts.push(await (await get(i)).arrayBuffer());
    return new Blob(parts, { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
  };
  AF.saveBlob = function (blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  };
  // Draw a .docx in the browser (serverless mode, no server-side renderer).
  AF.renderDocx = async function (blob, container) {
    container.innerHTML = "";
    const style = document.createElement("div");
    const body = document.createElement("div");
    container.append(style, body);
    await window.docx.renderAsync(blob, body, style, { className: "docx", inWrapper: true, breakPages: true,
      ignoreLastRenderedPageBreak: false, ignoreWidth: false, ignoreHeight: false, renderHeaders: true, renderFooters: true });
    return [...body.querySelectorAll("section.docx")];
  };

  AF.bytes = (n) => n == null ? "–" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";
  AF.date = (s) => s ? new Date(s * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–";
  AF.dateOnly = (s) => s ? new Date(s * 1000).toLocaleDateString(undefined, { dateStyle: "medium" }) : "–";
  AF.esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  AF.icon = (id, cls = "icon") => '<svg class="' + cls + '" aria-hidden="true"><use href="#' + id + '"/></svg>';
  AF.busy = function (btn, on, label) {
    if (!btn) return;
    if (on) { btn.dataset.html = btn.innerHTML; btn.classList.add("is-loading"); btn.disabled = true; btn.innerHTML = '<span class="spin" aria-hidden="true"></span>' + AF.esc(label || "Working…"); }
    else { btn.classList.remove("is-loading"); btn.disabled = false; if (btn.dataset.html) btn.innerHTML = btn.dataset.html; }
  };

  function setThemeIcon() {
    const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
      (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    document.querySelectorAll("[data-theme-toggle] use").forEach((u) => u.setAttribute("href", dark ? "#i-sun" : "#i-moon"));
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.setAttribute("aria-pressed", String(dark)));
  }
  document.addEventListener("click", (e) => {
    const tt = e.target.closest("[data-theme-toggle]");
    if (tt) {
      const cur = document.documentElement.getAttribute("data-theme") ||
        (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("afs-theme", next); } catch (err) {}
      setThemeIcon();
    }
    const mb = e.target.closest("[data-menu]");
    if (mb) {
      const nav = document.getElementById("mobile-nav");
      const open = nav.getAttribute("data-open") !== "true";
      nav.setAttribute("data-open", String(open));
      mb.setAttribute("aria-expanded", String(open));
    }
  });
  setThemeIcon();
  // make sure a CSRF cookie exists even on cached pages
  if (!AF.csrf()) AF.api("/api/session").catch(() => {});
})();
