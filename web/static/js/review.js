/* Review workspace: real job state, true preview pages, figure inspector, caption editing, apply + download. */
(function () {
  const root = document.getElementById("review-app");
  if (!root) return;
  const JOB = root.dataset.job;
  const API = "/api/jobs/" + JOB;
  const $ = (id) => document.getElementById(id);
  const S = {
    job: null, figures: [], preview: null, source: null, view: "output", zoom: 1, fit: "width",
    selected: null, filter: "all", pollTimer: null, lastReady: null, dirty: false, tab: "figures",
  };
  const store = {
    get(k) { try { return JSON.parse(sessionStorage.getItem("afs:" + JOB + ":" + k)); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem("afs:" + JOB + ":" + k, JSON.stringify(v)); } catch (e) {} },
  };
  S.selected = store.get("selected");
  S.zoom = store.get("zoom") || 1;

  const STATUS_PILL = {
    insert: "pill-success", renumber: "pill-violet", replace: "pill-violet", preserve: "pill-success",
    skip_existing: "", excluded_first_page: "pill-violet", excluded_page_uncertain: "pill-warning",
    excluded_decorative: "", excluded_unassigned: "pill-warning", excluded_unsupported: "pill-warning", excluded_manual: "",
  };

  // Serverless hosts (Vercel) have no background worker: the page asks the server to run this job's work.
  S.lite = false;
  const sessionReady = AF.api("/api/session").then((s) => { S.lite = !!s.lite; }).catch(() => {});
  function kick() {
    if (!S.lite || S.kicking) return;
    S.kicking = true;
    AF.api(API + "/process", { method: "POST" }).catch(() => {}).finally(() => { S.kicking = false; poll(); });
  }

  // ------------------------------------------------------------------ job polling
  async function poll() {
    await sessionReady;
    clearTimeout(S.pollTimer);
    let job;
    try { job = await AF.api(API); }
    catch (e) {
      if (e.status === 404) { show("gone"); return; }
      S.pollTimer = setTimeout(poll, 4000);
      return;
    }
    S.job = job;
    header(job);
    if (job.state === "review_ready" || job.state === "regenerating") {
      const first = $("review-ui").classList.contains("hidden");
      show("review-ui");
      if (first || job.ready_revision?.revision !== S.lastReady) {
        S.lastReady = job.ready_revision?.revision;
        await loadFigures();
        await loadPreview();
        if (!first && job.state === "review_ready") AF.toast("Preview updated to revision " + S.lastReady + ".", "ok");
      }
      renderSidebar();
      renderDownload();
      fitGrid();
      if (job.needs_browser_ocr && job.state === "review_ready") runBrowserOcr();
      if (job.failure && job.failure.regeneration_only && !S.warnedRegen) {
        S.warnedRegen = true;
        AF.toast("Your last changes could not be applied: " + job.failure.message + " The previous verified version is still available.", "error", 9000);
      }
      if (job.state === "regenerating") { kick(); S.pollTimer = setTimeout(poll, 1500); }
    } else if (["failed", "canceled", "expired", "deleted"].includes(job.state)) {
      show("processing");
      timeline(job);
    } else {
      show("processing");
      timeline(job);
      kick();
      S.pollTimer = setTimeout(poll, 1200);
    }
  }

  function show(id) {
    ["processing", "review-ui", "gone"].forEach((x) => $(x).classList.toggle("hidden", x !== id));
    if (id === "review-ui") fitGrid();
    if (id === "gone") { $("doc-name").textContent = "Document not available"; $("doc-meta").textContent = ""; $("stepper").classList.add("hidden"); }
  }
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => fitGrid());
    ["review-ui", "mistakes"].forEach((id) => $(id) && ro.observe($(id)));
    document.querySelector(".ws-head") && ro.observe(document.querySelector(".ws-head"));
    document.querySelector(".stats-wrap") && ro.observe(document.querySelector(".stats-wrap"));
  }
  function fitGrid() {
    const g = $("review-grid");
    const top = g.getBoundingClientRect().top + window.scrollY;
    document.documentElement.style.setProperty("--review-top", Math.round(top) + "px");
  }

  function header(job) {
    $("doc-name").textContent = job.file.name;
    $("doc-meta").textContent = AF.bytes(job.file.byte_size) + (job.page_count ? " · " + job.page_count + " pages" : "") +
      " · uploaded " + AF.date(job.created_at) + " · auto-deleted " + AF.dateOnly(job.file.retention_expires_at);
    document.title = job.file.name + " · AutoFigure Studio";
    const st = job.state;
    const steps = [
      "done",
      st === "review_ready" || st === "regenerating" ? "done" : st === "failed" ? "failed" : "active",
      st === "review_ready" || st === "regenerating" ? "active" : "",
      job.ready_revision ? (st === "review_ready" ? "active" : "") : "",
    ];
    if (steps[3] === "active") steps[2] = "done";
    const reviewing = st === "review_ready" || st === "regenerating";
    $("stepper").classList.toggle("hidden", reviewing);
    $("ws-actions").classList.toggle("hidden", !reviewing);
    $("stepper").querySelectorAll("li").forEach((li, i) => {
      li.dataset.state = steps[i] || "";
      li.querySelector("i").innerHTML = steps[i] === "done" ? AF.icon("i-check", "icon") : String(i + 1);
      li.querySelector("i svg")?.setAttribute("style", "width:12px;height:12px");
    });
  }

  function timeline(job) {
    const done = job.steps.filter((x) => x.state === "done").length;
    const pct = Math.round((done / job.steps.length) * 100);
    $("proc-pct").textContent = pct + "%";
    $("proc-fill").style.width = Math.max(4, pct) + "%";
    const cur = job.steps.find((x) => x.state === "active" || x.state === "failed");
    $("proc-title").textContent = job.state === "failed" ? "Processing stopped" : cur ? cur.label + "…" : "Preparing your document…";
    $("timeline").innerHTML = job.steps.map((s) =>
      '<li data-state="' + s.state + '"><span class="dot">' + (s.state === "done" ? AF.icon("i-check", "icon") : s.state === "failed" ? AF.icon("i-x", "icon") : "") +
      '</span><span>' + AF.esc(s.label) + '</span><span class="xs muted">' + (s.state === "active" ? "in progress" : s.state === "done" ? "done" : s.state === "failed" ? "failed" : "") + "</span></li>").join("");
    if (job.queue_position) $("proc-sub").textContent = "Waiting in queue (" + job.queue_position + " ahead). You can close this tab and come back.";
    const fb = $("proc-failure");
    if (job.failure && !job.failure.regeneration_only) {
      fb.classList.remove("hidden");
      fb.innerHTML = '<div class="notice err" role="alert">' + AF.icon("i-alert") + '<div><b>' + AF.esc(job.failure.message) + '</b><br>' +
        AF.esc(job.failure.preserved) + " " + AF.esc(job.failure.next_step) + '<br><span class="xs muted">Support ID: <span class="mono">' + AF.esc(job.failure.support_id) + "</span></span></div></div>" +
        '<div class="row" style="margin-top:14px"><a class="btn btn-primary" href="/app">Try again</a><a class="btn" href="/help">View problem help</a></div>';
    } else if (["expired", "deleted"].includes(job.state)) {
      show("gone");
    }
  }

  // ------------------------------------------------------------------ data
  async function loadFigures() {
    const d = await AF.api(API + "/figures");
    S.figures = d.figures;
    if (S.selected && !S.figures.find((f) => f.figure_id === S.selected)) S.selected = null;
  }
  async function loadPreview() {
    const [out, src] = await Promise.all([
      AF.api(API + "/preview?kind=output"),
      S.source ? Promise.resolve(S.source) : AF.api(API + "/preview?kind=source").catch(() => null),
    ]);
    S.preview = out; S.source = src;
    renderPages();
    renderThumbs();
  }

  // ------------------------------------------------------------------ summary + sidebar
  function counts() {
    const c = {};
    S.figures.forEach((f) => { c[f.status] = (c[f.status] || 0) + 1; });
    return c;
  }
  function renderSidebar() {
    const c = counts();
    const inserted = (c.insert || 0) + (c.replace || 0);
    const kept = (c.preserve || 0) + (c.skip_existing || 0);
    const corrected = c.renumber || 0;
    const review = S.figures.filter((f) => f.needs_review && f.eligible).length;
    const pending = ["excluded_page_uncertain", "excluded_unassigned", "excluded_unsupported"].reduce((a, k) => a + (c[k] || 0), 0);
    const mistakes = S.figures.filter((f) => (f.mistakes || []).length).length;
    const captioned = inserted + (c.renumber || 0) + kept;
    const tiles = [
      { n: S.figures.length, label: S.figures.length === 1 ? "image found" : "images found", icon: "i-layers", tone: "", filter: "all" },
      { n: captioned, label: "captioned", icon: "i-check", tone: "ok", filter: "all" },
      { n: c.excluded_first_page || 0, label: "on cover page (skipped)", icon: "i-shield", tone: "violet", filter: "excluded" },
      { n: review + pending, label: "to check", icon: "i-alert", tone: review + pending ? "warn" : "", filter: "review" },
    ];
    if (mistakes) tiles.push({ n: mistakes, label: mistakes === 1 ? "mistake" : "mistakes", icon: "i-refresh", tone: "err", filter: "mistakes" });
    renderNextStep({ captioned, review: review + pending, mistakes });
    const mode = { lo: "Numbered by LO", heading1: "Numbered by Heading 1", document: "Numbered 1, 2, 3" }[S.job.summary.mode] || "";
    $("report").innerHTML = tiles.filter((t, i) => i === 0 || t.n).map((t) => '<button type="button" class="stat ' + t.tone + '" data-stat="' + t.filter + '">' +
      '<span class="stat-ic">' + AF.icon(t.icon) + '</span><span><b class="tnum">' + t.n + "</b><small>" + AF.esc(t.label) + "</small></span></button>").join("") +
      (mode ? '<span class="stat-mode">' + AF.icon("i-hash") + AF.esc(mode) + "</span>" : "");
    // outline
    const chapters = (S.job.summary.chapters || []);
    const groups = chapters.map((ch) => ({ key: ch.key, title: ch.title, number: ch.number, n: S.figures.filter((f) => f.eligible && f.chapter_number === ch.number).length }));
    const unassigned = S.figures.filter((f) => f.status === "excluded_unassigned").length;
    $("outline").innerHTML = groups.map((g) => '<li><button type="button" data-chapter="' + g.number + '"><span>' + AF.esc(g.title) + '</span><span class="count">' + g.n + "</span></button></li>").join("") +
      (unassigned ? '<li><button type="button" data-filter-go="excluded"><span>Front matter / Unassigned</span><span class="count">' + unassigned + "</span></button></li>" : "") +
      (!groups.length ? '<li class="small muted">No section headings detected.</li>' : "");
    // issues
    const issues = [];
    const w = (S.job.summary.warnings || []).concat(S.job.summary.analysis_warnings || []);
    const ISSUE = {
      document_wide_numbering: "No Heading 1 / LO headings found — numbering runs across the whole document. Map sections in Settings if needed.",
      headings_from_text_pattern: "LO headings were detected from bold text, not Word Heading 1 styles. Check the chapter mapping.",
      lo_numbers_out_of_order: "LO numbers are out of order in the document.",
      probe_page_count_differs: "Page mapping could not be fully verified; some figures need confirmation.",
      tracked_changes_present: "The document contains tracked changes. Captions are added as normal text.",
      external_links_present: "The document links to external content; it was not fetched.",
    };
    w.forEach((k) => { const key = String(k).split(":")[0]; if (ISSUE[key] && !issues.includes(ISSUE[key])) issues.push(ISSUE[key]); });
    if (S.job.summary.page_method === "explicit_breaks_only" && !S.lite) issues.push("The renderer was unavailable, so page 1 could not be verified. Figures need confirmation.");
    if (S.lite) issues.push("This server draws the preview in your browser, so page breaks can differ slightly from Microsoft Word. The captions, their text and positions are exactly what you download. Cover-page images are detected from Word's saved page layout and page breaks; check any marked “confirm”.");
    if (S.preview && S.preview.missing_highlights && S.preview.missing_highlights.length) issues.push(S.preview.missing_highlights.length + " caption(s) could not be located in the rendered preview; check them on the pages.");
    if (!S.job.vision_check && S.figures.some((f) => f.existing && f.eligible))
      issues.push("Existing caption descriptions were not compared with the images, because no vision model (Claude or ChatGPT) is configured on this server. Numbering and label mistakes were still checked.");
    if (c.excluded_unassigned) issues.push(c.excluded_unassigned + " figure(s) appear before the first section heading. Assign a section (Caption tab) or change “Figures before the first section” in Settings.");
    if (c.excluded_page_uncertain) issues.push(c.excluded_page_uncertain + " figure(s) could not be verified as not on page 1. Open them and confirm.");
    if (c.excluded_unsupported) issues.push(c.excluded_unsupported + " figure(s) are in text boxes, SmartArt or drawing canvases and were not edited automatically.");
    if (S.figures.some((f) => (f.warnings || []).includes("floating_image") && f.eligible)) issues.push("Some figures are floating images. Check their caption position in the preview.");
    $("checks-count").textContent = String(issues.length);
    $("checks").classList.toggle("has-items", issues.length > 0);
    $("issues").innerHTML = issues.length ? issues.map((t) => '<div class="notice warn" style="margin-bottom:8px">' + AF.icon("i-alert") + "<span>" + AF.esc(t) + "</span></div>").join("")
      : '<div class="notice ok">' + AF.icon("i-check") + "<span>No structural issues detected.</span></div>";
    renderMistakes();
    renderFigureList();
    if (S.tab === "settings") renderSettings();
    if (S.tab === "edit") renderEditor();
  }

  // One plain-language sentence that always says what to do next.
  function renderNextStep(n) {
    if (n) S.ns = n; else n = S.ns;
    if (!n || !S.job) return;
    const box = $("next-step");
    const busy = S.job.state === "regenerating";
    let tone = "ok", icon = "i-check", title, body, action = "";
    if (busy) {
      tone = "info"; icon = "i-refresh";
      title = "Updating your file…";
      body = "We are rebuilding the Word file and preview with your changes. This takes a few seconds.";
    } else if (n.mistakes) {
      tone = "err"; icon = "i-alert";
      title = n.mistakes === 1 ? "1 caption in your file has a mistake" : n.mistakes + " captions in your file have mistakes";
      body = "Check each suggested fix below, then press <b>Correct all</b> — or correct them one by one.";
      action = '<button class="btn btn-primary btn-sm" type="button" id="correct-all-top">' + AF.icon("i-sparkle") + "Correct all</button>";
    } else if (n.review) {
      tone = "warn"; icon = "i-alert";
      title = n.review === 1 ? "1 figure needs a quick check" : n.review + " figures need a quick check";
      body = "We were not fully sure about these. Open each one, confirm or edit the caption, then download.";
      action = '<button class="btn btn-sm" type="button" data-stat="review">Show them</button>';
    } else if (S.job.has_unrendered_changes) {
      tone = "warn"; icon = "i-refresh";
      title = "You have changes that are not in the file yet";
      body = "Press <b>Apply changes</b> to rebuild the Word file with them.";
      action = '<button class="btn btn-primary btn-sm" type="button" data-apply>' + AF.icon("i-refresh") + "Apply changes</button>";
    } else if (!S.figures.length) {
      tone = "info"; icon = "i-file";
      title = "No pictures were found in this document";
      body = "There is nothing to caption. Upload a different Word file if this is not what you expected.";
    } else {
      title = "All done — your figures are captioned";
      body = n.captioned + (n.captioned === 1 ? " caption is" : " captions are") + " in place and verified. Check the preview, then download your Word file.";
      action = '<button class="btn btn-primary btn-sm" type="button" data-download>' + AF.icon("i-download") + "Download</button>";
    }
    box.className = "next-step " + tone;
    box.innerHTML = '<span class="ns-ic">' + AF.icon(icon) + '</span><div class="grow"><b>' + AF.esc(title) + "</b><div class=\"small\">" + body + "</div></div>" + action +
      (n.mistakes && !busy && S.mistakeHtml ? '<div class="ns-fixes">' + S.mistakeHtml + "</div>" : "");
  }

  function renderMistakes() {
    const box = $("mistakes");
    const prefix = S.job.settings.prefix || "Figure";
    const sep = S.job.settings.separator || " ";
    const list = S.figures.filter((f) => (f.mistakes || []).length);
    if (!list.length) { box.classList.add("hidden"); box.innerHTML = ""; S.mistakeHtml = ""; return; }
    const auto = (f) => f.status === "renumber" && !f.mistakes.some((m) => m.kind === "description");
    box.classList.add("hidden");
    S.mistakeHtml = '<ul class="mistake-list">' + list.map((f) => {
        const desc = f.mistakes.find((m) => m.kind === "description");
        const fixedText = prefix + " " + (f.label || "?") + sep + (desc ? desc.suggested : (f.existing ? f.existing.description : f.caption_text));
        return '<li class="mistake">' + (f.thumb_url ? '<img alt="" src="' + f.thumb_url + '">' : '<span class="fthumb"></span>') +
          '<div style="min-width:0"><div class="was">' + AF.esc(f.mistakes[0].found) + "</div>" +
          '<div class="now">→ ' + AF.esc(fixedText) + "</div>" +
          '<div class="why">' + f.mistakes.map((m) => AF.esc(m.message)).join(" · ") + (auto(f) ? " · number already fixed in the output" : "") + "</div></div>" +
          '<div class="row" style="gap:6px"><button class="btn btn-xs" type="button" data-show="' + f.figure_id + '">' + AF.icon("i-eye") + "Show</button>" +
          '<button class="btn btn-xs btn-primary" type="button" data-correct="' + f.figure_id + '">Correct</button>' +
          (desc ? '<button class="btn btn-xs btn-ghost" type="button" data-keep="' + f.figure_id + '">Keep mine</button>' : "") + "</div></li>";
      }).join("") + "</ul>";
    renderNextStep();
  }

  async function correct(ids, dismiss) {
    try {
      const d = await AF.api(API + "/correct", { method: "POST", json: { figure_ids: ids, dismiss: !!dismiss } });
      if (d.regenerating) {
        AF.toast(d.corrected + " caption(s) corrected. Regenerating the Word file and preview…", "ok");
        S.job.state = "regenerating"; renderDownload(); kick(); setTimeout(poll, 800);
      } else { AF.toast("Kept your original caption.", "ok"); }
      await loadFigures(); renderSidebar();
    } catch (e) { AF.toast(e.message, "error"); }
  }

  // Serverless hosts have no OCR engine: read the text inside figures here, in the visitor's browser.
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const el = document.createElement("script");
      el.src = src; el.onload = resolve; el.onerror = () => reject(new Error("Could not load " + src));
      document.head.appendChild(el);
    });
  }
  // Tesseract reads small UI text and light-on-colour labels far better when the image is enlarged,
  // grey-scaled and contrast-stretched first.
  function prepareForOcr(bmp) {
    const scale = Math.max(1, Math.min(2, 2400 / Math.max(bmp.width, bmp.height)));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(bmp, 0, 0, c.width, c.height);
    const img = ctx.getImageData(0, 0, c.width, c.height);
    const d = img.data, hist = new Uint32Array(256);
    for (let i = 0; i < d.length; i += 4) {
      const g = (d[i] * 299 + d[i + 1] * 587 + d[i + 2] * 114) / 1000 | 0;
      d[i] = g; hist[g]++;
    }
    const n = d.length / 4;
    let lo = 0, hi = 255, acc = 0;
    while (lo < 255 && (acc += hist[lo]) < n * 0.01) lo++;
    acc = 0;
    while (hi > 0 && (acc += hist[hi]) < n * 0.01) hi--;
    const span = Math.max(1, hi - lo);
    for (let i = 0; i < d.length; i += 4) {
      const v = Math.max(0, Math.min(255, ((d[i] - lo) * 255) / span));
      d[i] = d[i + 1] = d[i + 2] = v;
    }
    ctx.putImageData(img, 0, 0);
    return c;
  }

  async function runBrowserOcr() {
    if (S.ocrStarted) return;
    S.ocrStarted = true;
    const targets = S.figures.filter((f) => f.content_type === "picture" && f.thumb_url && f.status !== "excluded_first_page" &&
      f.status !== "excluded_decorative" && f.caption_source !== "manual");
    const banner = $("ocr-banner");
    const show = (i, n) => { $("ocr-count").textContent = i + " / " + n; $("ocr-fill").style.width = Math.round((i / Math.max(1, n)) * 100) + "%"; };
    const results = [];
    let failed = false;
    if (targets.length) {
      banner.classList.remove("hidden"); fitGrid(); show(0, targets.length);
      try {
        await loadScript("/static/vendor/tesseract/tesseract.min.js");
        const worker = await window.Tesseract.createWorker("eng", 1, {
          workerPath: "/static/vendor/tesseract/worker.min.js", corePath: "/static/vendor/tesseract",
          langPath: "/static/vendor/tesseract/lang", workerBlobURL: false, gzip: true });
        for (let i = 0; i < targets.length; i++) {
          const f = targets[i];
          const res = await fetch(API + "/figures/" + f.figure_id + "/image", { credentials: "same-origin" });
          if (!res.ok) { show(i + 1, targets.length); continue; }
          const canvas = prepareForOcr(await createImageBitmap(await res.blob()));
          const { data } = await worker.recognize(canvas, {}, { blocks: true });
          const lines = [];
          (data.blocks || []).forEach((b) => (b.paragraphs || []).forEach((p) => (p.lines || []).forEach((l) => {
            const t = (l.text || "").trim();
            if (t) lines.push({ text: t, x: l.bbox.x0 / canvas.width, y: l.bbox.y0 / canvas.height, h: (l.bbox.y1 - l.bbox.y0) / canvas.height, conf: l.confidence });
          })));
          results.push({ figure_id: f.figure_id, lines: lines.slice(0, 400) });
          show(i + 1, targets.length);
        }
        await worker.terminate();
      } catch (e) {
        failed = true;
        console.warn("Text recognition unavailable in this browser:", e && e.message);
      }
    }
    try {
      const d = await AF.api(API + "/ocr", { method: "POST", json: { results, failed } });
      banner.classList.add("hidden"); fitGrid();
      if (d.updated || d.mistakes) {
        AF.toast((d.updated ? d.updated + " caption(s) improved using text found in your images. " : "") +
          (d.mistakes ? d.mistakes + " mistake(s) found in existing captions." : ""), "ok", 6000);
      }
      if (d.regenerating) { S.job.state = "regenerating"; renderDownload(); kick(); setTimeout(poll, 800); }
      else { await loadFigures(); renderSidebar(); }
    } catch (e) { banner.classList.add("hidden"); fitGrid(); }
  }

  function figTitle(f) {
    return f.label ? (S.job.settings.prefix || "Figure") + " " + f.label : "Image " + f.order;
  }

  function renderFigureList() {
    const list = S.figures.filter((f) => S.filter === "all" ? true : S.filter === "review" ? (f.needs_review && f.eligible) || f.status === "excluded_page_uncertain" || f.status === "excluded_unassigned" : !f.eligible);
    if (!list.length) {
      $("fig-list").innerHTML = '<li class="empty small">' + (S.figures.length ? "Nothing in this filter." : "No images eligible for automatic captions. Images on page 1 are never captioned; this document may only contain cover images or text.") + "</li>";
      return;
    }
    $("fig-list").innerHTML = list.map((f) => {
      const meta = ["Page " + (f.page ?? "?"), f.chapter_title ? AF.esc(shortTitle(f.chapter_title)) : null].filter(Boolean).join(" · ");
      const flags = (f.needs_review && f.eligible ? '<span class="pill pill-warning">' + AF.icon("i-alert") + "Review</span> " : "") +
        '<span class="pill ' + (STATUS_PILL[f.status] || "") + '">' + AF.esc(f.status_label) + "</span>";
      const tone = (f.mistakes || []).length ? " st-err" : (f.needs_review && f.eligible) || ["excluded_page_uncertain", "excluded_unassigned"].includes(f.status) ? " st-warn" : f.eligible ? " st-ok" : "";
      return '<li><button type="button" class="fig-card' + tone + (f.eligible ? "" : " excluded") + '" data-fig="' + f.figure_id + '" aria-current="' + (f.figure_id === S.selected) + '">' +
        (f.thumb_url ? '<img class="fthumb" loading="lazy" alt="" src="' + f.thumb_url + '">' : '<span class="fthumb" style="display:grid;place-items:center">' + AF.icon("i-file") + "</span>") +
        '<span style="min-width:0"><span class="row between" style="gap:6px"><span class="flabel">' + AF.esc(figTitle(f)) + '</span></span><span class="fmeta">' + meta + "</span>" +
        (f.eligible ? '<span class="fcap">' + AF.esc(f.caption_text || "—") + "</span>" : '<span class="fcap muted">' + AF.esc(f.reason) + "</span>") +
        '<span class="row" style="gap:4px;margin-top:4px">' + flags + "</span></span></button></li>";
    }).join("");
  }
  const shortTitle = (t) => (t.length > 34 ? t.slice(0, 33) + "…" : t);

  // ------------------------------------------------------------------ viewer
  function pageWidth(p) {
    const box = $("pages");
    const avail = Math.max(240, box.clientWidth - 48) / (S.view === "compare" ? 2.08 : 1);
    const base = S.fit === "page" ? Math.min(avail, ((box.clientHeight - 48) * p.w) / p.h) : S.fit === "width" ? Math.min(avail, 900) : (p.w * 96) / 72;
    return Math.max(160, Math.round(base * S.zoom));
  }

  function renderPages() {
    const box = $("pages");
    box.innerHTML = "";
    const cols = S.view === "compare" ? [["source", S.source, "Original"], ["output", S.preview, "Processed (revision " + (S.preview?.revision || "–") + ")"]]
      : [[S.view, S.view === "source" ? S.source : S.preview, null]];
    let total = 0;
    cols.forEach(([kind, man, title]) => {
      const col = document.createElement("div");
      col.className = "page-col";
      if (title) col.innerHTML = "<h4>" + AF.esc(title) + "</h4>";
      if (man && man.state === "client") {
        const holder = document.createElement("div");
        holder.className = "client-doc";
        holder.innerHTML = '<div class="page" style="--pw:720px"><div class="ph" style="--ar:0.77"></div></div>';
        col.appendChild(holder);
        box.appendChild(col);
        renderClient(kind, man, holder);
        return;
      }
      if (!man || man.state !== "ready") {
        col.innerHTML += '<div class="notice warn" style="max-width:520px">' + AF.icon("i-alert") + "<span>" +
          (kind === "source" ? "The original preview is not available for this document." : "The preview for this revision is not ready. Apply changes again or try later.") + "</span></div>";
        box.appendChild(col);
        return;
      }
      total = Math.max(total, man.page_count);
      man.pages.forEach((p) => {
        const el = document.createElement("div");
        el.className = "page";
        el.dataset.n = p.n; el.dataset.kind = kind;
        el.style.setProperty("--pw", pageWidth(p) + "px");
        el.innerHTML = '<div class="ph" style="--ar:' + (p.w / p.h).toFixed(4) + '"></div><span class="pnum">' + p.n + " / " + man.page_count + "</span>";
        el.dataset.src = p.src;
        col.appendChild(el);
      });
      box.appendChild(col);
    });
    $("page-total").textContent = "/ " + (total || "–");
    $("page-jump").max = total || 1;
    $("zoom-label").textContent = Math.round(S.zoom * 100) + "%";
    lazy();
    const savedPage = store.get("page");
    if (savedPage && !S.didRestore) { S.didRestore = true; setTimeout(() => gotoPage(savedPage, false), 50); }
  }

  const blobCache = {};
  async function renderClient(kind, man, holder) {
    try {
      blobCache[man.file] = blobCache[man.file] || AF.fetchChunked(man.file);
      const sections = await AF.renderDocx(await blobCache[man.file], holder);
      const avail = Math.max(240, $("pages").clientWidth - 48) / (S.view === "compare" ? 2.08 : 1);
      const w = sections[0] ? sections[0].offsetWidth : 800;
      const fit = S.fit === "page" ? Math.min(avail / w, ($("pages").clientHeight - 48) / ((sections[0] && sections[0].offsetHeight) || 1100)) : Math.min(1.2, avail / w);
      holder.style.zoom = String(Math.max(0.2, fit * S.zoom));
      sections.forEach((sec, i) => { sec.dataset.n = i + 1; sec.dataset.kind = kind; });
      man.page_count = sections.length;
      man.texts = sections.map((sec) => sec.textContent.replace(/\s+/g, " "));
      const total = Math.max(...[...document.querySelectorAll("#pages [data-kind]")].map((x) => Number(x.dataset.n)), 1);
      $("page-total").textContent = "/ " + total; $("page-jump").max = total;
      if (kind === "output") {
        const prefix = S.job.settings.prefix || "Figure";
        const want = Object.entries(man.labels || {}).map(([fid, lab]) => [fid, prefix + " " + lab]);
        holder.querySelectorAll("p").forEach((p) => {
          const t = p.textContent.replace(/\s+/g, " ").trim();
          const hit = want.find(([, key]) => t === key || (t.startsWith(key) && /^[\s:.\u2013\u2014]/.test(t.slice(key.length))));
          if (hit) {
            const f = S.figures.find((x) => x.figure_id === hit[0]);
            p.classList.add("hl-client"); p.dataset.fig = hit[0];
            p.dataset.selected = String(hit[0] === S.selected); p.dataset.review = String(!!(f && f.needs_review));
            p.setAttribute("role", "button"); p.tabIndex = 0;
          }
        });
      }
      const savedPage = store.get("page");
      if (savedPage && !S.didRestore) { S.didRestore = true; setTimeout(() => gotoPage(savedPage, false), 50); }
    } catch (e) {
      holder.innerHTML = '<div class="notice err" style="max-width:520px">' + AF.icon("i-alert") + "<span>The preview could not be drawn: " + AF.esc(e.message) + " You can still download the Word file.</span></div>";
    }
  }

  let io;
  function lazy() {
    if (io) io.disconnect();
    io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        const el = en.target;
        if (en.isIntersecting && !el.dataset.loaded) {
          el.dataset.loaded = "1";
          const img = new Image();
          img.alt = "Page " + el.dataset.n + " of the " + (el.dataset.kind === "source" ? "original" : "processed") + " document";
          img.decoding = "async";
          img.onload = () => { const ph = el.querySelector(".ph"); if (ph) ph.replaceWith(img); addHighlights(el); };
          img.onerror = () => { const ph = el.querySelector(".ph"); if (ph) ph.outerHTML = '<div class="notice err" style="margin:20px">' + AF.icon("i-alert") + "<span>Page image failed to load.</span></div>"; };
          img.src = el.dataset.src;
        }
      });
    }, { root: $("pages"), rootMargin: "600px 0px", threshold: 0 });
    document.querySelectorAll("#pages .page").forEach((p) => io.observe(p));
  }

  function addHighlights(pageEl) {
    if (pageEl.dataset.kind !== "output" || !S.preview) return;
    const n = Number(pageEl.dataset.n);
    Object.entries(S.preview.highlights || {}).forEach(([fid, h]) => {
      if (h.page !== n) return;
      const f = S.figures.find((x) => x.figure_id === fid);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "hl";
      b.dataset.fig = fid;
      b.dataset.selected = String(fid === S.selected);
      b.dataset.review = String(!!(f && f.needs_review));
      b.style.cssText = "left:" + (h.x * 100 - 0.8) + "%;top:" + (h.y * 100 - 0.5) + "%;width:" + (h.w * 100 + 1.6) + "%;height:" + (h.h * 100 + 1) + "%";
      b.setAttribute("aria-label", "Select " + (f ? figTitle(f) : "caption"));
      pageEl.appendChild(b);
    });
  }

  function currentPage() {
    const box = $("pages");
    const line = box.scrollTop + 60;
    let cur = 1;
    const top = box.getBoundingClientRect().top;
    box.querySelectorAll(".page-col:last-child [data-n]").forEach((p) => { if (p.getBoundingClientRect().top - top + box.scrollTop <= line) cur = Number(p.dataset.n); });
    return cur;
  }
  let st;
  $("pages").addEventListener("scroll", () => {
    clearTimeout(st);
    st = setTimeout(() => {
      const n = String(currentPage());
      $("page-jump").value = n;
      store.set("page", Number(n));
      document.querySelectorAll("#thumbs button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.n === n)));
    }, 60);
  }, { passive: true });

  function renderThumbs() {
    const man = S.view === "source" ? S.source : S.preview;
    if (man && man.state === "client") { $("thumbs").innerHTML = '<p class="xs muted" style="grid-column:1/-1">Pages are drawn in your browser; use the page box above to jump.</p>'; return; }
    if (!man || man.state !== "ready") { $("thumbs").innerHTML = ""; return; }
    $("thumbs").innerHTML = man.pages.map((p) => '<button type="button" data-n="' + p.n + '" aria-label="Go to page ' + p.n + '"><img loading="lazy" alt="" src="' + p.thumb + '"><span>' + p.n + "</span></button>").join("");
  }

  function gotoPage(n, smooth = true) {
    const el = document.querySelector('#pages [data-n="' + n + '"][data-kind="' + (S.view === "source" ? "source" : "output") + '"]') ||
      document.querySelector('#pages [data-n="' + n + '"]');
    const box = $("pages");
    if (el) box.scrollTo({ top: Math.max(0, box.scrollTop + el.getBoundingClientRect().top - box.getBoundingClientRect().top - 16), behavior: smooth ? "smooth" : "auto" });
  }
  function scrollToHighlight(fid) {
    const hl = document.querySelector('.hl[data-fig="' + fid + '"], .hl-client[data-fig="' + fid + '"]');
    if (!hl) return false;
    const box = $("pages");
    box.scrollTo({ top: Math.max(0, box.scrollTop + hl.getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 2), behavior: "smooth" });
    return true;
  }

  function selectFigure(fid, opts = {}) {
    S.selected = fid;
    store.set("selected", fid);
    document.querySelectorAll(".fig-card").forEach((c) => c.setAttribute("aria-current", String(c.dataset.fig === fid)));
    document.querySelectorAll(".hl, .hl-client").forEach((h) => (h.dataset.selected = String(h.dataset.fig === fid)));
    const f = S.figures.find((x) => x.figure_id === fid);
    if (!f) return;
    if (opts.scroll !== false) {
      const h = S.preview?.highlights?.[fid];
      if (S.view === "source") gotoPage(f.page);
      else if (h) {
        if (!scrollToHighlight(fid)) { gotoPage(h.page); setTimeout(() => scrollToHighlight(fid), 600); }
      } else if (f.page) {
        // excluded figures: show where they are in the original
        if (S.view === "output" && !f.eligible && S.source) setView("source");
        gotoPage(f.page);
      }
    }
    if (opts.edit !== false) setTab("edit");
    if (window.matchMedia("(max-width: 880px)").matches && opts.fromPreview) setMobile("inspector");
  }

  function setView(v) {
    S.view = v;
    document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
    renderPages();
    renderThumbs();
  }

  // ------------------------------------------------------------------ editor
  function setTab(t) {
    S.tab = t;
    document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === t)));
    document.querySelectorAll("[data-panel]").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== t));
    if (t === "edit") renderEditor();
    if (t === "settings") renderSettings();
  }

  function renderEditor() {
    const box = $("editor");
    const f = S.figures.find((x) => x.figure_id === S.selected);
    if (!f) { box.innerHTML = '<div class="empty small">' + AF.icon("i-eye") + "Select a figure from the list or click a caption in the preview.</div>"; return; }
    const prefix = S.job.settings.prefix || "Figure";
    const sep = S.job.settings.separator || " ";
    const chapters = S.job.summary.chapters || [];
    const engine = f.engine || (f.caption_source === "manual" ? "Edited by you" : f.caption_source === "existing" ? "Existing caption in your document" : "");
    const conf = { high: ["High confidence", "pill-success"], medium: ["Medium confidence", "pill-violet"], low: ["Low confidence", "pill-warning"], verified: ["Verified by you", "pill-success"] }[f.confidence_band] || null;
    box.innerHTML =
      (f.thumb_url ? '<img class="insp-thumb" alt="Thumbnail of ' + AF.esc(figTitle(f)) + '" src="' + f.thumb_url + '">' : "") +
      '<div class="row between" style="margin:14px 0 6px"><h3 style="margin:0" class="tnum">' + AF.esc(figTitle(f)) + '</h3><span class="pill ' + (STATUS_PILL[f.status] || "") + '">' + AF.esc(f.status_label) + "</span></div>" +
      '<p class="small muted" style="margin:0 0 12px">' + AF.esc(f.reason) + "</p>" +
      ((f.mistakes || []).length ? '<div class="notice warn" style="margin-bottom:12px">' + AF.icon("i-alert") + "<div><b>Mistake in your caption</b><br>" +
        f.mistakes.map((m) => AF.esc(m.message)).join("<br>") + (f.correction ? '<br>Suggested: <b>' + AF.esc(f.correction) + "</b>" : "") +
        '<div class="row" style="margin-top:8px;gap:6px"><button class="btn btn-xs btn-primary" type="button" data-correct="' + f.figure_id + '">Correct</button>' +
        (f.correction ? '<button class="btn btn-xs" type="button" data-keep="' + f.figure_id + '">Keep mine</button>' : "") + "</div></div></div>" : "") +
      (f.eligible ? (
        '<label class="lbl" for="cap-text">Caption description</label>' +
        '<textarea id="cap-text" maxlength="240" aria-describedby="cap-hint">' + AF.esc(f.caption_text || "") + "</textarea>" +
        '<div class="hint" id="cap-hint">The number is added automatically. ' + (conf ? '<span class="pill ' + conf[1] + '">' + conf[0] + "</span>" : "") + (f.needs_review ? ' <span class="pill pill-warning">' + AF.icon("i-alert") + "Needs review</span>" : "") + "</div>" +
        '<div class="caption-preview" style="margin-top:12px" id="cap-preview" aria-live="polite"></div>' +
        (engine ? '<p class="xs muted" style="margin-top:6px">Source: ' + AF.esc(engine) + "</p>" : "") +
        '<div class="row" style="margin-top:12px;gap:6px">' +
        '<button class="btn btn-primary btn-sm" type="button" id="cap-save">' + AF.icon("i-check") + "Save caption</button>" +
        (f.needs_review ? '<button class="btn btn-sm" type="button" id="cap-accept">Mark reviewed</button>' : "") + "</div>" +
        '<h4 class="xs muted" style="margin:18px 0 8px;text-transform:uppercase;letter-spacing:.08em">AutoFigure AI</h4>' +
        '<div class="row" style="gap:6px">' + [["regenerate", "Regenerate"], ["shorter", "Shorter"], ["more_technical", "More technical"], ["more_formal", "More formal"]].map((v) =>
          '<button class="btn btn-xs" type="button" data-suggest="' + v[0] + '">' + v[1] + "</button>").join("") + "</div>" +
        '<div id="suggest-out" style="margin-top:10px"></div>' +
        ((f.alternatives || []).length ? '<h4 class="xs muted" style="margin:14px 0 6px;text-transform:uppercase;letter-spacing:.08em">Second opinions</h4>' + f.alternatives.map((a) => '<button type="button" class="alt-chip" data-use="' + AF.esc(a) + '">' + AF.esc(a) + "</button>").join("") : "")
      ) : "") +
      '<h4 class="xs muted" style="margin:20px 0 8px;text-transform:uppercase;letter-spacing:.08em">Placement</h4>' +
      '<dl class="kv"><dt>Original page</dt><dd>' + (f.page ?? "unknown") + (f.page_confident ? "" : " (unverified)") + "</dd>" +
      "<dt>Evidence</dt><dd>" + AF.esc(f.page_evidence || "—") + "</dd>" +
      "<dt>Section</dt><dd>" + AF.esc(f.chapter_title || "—") + "</dd>" +
      "<dt>Object</dt><dd>" + AF.esc(f.kind + " " + f.content_type + (f.in_table ? " · in table" : "") + " · " + f.width_cm + "×" + f.height_cm + " cm") + "</dd>" +
      (f.existing ? "<dt>Existing caption</dt><dd>" + AF.esc(f.existing.text) + "</dd>" : "") +
      (f.alt_text ? "<dt>Alt text</dt><dd>" + AF.esc(f.alt_text) + "</dd>" : "") + "</dl>" +
      '<div class="stack" style="--gap:8px;margin-top:14px">' +
      (f.status === "excluded_page_uncertain" ? '<button class="btn btn-sm" type="button" data-override=\'{"page_confirmed_not_first":true}\'>Confirm not on page 1</button>' : "") +
      (f.status === "excluded_first_page" ? '<p class="xs muted">Images on the original first page are always protected.</p>' : "") +
      (["excluded_decorative", "excluded_unsupported", "excluded_manual"].includes(f.status) ? '<button class="btn btn-sm" type="button" data-override=\'{"include":true}\'>Caption this image</button>' : "") +
      (f.eligible ? '<button class="btn btn-ghost btn-sm" type="button" data-override=\'{"include":false}\' style="justify-self:start">Do not caption this image</button>' : "") +
      (chapters.length && f.status !== "excluded_first_page" ? '<label class="lbl" for="assign">Assign to section</label><select id="assign"><option value="">Automatic (from headings)</option>' +
        chapters.map((c) => '<option value="' + c.number + '"' + (f.chapter_number === c.number && S.job.settings.figure_overrides?.[f.figure_id]?.chapter != null ? " selected" : "") + ">" + AF.esc(c.title) + "</option>").join("") + "</select>" : "") +
      "</div>" +
      '<h4 class="xs muted" style="margin:20px 0 8px;text-transform:uppercase;letter-spacing:.08em">History</h4><div id="history" class="small muted">Loading…</div>';
    const ta = $("cap-text");
    const pv = $("cap-preview");
    const upd = () => { if (pv) pv.innerHTML = "<b>" + AF.esc(prefix + " " + (f.label || "?")) + "</b>" + AF.esc((ta.value.trim() ? sep : "") + ta.value.trim()); };
    if (ta) { upd(); ta.addEventListener("input", upd); }
    AF.api(API + "/figures/" + f.figure_id + "/history").then((d) => {
      $("history").innerHTML = d.history.length ? d.history.map((h) => '<div class="row between" style="padding:6px 0;border-bottom:1px solid var(--border)"><span style="min-width:0">' + AF.esc(h.caption_text) +
        '<br><span class="xs">' + AF.date(h.created_at) + '</span></span><button class="btn btn-xs" type="button" data-use="' + AF.esc(h.caption_text) + '">' + AF.icon("i-undo") + "Use</button></div>").join("") : "No edits yet.";
    }).catch(() => { $("history").textContent = "History unavailable."; });
  }

  async function saveCaption(text) {
    const f = S.figures.find((x) => x.figure_id === S.selected);
    const btn = $("cap-save");
    AF.busy(btn, true, "Saving…");
    try {
      await AF.api(API + "/figures/" + f.figure_id, { method: "PATCH", json: { caption_text: text } });
      f.caption_text = text; f.caption_source = "manual"; f.needs_review = false; f.confidence_band = "verified"; f.engine = "";
      S.job.has_unrendered_changes = true;
      AF.toast("Caption saved. Apply changes to regenerate the Word file and preview.", "ok");
      renderFigureList(); renderEditor(); renderDownload();
    } catch (e) { AF.toast(e.message, "error"); AF.busy(btn, false); }
  }

  async function override(ov) {
    const f = S.figures.find((x) => x.figure_id === S.selected);
    const current = (S.job.settings.figure_overrides || {})[f.figure_id] || {};
    const merged = Object.assign({}, current, ov);
    try {
      await AF.api(API + "/figures/" + f.figure_id, { method: "PATCH", json: { override: merged } });
      S.job.settings.figure_overrides = Object.assign({}, S.job.settings.figure_overrides, { [f.figure_id]: merged });
      S.job.has_unrendered_changes = true;
      AF.toast("Saved. Apply changes to update numbering and the preview.", "ok");
      renderDownload();
    } catch (e) { AF.toast(e.message, "error"); }
  }

  async function suggest(variant, btn) {
    const f = S.figures.find((x) => x.figure_id === S.selected);
    AF.busy(btn, true, "Thinking…");
    try {
      const d = await AF.api(API + "/figures/" + f.figure_id + "/suggest", { method: "POST", json: { variant } });
      const alts = (d.alternatives || []).filter((a) => a !== d.suggestion);
      $("suggest-out").innerHTML = '<button type="button" class="alt-chip" data-use="' + AF.esc(d.suggestion) + '"><b>' + AF.esc(d.suggestion) + "</b><br><span class=\"xs muted\">" +
        AF.esc(d.engine || "AutoFigure AI") + " · " + AF.esc(d.confidence_band) + " confidence" + (d.needs_review ? " · check before using" : "") + " — click to use</span></button>" +
        alts.map((a) => '<button type="button" class="alt-chip" style="margin-top:6px" data-use="' + AF.esc(a) + '">' + AF.esc(a) + "</button>").join("");
    } catch (e) { AF.toast(e.message, "error"); }
    finally { AF.busy(btn, false); }
  }

  // ------------------------------------------------------------------ settings
  function renderSettings() {
    const s = S.job.settings;
    const sel = (name, opts) => '<select name="' + name + '" id="set-' + name + '">' + opts.map(([v, l]) => '<option value="' + v + '"' + (String(s[name]) === String(v) ? " selected" : "") + ">" + l + "</option>").join("") + "</select>";
    const sw = (name, title, sub) => '<label class="switch full"><input type="checkbox" name="' + name + '"' + (s[name] ? " checked" : "") + "><span><b>" + title + "</b><small>" + sub + "</small></span></label>";
    const heads = S.job.headings || [];
    $("settings-panel").innerHTML =
      '<form id="settings-form" class="stack" style="--gap:14px">' +
      '<div class="form-grid">' +
      '<div><label class="lbl" for="set-prefix">Label prefix</label><input type="text" id="set-prefix" name="prefix" maxlength="24" value="' + AF.esc(s.prefix) + '"></div>' +
      '<div><label class="lbl" for="set-separator">After number</label>' + sel("separator", [[" ", "Space (Figure 1.1 Text)"], [": ", "Colon (Figure 1.1: Text)"], [". ", "Period (Figure 1.1. Text)"], [" – ", "En dash"], [" — ", "Em dash"]]) + "</div>" +
      '<div><label class="lbl" for="set-number_style">Number format</label>' + sel("number_style", [["chapter", "1.1, 1.2, 2.1"], ["chapter_dash", "1-1, 1-2, 2-1"], ["document", "1, 2, 3"]]) + "</div>" +
      '<div><label class="lbl" for="set-align">Alignment</label>' + sel("align", [["center", "Center"], ["left", "Left"], ["right", "Right"]]) + "</div>" +
      '<div><label class="lbl" for="set-font_size">Font size (pt)</label><input type="number" id="set-font_size" name="font_size" min="7" max="16" step="0.5" value="' + s.font_size + '"></div>' +
      '<div><label class="lbl" for="set-color">Caption look</label>' + sel("color", [["neutral", "Neutral dark grey"], ["style", "Use document's Caption style"]]) + "</div>" +
      sw("italic", "Italic", "Subtle italic like a classic academic caption.") +
      sw("end_punctuation", "End with a period", "Adds “.” after the description.") +
      "</div>" +
      '<h4 class="xs muted" style="text-transform:uppercase;letter-spacing:.08em;margin:6px 0 0">Numbering &amp; sections</h4>' +
      '<div class="form-grid">' +
      '<div><label class="lbl" for="set-chapter_source">Sections from</label>' + sel("chapter_source", [["auto", "Automatic (LO first)"], ["lo", "LO headings only"], ["heading1", "Every Heading 1"], ["document", "No sections"]]) + "</div>" +
      '<div><label class="lbl" for="set-lo_number_mode">LO numbering</label>' + sel("lo_number_mode", [["visible", "Use LO number (LO4 → 4.1)"], ["ordinal", "Ordinal (1, 2, 3…)"]]) + "</div>" +
      '<div><label class="lbl" for="set-unassigned_policy">Figures before the first section</label>' + sel("unassigned_policy", [["review", "Hold for my review"], ["first_chapter", "Add to the first section"]]) + "</div>" +
      '<div><label class="lbl" for="set-existing_mode">Existing captions</label>' + sel("existing_mode", [["renumber", "Keep descriptions and renumber"], ["skip", "Skip already captioned figures"], ["replace", "Replace with AI suggestions"]]) + "</div>" +
      '<div><label class="lbl" for="set-position">Caption position</label>' + sel("position", [["below", "Below the image"], ["above", "Above the image (advanced)"]]) + "</div>" +
      '<div><label class="lbl" for="set-field_mode">Word field mode</label>' + sel("field_mode", [["seq", "Word SEQ fields (updatable)"], ["static", "Static text (most compatible)"]]) + "</div>" +
      sw("include_decorative", "Include decorative images", "Caption small icons and logos too.") +
      sw("keep_with_image", "Keep caption with image", "Prevents a page break between picture and caption.") +
      sw("update_fields_on_open", "Ask Word to update fields on open", "Word will show a prompt when the file opens.") +
      "</div>" +
      '<h4 class="xs muted" style="text-transform:uppercase;letter-spacing:.08em;margin:6px 0 0">AI descriptions</h4>' +
      '<div class="form-grid">' +
      '<div><label class="lbl" for="set-tone">Tone</label>' + sel("tone", [["academic", "Academic"], ["professional", "Professional"], ["technical", "Technical"], ["plain", "Plain language"]]) + "</div>" +
      '<div><label class="lbl" for="set-length">Length</label>' + sel("length", [["short", "Short"], ["standard", "Standard"], ["detailed", "Detailed"]]) + "</div>" +
      '<div><label class="lbl" for="set-language">Language</label>' + sel("language", [["auto", "Auto detect"], ["en", "English"], ["my", "Burmese"]]) + "</div>" +
      '</div><p class="hint">Tone, length and language apply to new suggestions and to images you add; captions you already have are kept.</p>' +
      '<h4 class="xs muted" style="text-transform:uppercase;letter-spacing:.08em;margin:6px 0 0">Chapter mapping</h4>' +
      (heads.length ? '<div class="stack" style="--gap:8px">' + heads.map((h) => {
        const cur = (s.heading_overrides || {})[h.heading_id] || "";
        return '<div class="row between" style="gap:8px"><span class="small grow" style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + AF.esc(h.text) + '">' + AF.esc(h.text) +
          (h.lo_number != null ? ' <span class="pill pill-accent">LO' + h.lo_number + "</span>" : "") + (h.source === "text_pattern" ? ' <span class="pill pill-warning">bold text</span>' : "") + '</span><select data-heading="' + h.heading_id + '" style="width:150px;min-height:36px" aria-label="Mapping for ' + AF.esc(h.text) + '">' +
          [["", "Automatic"], ["boundary", "Start a section"], ["ignore", "Ignore"]].map(([v, l]) => '<option value="' + v + '"' + (cur === v ? " selected" : "") + ">" + l + "</option>").join("") + "</select></div>";
      }).join("") + "</div>" : '<p class="small muted">No Heading 1 paragraphs found. Numbering runs across the whole document.</p>') +
      '<div class="row"><button class="btn btn-primary btn-sm" type="submit">Save settings</button><button class="btn btn-ghost btn-sm" type="button" id="reset-settings">Reset to defaults</button></div>' +
      '<p class="hint">Saving does not change the file yet — use Apply changes to regenerate the Word document and preview.</p></form>';
  }

  async function saveSettings(form, reset) {
    const fd = new FormData(form);
    const body = {};
    if (reset) {
      Object.assign(body, { prefix: "Figure", separator: " ", number_style: "chapter", align: "center", font_size: 10, italic: true, color: "neutral",
        end_punctuation: false, chapter_source: "auto", lo_number_mode: "visible", unassigned_policy: "review", existing_mode: "renumber",
        position: "below", field_mode: "seq", include_decorative: false, keep_with_image: true, update_fields_on_open: false,
        tone: "academic", length: "standard", language: "auto", heading_overrides: {} });
    } else {
      ["prefix", "separator", "number_style", "align", "color", "chapter_source", "lo_number_mode", "unassigned_policy", "existing_mode", "position", "field_mode", "tone", "length", "language"]
        .forEach((k) => { if (fd.has(k)) body[k] = fd.get(k); });
      body.font_size = Number(fd.get("font_size"));
      ["italic", "end_punctuation", "include_decorative", "keep_with_image", "update_fields_on_open"].forEach((k) => (body[k] = fd.has(k)));
      const ho = {};
      form.querySelectorAll("[data-heading]").forEach((s) => { if (s.value) ho[s.dataset.heading] = s.value; });
      body.heading_overrides = ho;
    }
    try {
      const d = await AF.api(API + "/settings", { method: "PATCH", json: body });
      S.job.settings = d.settings;
      S.job.has_unrendered_changes = true;
      AF.toast("Settings saved. Apply changes to regenerate the document.", "ok");
      renderSettings(); renderDownload();
    } catch (e) { AF.toast(e.message, "error"); }
  }

  // ------------------------------------------------------------------ download bar
  function renderDownload() {
    const j = S.job;
    const dl = $("download-btn");
    const apply = $("apply-btn");
    const rs = $("rev-state");
    if (j.ready_revision) {
      [dl, $("download-top")].forEach((b) => {
        b.href = API + "/download";
        b.removeAttribute("aria-disabled");
        b.setAttribute("download", j.download_name);
      });
    }
    const busy = j.state === "regenerating";
    renderNextStep();
    apply.disabled = busy || !j.has_unrendered_changes;
    if (busy) {
      rs.innerHTML = '<span class="spin" style="width:14px;height:14px;border:2px solid var(--primary);border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite"></span>Generating revision ' + (j.current_revision + 1) + " and rendering preview…";
    } else if (j.has_unrendered_changes) {
      rs.innerHTML = AF.icon("i-alert") + '<span>You have changes that are not in the file yet. <b>Apply changes</b> to regenerate it. Download gives revision ' + (j.ready_revision?.revision || "–") + ".</span>";
      rs.style.color = "var(--warning)";
    } else {
      rs.style.color = "";
      rs.innerHTML = AF.icon("i-check") + "<span>Preview shows revision " + (j.ready_revision?.revision || "–") + " — exactly the file you will download" +
        (j.ready_revision?.verification ? " (" + j.ready_revision.verification.verified_captions + " captions verified)" : "") + ".</span>";
    }
  }

  async function applyChanges() {
    const btn = $("apply-btn");
    AF.busy(btn, true, "Applying…");
    try {
      await AF.api(API + "/render", { method: "POST" });
      S.job.state = "regenerating";
      renderDownload();
      AF.toast("Regenerating the Word file from your original…");
      kick();
      setTimeout(poll, 800);
    } catch (e) { AF.toast(e.message, "error"); }
    finally { AF.busy(btn, false); renderDownload(); }
  }

  // ------------------------------------------------------------------ events
  document.addEventListener("toggle", (e) => {
    if (e.target.id !== "mistakes-details") return;
    S.mistakesOpen = e.target.open;
    const h = e.target.querySelector("summary .xs"); if (h) h.textContent = e.target.open ? "Hide" : "Show";
  }, true);
  document.addEventListener("click", (e) => {
    const t = e.target;
    const card = t.closest("[data-fig]");
    if (card) { selectFigure(card.dataset.fig, { fromPreview: card.classList.contains("hl") || card.classList.contains("hl-client") }); return; }
    if (t.closest("[data-download]")) { e.preventDefault(); $("download-top").click(); return; }
    if (t.closest("[data-apply]")) { e.preventDefault(); $("apply-btn").click(); return; }
    if (t.closest("#correct-all-top")) { e.preventDefault(); correct(null); return; }
    const dlb = t.closest("#download-btn, #download-top");
    if (dlb && S.lite && S.job && S.job.ready_revision) {
      e.preventDefault();
      const b = dlb;
      AF.busy(b, true, "Preparing…");
      AF.fetchChunked(API + "/download").then((blob) => AF.saveBlob(blob, S.job.download_name)).catch((er) => AF.toast(er.message, "error")).finally(() => AF.busy(b, false));
      return;
    }
    const v = t.closest("[data-view]"); if (v) { setView(v.dataset.view); return; }
    const z = t.closest("[data-zoom]"); if (z) { S.zoom = Math.min(3, Math.max(0.4, S.zoom + Number(z.dataset.zoom) * 0.15)); S.fit = S.fit === "page" ? "width" : S.fit; store.set("zoom", S.zoom); renderPages(); return; }
    const ft = t.closest("[data-fit]"); if (ft) { S.fit = ft.dataset.fit; S.zoom = 1; store.set("zoom", 1); renderPages(); return; }
    const th = t.closest("#thumbs button"); if (th) { gotoPage(Number(th.dataset.n)); return; }
    const tab = t.closest("[data-tab]"); if (tab) { setTab(tab.dataset.tab); return; }
    const fl = t.closest("[data-filter]"); if (fl) { S.filter = fl.dataset.filter; document.querySelectorAll("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b === fl))); renderFigureList(); return; }
    const fg = t.closest("[data-filter-go]"); if (fg) { S.filter = fg.dataset.filterGo; setTab("figures"); document.querySelectorAll("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filter === S.filter))); renderFigureList(); return; }
    const ch = t.closest("[data-chapter]"); if (ch) { const f = S.figures.find((x) => x.eligible && String(x.chapter_number) === ch.dataset.chapter); if (f) selectFigure(f.figure_id, { edit: false }); return; }
    const stt = t.closest("[data-stat]");
    if (stt) {
      if (stt.dataset.stat === "mistakes") { $("next-step").scrollIntoView({ behavior: "smooth" }); return; }
      S.filter = stt.dataset.stat; setTab("figures");
      document.querySelectorAll("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filter === S.filter)));
      renderFigureList(); if (window.matchMedia("(max-width: 880px)").matches) setMobile("inspector");
      return;
    }
    if (t.closest("#correct-all")) { e.preventDefault(); correct(null); return; }
    const cr = t.closest("[data-correct]"); if (cr) { correct([cr.dataset.correct]); return; }
    const kp = t.closest("[data-keep]"); if (kp) { correct([kp.dataset.keep], true); return; }
    const sh = t.closest("[data-show]"); if (sh) { const f = S.figures.find((x) => x.figure_id === sh.dataset.show); if (f && f.page) { setView("source"); setTimeout(() => gotoPage(f.page), 300); } selectFigure(sh.dataset.show, { scroll: false }); return; }
    const sg = t.closest("[data-suggest]"); if (sg) { suggest(sg.dataset.suggest, sg); return; }
    const use = t.closest("[data-use]"); if (use) { const ta = $("cap-text"); if (ta) { ta.value = use.dataset.use; ta.dispatchEvent(new Event("input")); ta.focus(); } return; }
    const ov = t.closest("[data-override]"); if (ov) { override(JSON.parse(ov.dataset.override)); return; }
    const mt = t.closest("[data-mtab]"); if (mt) { setMobile(mt.dataset.mtab); return; }
    if (t.closest("#cap-save")) { saveCaption($("cap-text").value); return; }
    if (t.closest("#cap-accept")) {
      const f = S.figures.find((x) => x.figure_id === S.selected);
      AF.api(API + "/figures/" + f.figure_id, { method: "PATCH", json: { accept: true } }).then(() => { f.needs_review = false; renderFigureList(); renderEditor(); renderSidebar(); }).catch((er) => AF.toast(er.message, "error"));
      return;
    }
    if (t.closest("#accept-all")) {
      AF.api(API + "/accept-confident", { method: "POST" }).then((d) => { AF.toast(d.accepted + " confident suggestion(s) accepted.", "ok"); return loadFigures(); }).then(renderSidebar).catch((er) => AF.toast(er.message, "error"));
      return;
    }
    if (t.closest("#apply-btn")) { applyChanges(); return; }
    if (t.closest("#reset-settings")) { saveSettings($("settings-form"), true); return; }
    if (t.closest("#fullscreen")) { const v2 = document.querySelector(".viewer"); if (document.fullscreenElement) document.exitFullscreen(); else v2.requestFullscreen?.(); return; }
    const dlx = t.closest("#download-btn, #download-top");
    if (dlx && dlx.getAttribute("aria-disabled") === "true") { e.preventDefault(); return; }
    if (t.closest("[data-delete-upload]")) { confirmDelete(); }
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "assign") override({ chapter: e.target.value === "" ? null : Number(e.target.value) });
  });
  document.addEventListener("submit", (e) => {
    if (e.target.id === "settings-form") { e.preventDefault(); saveSettings(e.target, false); }
  });
  $("page-jump").addEventListener("change", (e) => gotoPage(Number(e.target.value)));
  let searchIdx = -1;
  $("search").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll(".page.search-hit").forEach((p) => p.classList.remove("search-hit"));
    const man = S.view === "source" ? S.source : S.preview;
    if (!q || !man || !man.texts || !man.texts.length) { $("search-result").textContent = man && !man.texts ? "Search unavailable" : ""; return; }
    const hits = man.texts.map((t, i) => (t.toLowerCase().includes(q) ? i + 1 : 0)).filter(Boolean);
    if (!hits.length) { $("search-result").textContent = "No matches"; return; }
    searchIdx = (searchIdx + 1) % hits.length;
    $("search-result").textContent = "Page " + hits[searchIdx] + " (" + (searchIdx + 1) + "/" + hits.length + ")";
    document.querySelectorAll('.page[data-n="' + hits[searchIdx] + '"]').forEach((p) => p.classList.add("search-hit"));
    gotoPage(hits[searchIdx]);
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (e.key === "+" || e.key === "=") document.querySelector('[data-zoom="1"]')?.click();
    if (e.key === "-") document.querySelector('[data-zoom="-1"]')?.click();
    if (e.key === "j" || e.key === "k") {
      const el = S.figures.filter((f) => f.eligible);
      const i = el.findIndex((f) => f.figure_id === S.selected);
      const n = el[Math.min(el.length - 1, Math.max(0, i + (e.key === "j" ? 1 : -1)))];
      if (n) selectFigure(n.figure_id);
    }
  });
  let rz;
  window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { fitGrid(); if (S.preview) renderPages(); }, 200); });

  function setMobile(pane) {
    document.querySelectorAll("[data-mtab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mtab === pane)));
    document.querySelectorAll("[data-mpane]").forEach((p) => p.dataset.active = String(p.dataset.mpane === pane));
    if (pane === "viewer" && S.preview) renderPages();
  }

  function confirmDelete() {
    const dlg = $("confirm-dialog");
    dlg.returnValue = "";
    dlg.showModal();
    dlg.addEventListener("close", async function onClose() {
      dlg.removeEventListener("close", onClose);
      if (dlg.returnValue !== "confirm") return;
      try {
        await AF.api("/api/uploads/" + S.job.file.upload_id, { method: "DELETE" });
        AF.toast("Your upload and generated files were deleted.", "ok");
        setTimeout(() => (location.href = "/app"), 900);
      } catch (e) { AF.toast(e.message, "error"); }
    });
  }

  window.addEventListener("beforeunload", (e) => {
    const ta = $("cap-text");
    const f = S.figures.find((x) => x.figure_id === S.selected);
    if (ta && f && ta.value !== (f.caption_text || "")) { e.preventDefault(); e.returnValue = ""; }
  });

  poll();
})();
