/* The collector's front end. One file: the shell (tooltips, toast, help,
   status polling), the review deck, setup, and the older forms that still
   post JSON. Nothing here holds state the server does not already have. */

function toast(msg, ok) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.hidden = false;
  el.textContent = msg;
  el.classList.toggle("bad", ok === false);
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ── tooltips ───────────────────────────────────────────────────────── */

const tip = document.getElementById("tip");
document.addEventListener("mouseover", (ev) => {
  if (!tip) return;
  const el = ev.target.closest && ev.target.closest("[data-tip]");
  if (!el || !el.getAttribute("data-tip")) { tip.classList.remove("on"); return; }
  const r = el.getBoundingClientRect();
  tip.textContent = el.getAttribute("data-tip");
  tip.style.left = Math.max(12, Math.min(r.left, window.innerWidth - 300)) + "px";
  tip.style.top = (r.bottom + 8) + "px";
  tip.classList.add("on");
});

/* ── the help drawer ────────────────────────────────────────────────── */

const help = document.getElementById("help");
document.getElementById("help-open")?.addEventListener("click", () => help?.classList.toggle("open"));
document.getElementById("help-close")?.addEventListener("click", () => help?.classList.remove("open"));

/* ── the review deck: one place at a time ───────────────────────────── */

const deck = {
  cards: Array.from(document.querySelectorAll(".review-item")),
  at: 0,
};

function showCard() {
  deck.cards.forEach((card, i) => { card.hidden = i !== deck.at; });
  const dots = document.querySelectorAll("#review-dots span");
  dots.forEach((d, i) => {
    d.className = i < deck.at ? "done" : (i === deck.at ? "here" : "");
  });
  const count = document.getElementById("review-count");
  const done = deck.at >= deck.cards.length;
  if (count) {
    count.textContent = done ? "all clear" : (deck.at + 1) + " of " + deck.cards.length;
  }
  const clear = document.getElementById("all-clear");
  if (clear) clear.hidden = !done;
}

async function decide(status) {
  const card = deck.cards[deck.at];
  if (!card) return;
  const words = {
    complete: "published", pending: "sent back to the crawler",
    no_data: "recorded as having no such tax",
  };
  try {
    await api("/api/review", {
      geoid: card.dataset.geoid,
      category: card.dataset.category,
      status,
    });
    toast(card.dataset.name + " " + (words[status] || status) + ".");
    deck.at += 1;
    showCard();
  } catch (e) { toast(e.message, false); }
}

if (deck.cards.length) {
  document.addEventListener("keydown", (ev) => {
    if (ev.target.matches("input, textarea, select")) return;
    if (ev.key === "1") decide("complete");
    else if (ev.key === "2") decide("pending");
    else if (ev.key === "3") decide("no_data");
  });
}

/* ── setup: the three plain choices map onto the real settings ──────── */

function formToSettings(form) {
  const data = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") data[el.name] = el.checked ? "1" : "0";
    else if (el.type === "radio") { if (el.checked) data[el.name] = el.value; }
    else data[el.name] = el.value;
  }
  // "When it crawls" is one question on screen and three settings underneath.
  const when = data.when_mode;
  delete data.when_mode;
  if (when === "overnight") {
    data.schedule_enabled = "1";
    data.continuous_enabled = "0";
    if (!data.schedule_kind) data.schedule_kind = "daily";
  } else if (when === "always") {
    data.continuous_enabled = "1";
    data.schedule_enabled = "0";
  } else if (when === "manual") {
    data.continuous_enabled = "0";
    data.schedule_enabled = "0";
  }
  return data;
}

function showProviderBlock() {
  const chosen = document.querySelector("input[name=provider]:checked")?.value || "none";
  document.querySelectorAll("[data-provider-block]").forEach((el) => {
    el.style.display = el.dataset.providerBlock === chosen ? "" : "none";
  });
}
document.querySelectorAll("input[name=provider]").forEach(
  (el) => el.addEventListener("change", showProviderBlock));
showProviderBlock();

document.getElementById("settings-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/settings", formToSettings(ev.target));
    toast("Saved.");
  } catch (e) { toast(e.message, false); }
});

/* Removing a stored key: empty-means-unchanged protects secrets from every
   ordinary save, so removal has to be its own deliberate flag. */
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-clear-key]");
  if (!btn) return;
  const form = btn.closest("form");
  const key = btn.dataset.clearKey;
  const input = form && form.elements[key];
  if (!input) return;
  input.value = "";
  let flag = form.elements[key + "__clear"];
  if (!flag) {
    flag = document.createElement("input");
    flag.type = "hidden";
    flag.name = key + "__clear";
    form.appendChild(flag);
  }
  flag.value = "1";
  toast("Key will be removed when you save.");
});

/* ── buttons that do one thing ──────────────────────────────────────── */

function setStartButtons(running) {
  const start = document.getElementById("btn-start");
  const pause = document.getElementById("btn-pause");
  if (start) start.hidden = running;
  if (pause) pause.hidden = !running;
}

document.addEventListener("click", async (ev) => {
  const rev = ev.target.closest("[data-review]");
  if (rev) { decide(rev.dataset.review); return; }

  const btn = ev.target.closest("[data-action]");
  if (!btn) return;
  const act = btn.dataset.action;
  try {
    if (act === "start") {
      const r = await api("/api/start", {});
      toast(r.ready ? "Collecting. Leave it running."
                    : "Setting up first — this takes a few minutes.");
      setStartButtons(true);
    } else if (act === "pause") {
      await api("/api/pause", {});
      toast("Paused. Nothing new will be collected.");
      setStartButtons(false);
    } else if (act === "step") {
      const r = await api("/api/autopilot/step", {});
      toast(r.label || "Nothing to do.");
    } else if (act === "export") {
      const r = await api("/api/export", {});
      toast("Exported CSVs to " + r.dir);
    } else if (act === "burst") {
      const size = Number(document.getElementById("burst-size").value || 20);
      await api("/api/crawl/burst", { size });
      toast("Working through " + size + " places now.");
    } else if (act === "stop") {
      await api("/api/crawl/stop", {});
      toast("Stop requested.");
    } else if (act === "init") {
      const r = await api("/api/init", {});
      toast("Schema ready at " + r.path);
    } else if (act === "seed") {
      await api("/api/seed", {
        counties_only: document.getElementById("counties-only")?.checked,
        include_mcd: document.getElementById("include-mcd")?.checked,
      });
      toast("Setting up from the Census files — a few minutes.");
    } else if (act === "sst") {
      await api("/api/bulk/sst", { states: document.getElementById("sst-states")?.value || "" });
      toast("Loading published sales tax files.");
    } else if (act === "cog") {
      await api("/api/bulk/cog", {});
      toast("Loading the Census of Governments.");
    } else if (act === "statutes") {
      const st = (document.getElementById("statute-state")?.value || "").trim();
      if (st.length !== 2) throw new Error("Enter a two-letter state code");
      await api("/api/statutes/fetch", { state: st });
      toast("Fetching " + st.toUpperCase() + " statutes.");
    } else if (act === "test-provider") {
      const form = document.getElementById("settings-form");
      const out = document.getElementById("provider-test-result");
      if (form) await api("/api/settings", formToSettings(form));
      if (out) out.textContent = "Testing…";
      const r = await api("/api/provider/test", {});
      if (out) out.textContent = r.ok
        ? "✓ " + r.provider + " (" + r.model + ") answered: " + r.response
        : "✗ " + (r.error || "test failed");
      toast(r.ok ? "It works." : (r.error || "Test failed"), r.ok);
    }
  } catch (e) { toast(e.message, false); }
});

/* ── the older forms ────────────────────────────────────────────────── */

document.getElementById("plan-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  try {
    const r = await api("/api/plan", Object.fromEntries(fd.entries()));
    toast("Added " + r.created + " places to the list.");
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

/* ── search: "/" puts the cursor in the box ─────────────────────────── */

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") help?.classList.remove("open");
  if (ev.key !== "/" || ev.metaKey || ev.ctrlKey) return;
  if (ev.target.matches("input, textarea, select")) return;
  const box = document.getElementById("top-search");
  if (box) { ev.preventDefault(); box.focus(); box.select(); }
});

/* ── the status poll ────────────────────────────────────────────────── */

function num(n) { return Number(n || 0).toLocaleString(); }

async function poll() {
  try {
    const s = await api("/api/status");
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

    set("stat-review", s.stats.needs_review);
    set("rail-state", s.worker.state === "running" ? "collecting" : s.worker.state);
    setStartButtons(s.running);

    const badge = document.getElementById("rail-badge");
    if (badge) {
      badge.textContent = s.stats.needs_review;
      badge.hidden = !s.stats.needs_review;
    }
    const open = document.getElementById("open-count");
    if (open) {
      open.querySelectorAll("span").forEach((el) => { el.textContent = s.stats.needs_review; });
      const line = document.getElementById("headline");
      if (line) {
        line.textContent = s.stats.needs_review === 0
          ? "Nothing needs you right now."
          : (s.stats.needs_review === 1 ? "place needs your eyes." : "places need your eyes.");
      }
    }
    if (s.tally) {
      set("tally-fig", num(s.tally.done) + " / " + num(s.tally.total));
      set("tally-pct", s.tally.pct + "%");
      const bar = document.getElementById("tally-bar");
      if (bar) bar.style.width = s.tally.pct + "%";
    }
    if (s.progress) {
      const fill = document.getElementById("progress-fill");
      if (fill) fill.style.width = s.progress.pop_pct + "%";
      set("progress-pct", s.progress.pop_pct + "%");
      set("progress-left", num(s.progress.juris_total - s.progress.juris_done) + " places to go");
    }
    const pulse = document.getElementById("pulse");
    if (pulse) {
      pulse.classList.toggle("live", s.worker.state === "running");
      pulse.setAttribute("data-tip", s.collecting || "");
    }
    const warn = document.getElementById("warn-line");
    if (warn) {
      warn.textContent = s.warning || "";
      warn.hidden = !s.warning;
    }
  } catch (e) { /* the page keeps whatever it last showed */ }
}
setInterval(poll, 4000);

async function pollTimeline() {
  const box = document.getElementById("timeline");
  if (!box) return;
  try {
    const events = await api("/api/timeline?limit=8");
    if (!events.length) return;
    box.innerHTML = events.map((e) =>
      `<li><span class="dot ${escapeHtml(e.tone)}"></span>` +
      `<div class="text">${e.href ? `<a href="${escapeHtml(e.href)}">${escapeHtml(e.text)}</a>` : escapeHtml(e.text)}</div>` +
      `<div class="time">${escapeHtml(e.time || "")}</div></li>`
    ).join("");
  } catch (e) { /* leave the rendered feed alone */ }
}
if (document.getElementById("timeline")) setInterval(pollTimeline, 20000);
