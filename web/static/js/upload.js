/* Upload card: validation before upload, real byte progress, consent, settings. */
(function () {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const input = document.getElementById("file-input");
  const dz = document.getElementById("dropzone");
  const chip = document.getElementById("file-chip");
  const errBox = document.getElementById("upload-error");
  const status = document.getElementById("upload-status");
  const bar = document.getElementById("upload-progress");
  const submit = document.getElementById("upload-submit");
  let file = null;
  let maxMb = 50;
  let lite = false, chunkBytes = 3 * 1048576;
  AF.api("/api/session").then((s) => { maxMb = s.limits.max_upload_mb; lite = !!s.lite; chunkBytes = s.chunk_bytes || chunkBytes; if (s.maintenance) showError(s.maintenance_message || "Uploads are paused for maintenance."); }).catch(() => {});

  const SIGS = [
    [[0x25, 0x50, 0x44, 0x46], "This is a PDF. Please upload a Microsoft Word (.docx) document."],
    [[0x89, 0x50, 0x4e, 0x47], "This is a PNG image. Please upload a Microsoft Word (.docx) document."],
    [[0xff, 0xd8, 0xff], "This is a JPG image. Please upload a Microsoft Word (.docx) document."],
    [[0xd0, 0xcf, 0x11, 0xe0], "This looks like a legacy .doc or a password-protected file. Save it as .docx (without a password) and try again."],
  ];

  function showError(msg) {
    errBox.innerHTML = AF.icon("i-alert") + "<span></span>";
    errBox.lastChild.textContent = msg;
    errBox.classList.remove("hidden");
    dz.dataset.state = "invalid";
  }
  function clearError() { errBox.classList.add("hidden"); dz.dataset.state = file ? "ready" : "idle"; }

  async function check(f) {
    const name = f.name || "";
    const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    const head = new Uint8Array(await f.slice(0, 8).arrayBuffer());
    for (const [sig, msg] of SIGS) if (sig.every((b, i) => head[i] === b)) return msg;
    if (ext === "docm" || ext === "dotm") return "Macro-enabled Word files are not accepted. Save as .docx without macros.";
    if (ext === "doc") return "Legacy .doc files are not supported. In Word choose File → Save As → Word Document (.docx).";
    if (ext !== "docx") return "Only Microsoft Word .docx files are accepted.";
    if (!(head[0] === 0x50 && head[1] === 0x4b)) return "This file is not a valid Word document (.docx).";
    if (f.size === 0) return "The file is empty.";
    if (f.size > maxMb * 1048576) return "The file is larger than the " + maxMb + " MB limit.";
    return null;
  }

  async function choose(f) {
    file = null; chip.classList.add("hidden");
    if (!f) return;
    const problem = await check(f);
    if (problem) { showError(problem); status.textContent = ""; input.value = ""; return; }
    file = f; clearError();
    document.getElementById("file-name").textContent = f.name;
    document.getElementById("file-meta").textContent = AF.bytes(f.size) + " · Word document · ready to upload (not uploaded yet)";
    chip.classList.remove("hidden");
    dz.dataset.state = "ready";
    status.textContent = "Selected " + f.name;
  }

  input.addEventListener("change", () => choose(input.files[0]));
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.dataset.state = "over"; }));
  ["dragleave", "dragend"].forEach((ev) => dz.addEventListener(ev, () => { dz.dataset.state = file ? "ready" : "idle"; }));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    const items = e.dataTransfer.items;
    if (items && items[0] && items[0].webkitGetAsEntry && items[0].webkitGetAsEntry() && items[0].webkitGetAsEntry().isDirectory) {
      showError("That is a folder. Drop a single .docx file."); return;
    }
    if (e.dataTransfer.files.length > 1) { showError("Drop one Word document at a time."); return; }
    choose(e.dataTransfer.files[0]);
  });
  document.getElementById("file-remove").addEventListener("click", () => { file = null; input.value = ""; chip.classList.add("hidden"); clearError(); input.click(); });

  function settings() {
    const fd = new FormData(form);
    const s = {};
    ["language", "tone", "length", "existing_mode", "number_style"].forEach((k) => { if (fd.get(k)) s[k] = fd.get(k); });
    const prefix = (fd.get("prefix") || "Figure").trim();
    if (prefix) s.prefix = prefix;
    s.include_decorative = !!fd.get("include_decorative");
    return s;
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!file) { showError("Choose a Word (.docx) file first."); input.focus(); return; }
    if (!document.getElementById("consent").checked) { showError("Please tick “I agree to the privacy terms” to continue."); document.getElementById("consent").focus(); return; }
    if (lite) { chunkedUpload(); return; }
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("consent", "yes");
    fd.append("settings", JSON.stringify(settings()));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.setRequestHeader("X-CSRF-Token", AF.csrf());
    bar.classList.remove("hidden");
    AF.busy(submit, true, "Uploading…");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        const pct = Math.round((ev.loaded / ev.total) * 100);
        bar.firstElementChild.style.width = pct + "%";
        status.textContent = "Uploading " + pct + "% (" + AF.bytes(ev.loaded) + " of " + AF.bytes(ev.total) + ")";
      }
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (err) {}
      if (xhr.status === 202 && data) {
        status.textContent = "Upload stored securely. Opening your document…";
        location.href = data.review_url;
      } else {
        AF.busy(submit, false);
        bar.classList.add("hidden");
        status.textContent = "";
        showError((data && data.error && data.error.message) || "Upload failed. Please try again.");
      }
    };
    xhr.onerror = () => { AF.busy(submit, false); bar.classList.add("hidden"); showError("Network problem — your file was not uploaded. Try again."); };
    xhr.send(fd);
  });

  async function chunkedUpload() {
    bar.classList.remove("hidden");
    AF.busy(submit, true, "Uploading…");
    try {
      const token = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^a-z0-9-]/gi, "");
      const count = Math.max(1, Math.ceil(file.size / chunkBytes));
      for (let i = 0; i < count; i++) {
        const part = file.slice(i * chunkBytes, (i + 1) * chunkBytes);
        const res = await fetch("/api/uploads/chunk", { method: "POST", credentials: "same-origin", body: part,
          headers: { "X-CSRF-Token": AF.csrf(), "X-Upload-Token": token, "X-Chunk-Index": String(i), "Content-Type": "application/octet-stream" } });
        if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error((d.error && d.error.message) || "Upload failed."); }
        const done = Math.min(file.size, (i + 1) * chunkBytes);
        bar.firstElementChild.style.width = Math.round((done / file.size) * 100) + "%";
        status.textContent = "Uploading " + Math.round((done / file.size) * 100) + "% (" + AF.bytes(done) + " of " + AF.bytes(file.size) + ")";
      }
      const fd = new FormData();
      fd.append("chunk_token", token); fd.append("chunk_count", String(count)); fd.append("filename", file.name);
      fd.append("consent", "yes"); fd.append("settings", JSON.stringify(settings()));
      status.textContent = "Checking your Word file…";
      const res = await fetch("/api/uploads", { method: "POST", credentials: "same-origin", body: fd, headers: { "X-CSRF-Token": AF.csrf() } });
      const data = await res.json().catch(() => null);
      if (res.status !== 202 || !data) throw new Error((data && data.error && data.error.message) || "Upload failed. Please try again.");
      status.textContent = "Upload stored securely. Opening your document…";
      location.href = data.review_url;
    } catch (err) {
      AF.busy(submit, false); bar.classList.add("hidden"); status.textContent = ""; showError(err.message);
    }
  }

  // move documents to another device
  const tb = document.getElementById("transfer-btn");
  if (tb) {
    const dlg = document.getElementById("transfer-dialog");
    tb.addEventListener("click", async () => {
      try {
        const d = await AF.api("/api/session/transfer", { method: "POST" });
        document.getElementById("transfer-qr").innerHTML = d.qr_svg;
        document.getElementById("transfer-url").value = d.url;
        dlg.showModal();
      } catch (e) { AF.toast(e.message, "error"); }
    });
    document.getElementById("transfer-copy").addEventListener("click", () => {
      const u = document.getElementById("transfer-url");
      (navigator.clipboard ? navigator.clipboard.writeText(u.value) : Promise.reject()).then(() => AF.toast("Link copied.", "ok")).catch(() => { u.select(); });
    });
    document.getElementById("transfer-close").addEventListener("click", () => dlg.close());
  }

  // workspace list
  const list = document.getElementById("my-uploads");
  if (list) {
    const LABEL = { review_ready: ["Ready for review", "pill-success"], failed: ["Failed", "pill-error"] };
    const load = () => AF.api("/api/uploads").then((d) => {
      document.getElementById("transfer-btn")?.classList.toggle("hidden", !d.uploads.length);
      if (!d.uploads.length) { list.innerHTML = '<div class="empty" style="padding:22px 10px">' + AF.icon("i-file") + '<p style="margin:0">No documents yet.<br><span class="xs">Your uploads appear here so you can return to them.</span></p></div>'; return; }
      list.innerHTML = '<ul class="fig-list">' + d.uploads.map((u) => {
        const [lab, cls] = LABEL[u.state] || ["Processing", "pill-accent"];
        return '<li class="fig-card" style="grid-template-columns:36px 1fr auto;cursor:default"><div class="doc-badge" style="width:30px;height:36px">W</div>' +
          '<div style="min-width:0"><a href="/app/jobs/' + AF.esc(u.job_id) + '" style="font-weight:650;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + AF.esc(u.original_name) + '</a>' +
          '<span class="fmeta">' + AF.date(u.created_at) + ' · ' + AF.bytes(u.byte_size) + ' · deleted ' + AF.dateOnly(u.retention_expires_at) + '</span></div>' +
          '<div class="row" style="gap:6px"><span class="pill ' + cls + '">' + lab + '</span><button class="icon-btn" data-del="' + AF.esc(u.id) + '" aria-label="Delete ' + AF.esc(u.original_name) + '">' + AF.icon("i-trash") + '</button></div></li>';
      }).join("") + "</ul>";
    }).catch(() => { list.innerHTML = '<p class="small muted">Could not load your documents.</p>'; });
    list.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-del]");
      if (!b) return;
      if (!confirm("Delete this upload and every generated file now? This cannot be undone.")) return;
      try { await AF.api("/api/uploads/" + b.dataset.del, { method: "DELETE" }); AF.toast("Your files were deleted.", "ok"); load(); }
      catch (err) { AF.toast(err.message, "error"); }
    });
    load();
  }
})();
