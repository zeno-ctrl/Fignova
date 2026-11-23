/* Owner console pages. Every call is authorised server-side; this script only renders. */
(function () {
  const $ = (id) => document.getElementById(id);
  const section = document.body.dataset.section;
  AF.api("/api/session").then((x) => { window.AF_LITE = !!x.lite; }).catch(() => {});
  const STATE_PILL = { review_ready: ["Ready", "pill-success"], failed: ["Failed", "pill-error"], deleted: ["Deleted", ""], expired: ["Expired", ""], regenerating: ["Regenerating", "pill-accent"] };
  const statePill = (s) => { const [l, c] = STATE_PILL[s] || ["Processing", "pill-accent"]; return '<span class="pill ' + c + '">' + l + "</span>"; };

  // step-up wrapper: retries the action after a successful authenticator confirmation
  async function sensitive(fn) {
    try { return await fn(); }
    catch (e) {
      if (e.code !== "reauth_required") throw e;
      const ok = await stepUp();
      if (!ok) throw new Error("Action cancelled.");
      return await fn();
    }
  }
  function stepUp() {
    return new Promise((resolve) => {
      const dlg = $("stepup"), form = $("stepup-form"), err = $("stepup-err");
      err.classList.add("hidden"); $("stepup-code").value = "";
      dlg.showModal();
      const done = (v) => { form.onsubmit = null; $("stepup-cancel").onclick = null; dlg.close(); resolve(v); };
      $("stepup-cancel").onclick = () => done(false);
      form.onsubmit = async (e) => {
        e.preventDefault();
        try { await AF.api("/api/owner/reauth", { method: "POST", json: { totp: $("stepup-code").value } }); done(true); }
        catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
      };
    });
  }
  async function downloadFile(url, fallbackName) {
    if (window.AF_LITE) {
      const blob = await sensitive(() => AF.fetchChunked(url));
      AF.saveBlob(blob, fallbackName);
      return;
    }
    await sensitive(async () => {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) { const d = await res.json().catch(() => ({})); const e = new Error((d.error && d.error.message) || "Download failed"); e.code = d.error && d.error.code; throw e; }
      const blob = await res.blob();
      const cd = res.headers.get("content-disposition") || "";
      const m = cd.match(/filename\*=UTF-8''([^;]+)/);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = m ? decodeURIComponent(m[1]) : fallbackName;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    });
  }
  $("logout")?.addEventListener("click", async () => { await AF.api("/api/owner/logout", { method: "POST" }).catch(() => {}); location.href = "/owner/login"; });
  const guard = (p) => p.catch((e) => { if (e.status === 401) location.href = "/owner/login"; else AF.toast(e.message, "error"); });

  // ------------------------------------------------------------ dashboard
  if (section === "dashboard") {
    const hr = new Date().getHours();
    const g = $("greet");
    if (g) g.textContent = (hr < 12 ? "Good morning, " : hr < 18 ? "Good afternoon, " : "Good evening, ") + g.textContent.replace("Welcome back, ", "");
    if ($("greet-date")) $("greet-date").textContent = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    let days = 30;
    const load = () => guard(AF.api("/api/owner/metrics?days=" + days).then((m) => {
      const ICONS = ["i-file", "i-upload", "i-check", "i-layers"];
      let k = 0;
      const card = (label, val, sub) => '<div class="card metric"><span class="m-ic">' + AF.icon(ICONS[k++]) + '</span><div class="mlabel">' + label + '</div><div class="mval">' + val + '</div><div class="msub">' + (sub || "&nbsp;") + "</div></div>";
      $("metrics").innerHTML =
        card("Retained uploads", m.retained_uploads, m.expiring_within_3_days + " expire within 3 days") +
        card("New uploads", m.new_uploads, "last " + m.days + " days") +
        card("Completed / failed", m.completed_jobs + " / " + m.failed_jobs, m.preview_failures + " preview render failures") +
        card("Storage used", AF.bytes(m.storage_bytes), Math.round((m.storage_bytes / 1048576 / m.storage_limit_mb) * 100) + "% of " + m.storage_limit_mb + " MB · " + m.uploads_today + "/" + m.max_uploads_per_day + " uploads today") +
        '<div class="card metric" style="grid-column:1/-1;padding:14px 20px"><div class="row between"><span class="mlabel">Storage capacity</span><span class="xs muted">New uploads pause automatically at 95%</span></div>' +
        '<div class="progress" style="margin-top:8px;height:10px"><i style="width:' + Math.min(100, (m.storage_bytes / 1048576 / m.storage_limit_mb) * 100).toFixed(1) + '%;' + ((m.storage_bytes / 1048576 / m.storage_limit_mb) > 0.8 ? "background:var(--warning)" : "") + '"></i></div></div>';
      // single-series bar chart (no legend needed), hover tooltip, table fallback
      const max = Math.max(1, ...m.uploads_per_day);
      const start = Date.now() / 1000 - m.days * 86400;
      $("chart").innerHTML = '<div class="bars" role="img" aria-label="Uploads per day for the last ' + m.days + ' days; maximum ' + max + '">' +
        m.uploads_per_day.map((n, i) => '<div class="bar" data-zero="' + (n === 0) + '" data-tip="' + AF.dateOnly(start + i * 86400 + 43200) + ": " + n + " upload" + (n === 1 ? "" : "s") + '" style="height:' + Math.max(n ? 4 : 0, (n / max) * 100) + '%"></div>').join("") +
        '</div><div class="row between xs muted" style="margin-top:6px"><span>' + AF.dateOnly(start) + "</span><span>max " + max + "/day</span><span>today</span></div>";
      $("chart-table").innerHTML = '<table class="data"><thead><tr><th>Day</th><th class="num">Uploads</th></tr></thead><tbody>' +
        m.uploads_per_day.map((n, i) => n ? "<tr><td>" + AF.dateOnly(start + i * 86400 + 43200) + '</td><td class="num">' + n + "</td></tr>" : "").join("") + "</tbody></table>";
      const states = Object.entries(m.jobs_by_state);
      $("status-list").innerHTML = states.length ? states.map(([s, n]) => "<li>" + statePill(s) + '<span class="muted small grow" style="padding-left:8px">' + AF.esc(s.replace(/_/g, " ")) + '</span><b class="tnum">' + n + "</b></li>").join("") : '<li class="muted">No jobs in this period.</li>';
      const att = [];
      if (m.stuck_jobs.length) att.push('<div class="notice warn">' + AF.icon("i-alert") + "<span>" + m.stuck_jobs.length + ' job(s) have not progressed for 15+ minutes. <a href="/owner/jobs">Review jobs</a></span></div>');
      if (m.preview_failures) att.push('<div class="notice warn">' + AF.icon("i-alert") + "<span>" + m.preview_failures + " preview render failure(s).</span></div>");
      if (m.queue.dead) att.push('<div class="notice err">' + AF.icon("i-alert") + "<span>" + m.queue.dead + ' dead-lettered task(s). <a href="/owner/jobs">Inspect</a></span></div>');
      att.push('<p class="xs muted">Caption engine: AutoFigure AI (' + AF.esc(m.ai_provider) + ") · model calls this period: " + m.ai_calls + "</p>");
      $("attention").innerHTML = att.join("");
    }));
    document.querySelectorAll("[data-days]").forEach((b) => b.addEventListener("click", () => { days = Number(b.dataset.days); document.querySelectorAll("[data-days]").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); load(); }));
    const tip = $("chart-tip");
    document.addEventListener("mousemove", (e) => {
      const b = e.target.closest(".bar");
      if (!b) { tip.classList.add("hidden"); return; }
      tip.textContent = b.dataset.tip; tip.style.left = e.clientX + "px"; tip.style.top = e.clientY + "px"; tip.classList.remove("hidden");
    });
    load();
    guard(AF.api("/api/owner/uploads?page=1").then((d) => { $("latest").innerHTML = uploadTable(d.items.slice(0, 6), false); }));
  }

  function uploadTable(items, selectable) {
    if (!items.length) return '<div class="empty">' + AF.icon("i-file") + "<p>No uploads match.</p></div>";
    return (selectable ? "" : '<div class="table-wrap"><table class="data"><thead><tr><th>File</th><th>Uploaded</th><th>Status</th><th class="num">Captions</th></tr></thead><tbody>') +
      items.map((u) => selectable ?
        "<tr><td><input type=checkbox data-sel=\"" + u.id + "\" aria-label=\"Select " + AF.esc(u.original_name) + "\"" + (u.deletion_state !== "active" ? " disabled" : "") + '></td><td><a href="/owner/uploads/' + u.id + '">' + AF.esc(u.original_name) + '</a><div class="xs muted mono">' + u.id.slice(-12) + "</div></td><td>" + AF.esc(u.uploader) + "</td><td>" + AF.date(u.created_at) + '</td><td class="num">' + AF.bytes(u.byte_size) + '</td><td class="num">' + (u.page_count ?? "–") + '</td><td class="num">' + (u.figure_count ?? "–") + '</td><td class="num">' + u.captions + "</td><td>" + (u.deletion_state === "active" ? statePill(u.state) : '<span class="pill">' + AF.esc(u.deletion_state.replace("_", " ")) + "</span>") + "</td><td>" + (u.deletion_state === "active" ? AF.dateOnly(u.retention_expires_at) : "—") + "</td></tr>"
        : '<tr><td><a href="/owner/uploads/' + u.id + '">' + AF.esc(u.original_name) + "</a></td><td>" + AF.date(u.created_at) + "</td><td>" + (u.deletion_state === "active" ? statePill(u.state) : '<span class="pill">' + AF.esc(u.deletion_state) + "</span>") + '</td><td class="num">' + u.captions + "</td></tr>").join("") +
      (selectable ? "" : "</tbody></table></div>");
  }

  // ------------------------------------------------------------ uploads library
  if (section === "uploads" && $("rows")) {
    let page = 1;
    const load = () => {
      const f = new FormData($("filters"));
      const qs = new URLSearchParams({ search: f.get("search") || "", status: f.get("status") || "", sort: f.get("sort") || "newest", page });
      if (f.get("from")) qs.set("date_from", String(new Date(f.get("from")).getTime() / 1000));
      guard(AF.api("/api/owner/uploads?" + qs).then((d) => {
        $("rows").innerHTML = d.items.length ? uploadTable(d.items, true) : '<tr><td colspan="10"><div class="empty">' + AF.icon("i-file") + "<p>No uploads match these filters.</p></div></td></tr>";
        $("count").textContent = d.total + " record" + (d.total === 1 ? "" : "s");
        const pages = Math.max(1, Math.ceil(d.total / d.page_size));
        $("pager").innerHTML = '<button class="btn btn-sm" ' + (page <= 1 ? "disabled" : "") + ' data-p="-1">Previous</button><span class="small muted">Page ' + page + " of " + pages + '</span><button class="btn btn-sm" ' + (page >= pages ? "disabled" : "") + ' data-p="1">Next</button>';
        updateBulk();
      }));
    };
    const updateBulk = () => { const n = document.querySelectorAll("[data-sel]:checked").length; $("bulk-delete").disabled = !n; $("bulk-delete").lastChild.textContent = n ? "Delete " + n + " selected" : "Delete selected"; };
    $("filters").addEventListener("submit", (e) => { e.preventDefault(); page = 1; load(); });
    ["status", "sort", "from"].forEach((id) => $(id).addEventListener("change", () => { page = 1; load(); }));
    $("pager").addEventListener("click", (e) => { const b = e.target.closest("[data-p]"); if (b) { page += Number(b.dataset.p); load(); } });
    $("sel-all").addEventListener("change", (e) => { document.querySelectorAll("[data-sel]:not(:disabled)").forEach((c) => (c.checked = e.target.checked)); updateBulk(); });
    $("rows").addEventListener("change", updateBulk);
    $("bulk-delete").addEventListener("click", async () => {
      const ids = [...document.querySelectorAll("[data-sel]:checked")].map((c) => c.dataset.sel);
      const typed = prompt("This permanently deletes the originals, outputs and previews of " + ids.length + " upload(s).\nType DELETE " + ids.length + " to confirm.");
      if (typed !== "DELETE " + ids.length) return;
      try { const r = await sensitive(() => AF.api("/api/owner/uploads/bulk-delete", { method: "POST", json: { upload_ids: ids, confirm: typed } })); AF.toast("Deleted " + r.deleted + " upload(s), " + r.objects_removed + " stored objects removed.", "ok"); load(); }
      catch (e) { AF.toast(e.message, "error"); }
    });
    load();
  }

  // ------------------------------------------------------------ upload details
  const detail = $("upload-detail");
  if (detail) {
    const UID = detail.dataset.upload;
    const base = "/api/owner/uploads/" + UID;
    const render = (d) => {
      const u = d.upload, j = d.job, active = u.deletion_state === "active";
      const s = j.summary || {}, c = s.counts || {};
      const heads = (j.headings || []).map((h) => AF.esc(h.text)).join(" · ") || "None detected";
      detail.innerHTML =
        '<div class="row between" style="margin-bottom:18px"><div><a class="small" href="/owner/uploads">← Uploads</a><h2 style="font-family:var(--font);font-size:1.4rem;margin:6px 0 0;overflow-wrap:anywhere">' + AF.esc(u.original_name) + "</h2>" +
        '<span class="small muted">' + AF.esc(u.uploader) + " · uploaded " + AF.date(u.created_at) + "</span></div>" +
        '<div class="row">' + (active ? statePill(j.state) : '<span class="pill pill-error">' + AF.esc(u.deletion_state.replace("_", " ")) + "</span>") + "</div></div>" +
        (active ? "" : '<div class="notice warn" style="margin-bottom:16px">' + AF.icon("i-info") + "<span>Files for this upload were deleted on " + AF.date(u.deleted_at) + ". Only minimal metadata and audit history remain; there is nothing to preview or download.</span></div>") +
        '<div class="row" style="gap:8px;margin-bottom:20px">' +
        '<button class="btn btn-sm" data-act="prev-src" ' + (active ? "" : "disabled") + ">" + AF.icon("i-eye") + "Preview original</button>" +
        '<button class="btn btn-sm" data-act="dl-src" ' + (u.original_available ? "" : "disabled") + ">" + AF.icon("i-download") + "Download original</button>" +
        '<button class="btn btn-sm" data-act="prev-out" ' + (active && j.ready_revision ? "" : "disabled") + ">" + AF.icon("i-eye") + "Preview processed</button>" +
        '<button class="btn btn-sm" data-act="dl-out" ' + (active && j.ready_revision ? "" : "disabled") + ">" + AF.icon("i-download") + "Download processed</button>" +
        '<button class="btn btn-danger btn-sm" data-act="delete" ' + (active ? "" : "disabled") + ">" + AF.icon("i-trash") + "Delete files</button>" +
        '<a class="btn btn-ghost btn-sm" href="#activity">' + AF.icon("i-list") + "View audit log</a></div>" +
        (active && !j.ready_revision ? '<div class="notice warn" style="margin-bottom:16px">' + AF.icon("i-info") + "<span>No processed version: " + AF.esc(j.failure ? j.failure.message + " (" + j.failure.code + ")" : "processing has not finished.") + "</span></div>" : "") +
        '<div class="grid-2" style="align-items:start;grid-template-columns:1fr 1.4fr">' +
        '<section class="card card-pad"><h3>Document metadata</h3><dl class="kv">' +
        "<dt>Type</dt><dd>" + AF.esc(u.file_type) + "</dd><dt>Size</dt><dd>" + AF.bytes(u.byte_size) + '</dd><dt>SHA-256</dt><dd class="mono">' + AF.esc(u.sha256_prefix) + "…</dd>" +
        "<dt>Pages</dt><dd>" + (u.page_count ?? "–") + "</dd><dt>LO / Heading 1</dt><dd>" + heads + "</dd><dt>Images</dt><dd>" + (u.figure_count ?? "–") + "</dd>" +
        "<dt>Page-1 exclusions</dt><dd>" + (c.excluded_first_page || 0) + "</dd><dt>Captions written</dt><dd>" + ((c.insert || 0) + (c.replace || 0) + (c.renumber || 0)) + "</dd>" +
        "<dt>Needs decision</dt><dd>" + ((c.excluded_page_uncertain || 0) + (c.excluded_unassigned || 0)) + "</dd><dt>Warnings</dt><dd>" + AF.esc([].concat(s.warnings || [], s.analysis_warnings || [], u.validation_warnings || []).join(", ") || "none") + "</dd>" +
        "<dt>Retention until</dt><dd>" + AF.date(u.retention_expires_at) + "</dd><dt>Job</dt><dd class=\"mono\">" + AF.esc(j.id) + "</dd></dl>" +
        '<h3 style="margin-top:20px">Versions</h3>' + (d.revisions.length ? '<ul class="status-list">' + d.revisions.map((r) => "<li><span>Revision " + r.revision + " · " + AF.date(r.created_at) + '</span><span class="pill ' + (r.preview_state === "ready" ? "pill-success" : "") + '">' + AF.esc(r.preview_state) + "</span></li>").join("") + "</ul>" : '<p class="muted">No output versions.</p>') +
        "</section>" +
        '<section class="card card-pad"><h3>Figures</h3>' + (d.figures.length ? '<div class="table-wrap"><table class="data"><thead><tr><th></th><th>Label</th><th>Page</th><th>Status</th><th>Caption</th></tr></thead><tbody>' +
          d.figures.map((f) => "<tr><td>" + (f.thumb_url ? '<img src="' + f.thumb_url + '" alt="" style="width:56px;height:40px;object-fit:cover;border-radius:6px">' : "") + '</td><td class="tnum">' + AF.esc(f.label || "–") + "</td><td>" + (f.page ?? "?") + "</td><td>" + AF.esc(f.status_label) + "</td><td>" + AF.esc(f.caption_text || "") + "</td></tr>").join("") +
          "</tbody></table></div>" : '<p class="muted">No figure data available.</p>') + "</section></div>" +
        activityHtml(d.activity);
    };
    const activityHtml = (acts) => '<section class="card card-pad" id="activity" style="margin-top:20px"><h3>Activity</h3>' + (acts.length ? '<ul class="status-list">' + acts.map((a) => "<li><span>" + AF.esc(a.action.replace(/_/g, " ")) + ' <span class="muted">by ' + AF.esc(a.actor) + "</span></span><span class=\"small muted\">" + AF.date(a.at) + "</span></li>").join("") + "</ul>" : '<p class="muted">No owner activity recorded for this upload yet.</p>') + "</section>";
    const load = () => guard(AF.api(base).then(render));
    async function openViewer(kind) {
      const man = await AF.api(base + "/preview?kind=" + (kind === "src" ? "source" : "output"));
      const card = $("viewer-card");
      card.classList.remove("hidden");
      $("viewer-title").textContent = (kind === "src" ? "Original Word document" : "Processed Word document (revision " + man.revision + ")");
      if (man.state === "client") {
        $("viewer-thumbs").innerHTML = "";
        $("viewer-pages").innerHTML = '<div class="skel" style="height:300px;width:100%"></div>';
        try {
          const sections = await AF.renderDocx(await AF.fetchChunked(man.file), $("viewer-pages"));
          $("viewer-title").textContent += " · " + sections.length + " pages (drawn in browser)";
        } catch (er) { $("viewer-pages").innerHTML = '<div class="notice err">' + AF.icon("i-alert") + "<span>" + AF.esc(er.message) + "</span></div>"; }
        card.scrollIntoView({ behavior: "smooth" });
        refreshActivity();
        return;
      }
      if (man.state !== "ready") { $("viewer-pages").innerHTML = '<div class="notice warn">' + AF.icon("i-alert") + "<span>Preview is not available (" + AF.esc(man.state) + ").</span></div>"; $("viewer-thumbs").innerHTML = ""; return; }
      $("viewer-thumbs").innerHTML = man.pages.map((p) => '<a href="#vp-' + p.n + '" style="flex:none"><img src="' + p.thumb + '" alt="Page ' + p.n + '" style="width:70px;border-radius:3px;box-shadow:var(--shadow-1)"></a>').join("");
      $("viewer-pages").innerHTML = man.pages.map((p) => '<div class="page" id="vp-' + p.n + '" style="--pw:760px"><img loading="lazy" src="' + p.src + '" alt="Page ' + p.n + '"><span class="pnum">' + p.n + "</span></div>").join("");
      card.scrollIntoView({ behavior: "smooth" });
      refreshActivity();
    }
    function refreshActivity() {
      AF.api(base).then((d) => {
        const act = document.querySelector("#activity");
        if (act) act.outerHTML = activityHtml(d.activity);
      }).catch(() => {});
    }
    $("close-viewer").addEventListener("click", () => { $("viewer-card").classList.add("hidden"); $("viewer-pages").innerHTML = ""; $("viewer-thumbs").innerHTML = ""; });
    detail.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const act = b.dataset.act;
      try {
        if (act === "prev-src") await openViewer("src");
        if (act === "prev-out") await openViewer("out");
        if (act === "dl-src") { await downloadFile(base + "/download?version=original", "original.docx"); refreshActivity(); }
        if (act === "dl-out") { await downloadFile(base + "/download?version=processed", "processed.docx"); refreshActivity(); }
        if (act === "delete") {
          const imp = await AF.api(base + "/delete-impact");
          if (!confirm("Delete this upload's files?\n\n" + imp.total + " stored objects will be removed (" + Object.entries(imp.objects).map(([k, v]) => v + " " + k).join(", ") + ").\nMetadata and the audit trail are kept. This cannot be undone.")) return;
          await sensitive(() => AF.api(base, { method: "DELETE" }));
          AF.toast("Upload files deleted.", "ok"); $("viewer-card").classList.add("hidden"); load();
        }
      } catch (err) { AF.toast(err.message, "error"); }
    });
    load();
  }

  // ------------------------------------------------------------ jobs
  if (section === "jobs") {
    const load = () => guard(AF.api("/api/owner/jobs?state=" + encodeURIComponent($("jstate").value)).then((d) => {
      $("qdepth").textContent = "Queue: " + Object.entries(d.queue).map(([k, v]) => v + " " + k).join(", ");
      $("jobs").innerHTML = d.jobs.length ? d.jobs.map((j) => '<tr><td class="mono">' + j.id.slice(-10) + '</td><td><a href="/owner/uploads/' + j.upload_id + '">' + AF.esc(j.original_name) + "</a></td><td>" + statePill(j.state) + " <span class=\"xs mono\">" + AF.esc(j.state) + "</span></td><td>" + AF.esc(j.failure_code || "") + "</td><td>" + AF.date(j.created_at) + "</td><td>" + AF.date(j.updated_at) + '</td><td class="num">' + (j.ready_revision ?? "–") + '</td><td class="num">' + j.ai_calls + "</td><td>" + (j.state === "failed" ? '<button class="btn btn-xs" data-retry="' + j.id + '">Retry</button>' : "") + "</td></tr>").join("")
        : '<tr><td colspan="9"><div class="empty">No jobs.</div></td></tr>';
      $("dead").innerHTML = d.dead_tasks.length ? '<ul class="status-list">' + d.dead_tasks.map((t) => "<li><span class=\"mono\">#" + t.id + " " + AF.esc(t.kind) + "</span><span>" + AF.esc(t.last_error) + " after " + t.attempts + " attempts</span></li>").join("") + "</ul>" : '<p class="muted">None.</p>';
    }));
    $("jstate").addEventListener("change", load);
    $("jobs").addEventListener("click", async (e) => { const b = e.target.closest("[data-retry]"); if (!b) return; try { await AF.api("/api/owner/jobs/" + b.dataset.retry + "/retry", { method: "POST" }); AF.toast("Job re-queued.", "ok"); load(); } catch (er) { AF.toast(er.message, "error"); } });
    load();
  }

  // ------------------------------------------------------------ settings
  if (section === "settings") {
    const FIELDS = [["max_upload_mb", "Maximum upload size (MB)", 1, 200], ["retention_days", "Retention period (days)", 1, 365], ["max_figures", "Maximum images per document", 1, 2000], ["max_pages", "Maximum pages per document", 1, 2000], ["max_ai_suggestions_per_job", "AI model calls per document", 0, 1000], ["max_uploads_per_day", "Uploads per day (whole site)", 1, 100000], ["storage_limit_mb", "Storage limit (MB) — Neon free tier ≈ 450", 50, 10000000]];
    const load = () => guard(AF.api("/api/owner/settings").then((d) => {
      const s = d.settings;
      $("svc-fields").innerHTML = '<div class="form-grid">' + FIELDS.map(([k, l, mn, mx]) => '<div><label class="lbl" for="f-' + k + '">' + l + '</label><input type="number" id="f-' + k + '" name="' + k + '" min="' + mn + '" max="' + mx + '" value="' + s[k] + '"></div>').join("") +
        '<label class="switch full"><input type="checkbox" name="maintenance_mode"' + (s.maintenance_mode ? " checked" : "") + '><span><b>Maintenance mode</b><small>Pause new uploads; existing documents stay available.</small></span></label>' +
        '<div class="full"><label class="lbl" for="f-msg">Maintenance message</label><input type="text" id="f-msg" name="maintenance_message" maxlength="240" value="' + AF.esc(s.maintenance_message || "") + '"></div></div>';
      const env = d.environment;
      $("env").innerHTML = '<dl class="kv"><dt>Caption engine</dt><dd>AutoFigure AI (' + AF.esc(env.ai_provider) + ")" + (env.ai_model ? " · " + AF.esc(env.ai_model) : "") + "</dd><dt>Renderer</dt><dd>" + AF.esc(env.renderer.version || "not installed") + "</dd><dt>Network sandbox</dt><dd>" + (env.renderer.network_sandbox ? "on (no network for the renderer)" : "off") + "</dd><dt>Storage</dt><dd>" + AF.esc(env.storage) + "</dd><dt>Secure cookies</dt><dd>" + (env.secure_cookies ? "on" : "off (development)") + "</dd><dt>Version</dt><dd>" + AF.esc(env.version) + "</dd></dl>";
      $("versions").innerHTML = d.versions.length ? '<ul class="status-list">' + d.versions.map((v) => "<li><span>Version " + v.version + " · " + AF.date(v.created_at) + '</span><button class="btn btn-xs" data-restore="' + v.version + '">Restore</button></li>').join("") + "</ul>" : '<p class="muted">Defaults in use.</p>';
    }));
    $("svc-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = {}; FIELDS.forEach(([k]) => (body[k] = Number(fd.get(k))));
      body.maintenance_mode = fd.has("maintenance_mode"); body.maintenance_message = fd.get("maintenance_message") || "";
      if (body.maintenance_mode && !confirm("Maintenance mode pauses all new uploads. Continue?")) return;
      try { await sensitive(() => AF.api("/api/owner/settings", { method: "PATCH", json: body })); AF.toast("Settings saved and audited.", "ok"); load(); } catch (er) { AF.toast(er.message, "error"); }
    });
    $("versions").addEventListener("click", async (e) => { const b = e.target.closest("[data-restore]"); if (!b) return; try { await sensitive(() => AF.api("/api/owner/settings/restore/" + b.dataset.restore, { method: "POST" })); AF.toast("Restored.", "ok"); load(); } catch (er) { AF.toast(er.message, "error"); } });
    load();
  }

  // ------------------------------------------------------------ security
  if (section === "security") {
    const load = () => guard(AF.api("/api/owner/security").then((d) => {
      $("sessions").innerHTML = '<ul class="status-list">' + d.sessions.map((s) => "<li><span>" + (s.id === d.current_session ? "<b>This session</b> · " : "") + AF.esc((s.user_agent || "").slice(0, 60)) + '<br><span class="xs muted">since ' + AF.date(s.created_at) + " · last seen " + AF.date(s.last_seen_at) + "</span></span>" +
        (s.revoked_at ? '<span class="pill">revoked</span>' : s.id === d.current_session ? "" : '<button class="btn btn-xs" data-revoke="' + s.id + '">Revoke</button>') + "</li>").join("") + "</ul>";
      $("mfa").innerHTML = "<p>" + AF.esc(d.mfa.type) + " · " + d.mfa.recovery_codes_remaining + " recovery codes remaining</p>";
      $("events").innerHTML = d.events.length ? '<div class="table-wrap"><table class="data"><thead><tr><th>Time</th><th>Event</th></tr></thead><tbody>' + d.events.map((e) => "<tr><td>" + AF.date(e.at) + '</td><td class="mono">' + AF.esc(e.kind) + "</td></tr>").join("") + "</tbody></table></div>" : '<p class="muted">No events.</p>';
    }));
    $("sessions").addEventListener("click", async (e) => { const b = e.target.closest("[data-revoke]"); if (!b) return; await AF.api("/api/owner/sessions/" + b.dataset.revoke + "/revoke", { method: "POST" }); load(); });
    $("revoke-others").addEventListener("click", async () => { await AF.api("/api/owner/sessions/revoke-others", { method: "POST" }); AF.toast("Other sessions signed out.", "ok"); load(); });
    let stage = 1;
    $("rotate-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        if (stage === 1) {
          const d = await sensitive(() => AF.api("/api/owner/mfa/rotate", { method: "POST", json: { password: $("rot-pw").value } }));
          $("rot-step").innerHTML = '<div class="qr">' + d.qr_svg + '</div><p class="xs muted">Key: <span class="mono">' + AF.esc(d.secret) + '</span></p><label class="lbl" for="rot-code">Code from the NEW entry</label><input id="rot-code" inputmode="numeric" maxlength="8" required>';
          e.target.querySelector("button").textContent = "Confirm new authenticator"; stage = 2;
        } else {
          const d = await sensitive(() => AF.api("/api/owner/mfa/rotate", { method: "POST", json: { password: $("rot-pw").value, new_totp: $("rot-code").value } }));
          $("rot-step").innerHTML = '<div class="notice ok">' + AF.icon("i-check") + '<span>Authenticator rotated. Other sessions were signed out. New recovery codes:</span></div><div class="codes">' + d.recovery_codes.map((c) => "<span>" + AF.esc(c) + "</span>").join("") + "</div>";
          stage = 1; e.target.querySelector("button").textContent = "Start rotation"; load();
        }
      } catch (er) { AF.toast(er.message, "error"); }
    });
    load();
  }

  // ------------------------------------------------------------ audit
  if (section === "audit") {
    let page = 1;
    const load = () => guard(AF.api("/api/owner/audit?page=" + page).then((d) => {
      $("chain").innerHTML = d.chain_valid ? '<div class="notice ok">' + AF.icon("i-shield") + "<span>Tamper-evident hash chain verified across " + d.chain_length + " events.</span></div>"
        : '<div class="notice err">' + AF.icon("i-alert") + "<span>Audit chain verification FAILED at event #" + d.chain_length + ". Records may have been altered.</span></div>";
      $("audit").innerHTML = d.events.map((e) => '<tr><td class="mono">' + e.id + "</td><td>" + AF.date(e.at) + "</td><td>" + AF.esc(e.actor) + '</td><td class="mono">' + AF.esc(e.action) + "</td><td>" + (e.target_upload_id ? '<a class="mono" href="/owner/uploads/' + e.target_upload_id + '">' + e.target_upload_id.slice(-10) + "</a>" : "") + '</td><td class="xs mono">' + AF.esc(JSON.stringify(e.detail)) + "</td></tr>").join("") || '<tr><td colspan="6"><div class="empty">No events.</div></td></tr>';
      $("apager").innerHTML = '<button class="btn btn-sm" ' + (page <= 1 ? "disabled" : "") + ' data-p="-1">Newer</button><button class="btn btn-sm" ' + (d.events.length < 50 ? "disabled" : "") + ' data-p="1">Older</button>';
    }));
    $("apager").addEventListener("click", (e) => { const b = e.target.closest("[data-p]"); if (b) { page += Number(b.dataset.p); load(); } });
    load();
  }

  // ------------------------------------------------------------ retention
  if (section === "retention") {
    const load = () => guard(AF.api("/api/owner/retention").then((d) => {
      $("ret-policy").textContent = "Uploads, outputs and previews are deleted " + d.retention_days + " days after upload. Sweeps run automatically every 5 minutes.";
      $("upcoming").innerHTML = d.upcoming.length ? '<ul class="status-list">' + d.upcoming.map((u) => '<li><a href="/owner/uploads/' + u.id + '">' + AF.esc(u.original_name) + '</a><span class="muted">' + AF.date(u.retention_expires_at) + "</span></li>").join("") + "</ul>" : '<p class="muted">Nothing retained.</p>';
      $("deleted").innerHTML = d.recently_deleted.length ? '<ul class="status-list">' + d.recently_deleted.map((u) => '<li><a href="/owner/uploads/' + u.id + '">' + AF.esc(u.original_name) + '</a><span class="pill">' + AF.esc(u.deletion_state.replace("_", " ")) + "</span></li>").join("") + "</ul>" : '<p class="muted">None yet.</p>';
      $("orphans").innerHTML = d.orphaned_objects ? '<div class="notice warn">' + AF.icon("i-alert") + "<span>" + d.orphaned_objects + " stored object(s) still exist for deleted uploads. Run the sweep.</span></div>" : '<div class="notice ok">' + AF.icon("i-check") + "<span>No stored content remains for deleted uploads.</span></div>";
    }));
    $("run-sweep").addEventListener("click", async () => { try { const r = await AF.api("/api/owner/retention/run", { method: "POST" }); AF.toast(r.expired + " expired upload(s) removed.", "ok"); load(); } catch (e) { AF.toast(e.message, "error"); } });
    load();
  }
})();
