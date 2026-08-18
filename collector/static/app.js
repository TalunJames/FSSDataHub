function toast(msg, ok) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.hidden = false;
  el.textContent = msg;
  el.style.borderLeftColor = ok === false ? "#8a2e24" : "#b08948";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 4200);
}

async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET"), headers: {} };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch (e) { data = { detail: text }; }
  if (!res.ok) {
    const msg = data.detail || data.message || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function fillInstruments(category) {
  const sel = document.getElementById("f-instrument");
  if (!sel || !window.INSTRUMENTS) return;
  const codes = window.INSTRUMENTS[category] || [];
  sel.innerHTML = codes.map((c) => `<option value="${c}">${c}</option>`).join("");
}

function formToSettings(form) {
  const data = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") data[el.name] = el.checked ? "1" : "0";
    else data[el.name] = el.value;
  }
  return data;
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-action]");
  if (btn) {
    const act = btn.dataset.action;
    try {
      if (act === "start") {
        const r = await api("/api/start", {});
        toast(r.ready ? "Collecting. Leave this running."
                      : "Setting up first — this takes a few minutes.");
        setStartButtons(true);
      } else if (act === "pause") {
        await api("/api/pause", {});
        toast("Paused. Nothing new will be collected.");
        setStartButtons(false);
      } else if (act === "step") {
        const r = await api("/api/autopilot/step", {});
        toast(r.label || "nothing to do");
      } else if (act === "export") {
        const r = await api("/api/export", {});
        toast("Exported CSVs to " + r.dir);
      } else if (act === "burst") {
        const size = Number(document.getElementById("burst-size").value || 20);
        await api("/api/crawl/burst", { size });
        toast("Burst started (" + size + " items)");
      } else if (act === "stop") {
        await api("/api/crawl/stop", {});
        toast("Stop requested");
      } else if (act === "init") {
        const r = await api("/api/init", {});
        toast("Schema ready at " + r.path);
      } else if (act === "seed") {
        await api("/api/seed", {
          counties_only: document.getElementById("counties-only")?.checked,
          include_mcd: document.getElementById("include-mcd")?.checked,
        });
        toast("Seeding in the background — Census bulk files, a few minutes");
      } else if (act === "sst") {
        await api("/api/bulk/sst", { states: document.getElementById("sst-states")?.value || "" });
        toast("SST fetch started — rate files for member states");
      } else if (act === "cog") {
        await api("/api/bulk/cog", {});
        toast("Census of Governments download started");
      } else if (act === "statutes") {
        const st = (document.getElementById("statute-state")?.value || "").trim();
        if (st.length !== 2) throw new Error("Enter a two-letter state code");
        await api("/api/statutes/fetch", { state: st });
        toast("Statute snapshot for " + st.toUpperCase() + " started");
      } else if (act === "test-provider") {
        const form = document.getElementById("settings-form");
        const out = document.getElementById("provider-test-result");
        if (form) await api("/api/settings", formToSettings(form));
        if (out) out.textContent = "Testing…";
        const r = await api("/api/provider/test", {});
        if (out) out.textContent = r.ok
          ? "✓ " + r.provider + " (" + r.model + ") answered: " + r.response
          : "✗ " + (r.error || "test failed");
        toast(r.ok ? "Key works" : (r.error || "test failed"), r.ok);
      }
    } catch (e) { toast(e.message, false); }
  }
  const rev = ev.target.closest("[data-review]");
  if (rev) {
    try {
      await api("/api/review", {
        geoid: rev.dataset.geoid,
        category: rev.dataset.category,
        status: rev.dataset.review,
      });
      toast(rev.dataset.geoid + " → " + rev.dataset.review);
      rev.closest(".review-card")?.remove();
    } catch (e) { toast(e.message, false); }
  }
  const sug = ev.target.closest("#j-results li");
  if (sug) {
    document.getElementById("f-geoid").value = sug.dataset.geoid;
    const u = document.getElementById("u-geoid");
    if (u) u.value = sug.dataset.geoid;
    document.getElementById("j-results").hidden = true;
  }
});

function setStartButtons(running) {
  const start = document.getElementById("btn-start");
  const pause = document.getElementById("btn-pause");
  if (start) start.hidden = running;
  if (pause) pause.hidden = !running;
}

document.getElementById("toggle-continuous")?.addEventListener("change", async (ev) => {
  try {
    await api("/api/crawl/toggle", { enabled: ev.target.checked });
    toast(ev.target.checked ? "Continuous crawl on" : "Continuous crawl off");
  } catch (e) { toast(e.message, false); ev.target.checked = !ev.target.checked; }
});

document.getElementById("plan-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  try {
    const r = await api("/api/plan", Object.fromEntries(fd.entries()));
    toast("Created " + r.created + " work items");
  } catch (e) { toast(e.message, false); }
});

document.getElementById("settings-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/settings", formToSettings(ev.target));
    toast("Settings saved");
  } catch (e) { toast(e.message, false); }
});

document.getElementById("finding-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const g = (name) => f[name]?.value;
  const num = (name) => {
    const v = g(name);
    return v === "" || v == null ? null : Number(v);
  };
  const finding = {
    geoid: g("geoid"),
    category: g("category"),
    instrument_code: g("instrument_code"),
    label: g("label") || null,
    status: g("status"),
    rate_value: num("rate_value"),
    rate_unit: g("rate_unit") || null,
    rate_basis: g("rate_basis") || null,
    statute_cite: g("statute_cite") || null,
    effective_date: g("effective_date") || null,
    expiration_date: g("expiration_date") || null,
    fiscal_year: num("fiscal_year"),
    voter_approval_required: g("voter_approval_required") || null,
    confidence: g("confidence"),
    extraction_method: "manual",
    notes: g("notes") || null,
    source: {
      url: g("source_url"),
      name: g("source_name") || g("source_url"),
      source_type: g("source_type"),
      authority_tier: Number(g("authority_tier")),
    },
  };
  try {
    const r = await api("/api/ingest", { finding, researcher: "manual" });
    toast("Wrote " + r.written + " finding(s); " + r.rejected + " rejected");
    if (r.errors?.length) toast(r.errors[0], false);
  } catch (e) { toast(e.message, false); }
});

document.getElementById("json-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const doc = JSON.parse(ev.target.json.value);
    const r = await api("/api/ingest", doc);
    toast("Wrote " + r.written + " finding(s)");
  } catch (e) { toast(e.message, false); }
});

document.getElementById("file-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const file = ev.target.file.files[0];
  if (!file) return toast("Choose a file", false);
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("/api/ingest-file", fd);
    toast("Wrote " + r.written + " finding(s)");
  } catch (e) { toast(e.message, false); }
});

document.getElementById("url-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  try {
    const r = await api("/api/fetch-url", {
      url: fd.get("url"),
      geoid: fd.get("geoid") || null,
      category: fd.get("category") || null,
      extract: fd.get("extract") === "1",
    });
    const pre = document.getElementById("url-preview");
    pre.hidden = false;
    pre.textContent = (r.error ? "ERROR: " + r.error + "\n\n" : "") + (r.text || "");
    toast(r.ok ? "Archived" + (r.findings_written ? "; " + r.findings_written + " findings" : "") : r.error, r.ok);
  } catch (e) { toast(e.message, false); }
});

const jSearch = document.getElementById("j-search");
if (jSearch) {
  let t;
  jSearch.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = jSearch.value.trim();
      const box = document.getElementById("j-results");
      if (q.length < 2) { box.hidden = true; return; }
      const rows = await api("/api/jurisdictions?q=" + encodeURIComponent(q));
      box.hidden = rows.length === 0;
      box.innerHTML = rows.map((r) =>
        `<li data-geoid="${r.geoid}">${r.name} <span class="dim">${r.state_usps} ${r.kind} · ${r.geoid}</span></li>`
      ).join("");
    }, 200);
  });
}

document.getElementById("f-category")?.addEventListener("change", (ev) => fillInstruments(ev.target.value));
fillInstruments(document.getElementById("f-category")?.value);

async function poll() {
  try {
    const s = await api("/api/status");
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("stat-jurisdictions", s.stats.jurisdictions);
    set("stat-work", s.stats.work_items);
    set("stat-pending", s.stats.pending);
    set("stat-review", s.stats.needs_review);
    set("stat-done", s.stats.done);
    set("stat-verified", s.stats.auto_verified);
    set("stat-instruments", s.stats.instruments);
    set("stat-pages", s.stats.pages);
    set("collect-line", s.collecting);
    set("rail-state", s.worker.state === "running" ? "collecting" : s.worker.state);
    setStartButtons(s.running);

    if (s.progress) {
      const fill = document.getElementById("progress-fill");
      if (fill) fill.style.width = s.progress.pop_pct + "%";
      const line = document.getElementById("progress-line");
      if (line) {
        line.innerHTML = "Records cover <strong>" + s.progress.pop_pct +
          "%</strong> of the US population (" +
          s.progress.juris_done.toLocaleString() + " of " +
          s.progress.juris_total.toLocaleString() +
          " counties and cities finished, " + s.progress.states_done + " of " +
          s.progress.states_total + " states' rules confirmed).";
      }
    }

    const warn = document.getElementById("warn-line");
    if (warn) {
      warn.textContent = s.warning || "";
      warn.hidden = !s.warning;
    }
    const pulse = document.querySelector(".pulse");
    if (pulse) pulse.classList.toggle("live", s.worker.state === "running");
  } catch (e) { /* ignore */ }
}
if (document.getElementById("collect-line") || document.getElementById("stat-pending")) {
  setInterval(poll, 2500);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function pollActivity() {
  const box = document.getElementById("activity-list");
  if (!box) return;
  try {
    const events = await api("/api/activity?limit=20");
    if (!events.length) {
      box.innerHTML = '<li class="dim">Nothing to report — no failures, nothing flagged.</li>';
      return;
    }
    box.innerHTML = events.map((e) =>
      `<li class="act ${escapeHtml(e.kind)}"><a href="${escapeHtml(e.href)}">${escapeHtml(e.title)}</a>` +
      (e.detail ? ` <span class="dim">${escapeHtml(e.detail)}</span>` : "") +
      ` <span class="dim ts">${escapeHtml(e.ts || "")}</span></li>`
    ).join("");
  } catch (e) { /* ignore */ }
}
if (document.getElementById("activity-list")) {
  pollActivity();
  setInterval(pollActivity, 10000);
}
