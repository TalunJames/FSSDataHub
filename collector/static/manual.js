(function () {
  if (!document.getElementById("step-pick")) return;

  const state = { geoid: "", name: "", category: "", session: null, files: [] };

  // Everything interpolated into innerHTML below comes from the database, and
  // much of that came from crawled web pages (titles, URLs, error text). A
  // hostile page title must render as text, never run.
  const esc = window.escapeHtml;

  // No interview for the pass categories: their answers are documents
  // (canvasses, statute pages), so the wizard goes straight to the source
  // drop instead of dead-ending on the interview API.
  const PASS_CATS = ["elections", "framework"];

  function show(id) {
    ["step-pick", "step-choose", "step-source", "step-ask"].forEach((s) => {
      const el = document.getElementById(s);
      if (el) el.hidden = s !== id;
    });
  }

  function cats() {
    const box = document.getElementById("m-cats");
    box.innerHTML = Object.keys(window.CATEGORIES || {}).map((c) =>
      `<button type="button" class="chip ${state.category === c ? "on" : ""}" data-cat="${c}">${c.replace(/_/g, " ")}</button>`
    ).join("");
  }

  function enableContinue() {
    document.getElementById("m-continue").disabled = !(state.geoid && state.category);
  }

  async function loadInbox() {
    const rows = await api("/api/manual/inbox");
    const ul = document.getElementById("m-inbox");
    if (!rows.length) {
      ul.innerHTML = "<li class='dim'>Inbox is empty.</li>";
      return;
    }
    ul.innerHTML = rows.map((r) =>
      `<li><button type="button" class="linkish" data-pick="${esc(r.geoid)}" data-cat="${esc(r.category)}">
        ${esc(r.name)} <span class="dim">${esc(r.state_usps)} · ${esc(r.category)}</span>
        <span class="pill-soft">${esc(r.status)}</span>
      </button>
      ${r.last_error ? `<div class="dim">${esc(r.last_error)}</div>` : ""}</li>`
    ).join("");
  }

  async function openChoose() {
    if (PASS_CATS.includes(state.category)) {
      show("step-source");
      loadIntake().catch(() => {});
      return;
    }
    const sess = await api("/api/interview?geoid=" + encodeURIComponent(state.geoid)
      + "&category=" + encodeURIComponent(state.category));
    state.session = sess;
    const j = sess.jurisdiction;
    state.name = j.name;
    document.getElementById("choose-title").textContent =
      j.name + " · " + j.state + " · " + sess.category_label;
    const bits = [];
    if (sess.last_error) bits.push("Last crawl: " + sess.last_error);
    if (sess.extract_error) bits.push("Extractor: " + sess.extract_error);
    if (!bits.length) bits.push(sess.pages_archived
      ? sess.pages_archived + " page(s) archived. " + sess.remaining + " question(s) still open."
      : "No crawl pages yet. " + sess.remaining + " question(s) open.");
    document.getElementById("choose-why").textContent = bits.join(" ");
    const known = document.getElementById("choose-known");
    if (sess.known.length) {
      known.innerHTML = "<table><tbody>" + sess.known.map((k) =>
        `<tr><td>${esc(k.label)}</td><td>${esc(k.status)}</td><td>${k.rate ? esc(k.rate) : "—"}</td></tr>`
      ).join("") + "</tbody></table>";
    } else {
      known.innerHTML = "<p class='hint'>Nothing on file yet.</p>";
    }
    show("step-choose");
  }

  async function loadIntake() {
    const rows = await api("/api/intake?geoid=" + encodeURIComponent(state.geoid));
    const ul = document.getElementById("intake-list");
    if (!rows.length) {
      ul.innerHTML = "<li class='dim'>Nothing queued yet.</li>";
      return;
    }
    ul.innerHTML = rows.map((r) =>
      `<li><span class="pill-soft">${esc(r.status)}</span> ${esc(r.kind)}
        ${esc(r.filename || r.url || "")}
        ${r.findings_written ? " · " + esc(r.findings_written) + " finding(s)" : ""}
        ${r.error ? "<div class='dim'>" + esc(r.error) + "</div>" : ""}
        ${r.status === "queued" || r.status === "failed"
          ? `<button type="button" data-run="${esc(r.id)}">Process now</button>` : ""}
      </li>`
    ).join("");
  }

  function renderAsk(sess) {
    state.session = sess;
    const done = document.getElementById("ask-done");
    const card = document.getElementById("ask-card");
    if (!sess.question) {
      card.hidden = true;
      done.hidden = false;
      return;
    }
    done.hidden = true;
    card.hidden = false;
    const q = sess.question;
    document.getElementById("q-progress").textContent =
      sess.remaining + " open · skip any you cannot answer";
    document.getElementById("q-title").textContent = q.title;
    document.getElementById("q-prompt").textContent = q.prompt;
    document.getElementById("q-why").textContent = q.why;
    const known = q.known;
    document.getElementById("q-known").textContent = known
      ? ("On file: " + known.status + (known.rate_value != null ? " @ " + known.rate_value + " " + (known.rate_unit || "") : ""))
      : "";
    const st = document.getElementById("q-status");
    const cur = (known && known.status) || "";
    const labels = {
      levied: "Levied",
      authorized_not_levied: "Authorized, not levied",
      prohibited: "Prohibited",
      repealed: "Repealed",
      unknown: "Unknown",
    };
    st.innerHTML = (sess.statuses || Object.keys(labels)).map((s) =>
      `<button type="button" class="chip ${cur === s ? "on" : ""}" data-status="${esc(s)}">${esc(labels[s] || s)}</button>`
    ).join("");
    const unit = document.getElementById("q-rate-unit");
    unit.innerHTML = (sess.rate_units || []).map((u) =>
      `<option value="${esc(u)}" ${u === (known && known.rate_unit) || u === sess.default_unit ? "selected" : ""}>${esc(u)}</option>`
    ).join("");
    document.getElementById("q-rate-value").value = (known && known.rate_value) || "";
    document.getElementById("q-year").value = (known && known.fiscal_year) || "";
    document.getElementById("q-cite").value = (known && known.statute_cite) || "";
    document.getElementById("q-notes").value = (known && known.notes) || "";
    const src = document.getElementById("q-source");
    src.value = (known && known.source_url) || (sess.citations[0] && sess.citations[0].url) || "";
    toggleRate(cur);
    const cites = document.getElementById("q-cites");
    cites.innerHTML = sess.citations.map((c) =>
      `<button type="button" class="chip" data-cite="${esc(c.url)}">${esc(c.label)}</button>`
    ).join("") || "<span class='dim'>No crawled URLs yet — paste one, or skip.</span>";
  }

  function selectedStatus() {
    const on = document.querySelector("#q-status .chip.on");
    return on ? on.dataset.status : "";
  }

  function toggleRate(status) {
    document.getElementById("q-rate").hidden = status !== "levied";
  }

  function payload() {
    return {
      status: selectedStatus() || "unknown",
      rate_value: document.getElementById("q-rate-value").value,
      rate_unit: document.getElementById("q-rate-unit").value,
      fiscal_year: document.getElementById("q-year").value,
      statute_cite: document.getElementById("q-cite").value,
      source_url: document.getElementById("q-source").value,
      notes: document.getElementById("q-notes").value,
    };
  }

  async function answer(action) {
    const q = state.session && state.session.question;
    const body = {
      geoid: state.geoid,
      category: state.category,
      question_id: q && q.id,
      action,
      payload: payload(),
    };
    if (action === "answered" && !selectedStatus()) {
      return toast("Pick a status, or Skip / Don’t know", false);
    }
    if (action === "answered" && selectedStatus() === "levied" && payload().rate_value === "") {
      return toast("Levied needs a rate — or Skip this one", false);
    }
    try {
      const r = await api("/api/interview/answer", body);
      if (r.errors && r.errors.length) toast(r.errors[0], false);
      else if (r.written) toast("Saved");
      else if (action === "skipped") toast("Skipped");
      else if (action === "skip_rest") toast("Remaining questions skipped");
      else if (action === "unknown") toast("Marked unknown");
      renderAsk(r.session);
    } catch (e) { toast(e.message, false); }
  }

  document.getElementById("m-cats").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-cat]");
    if (!b) return;
    state.category = b.dataset.cat;
    cats();
    enableContinue();
  });

  document.getElementById("m-continue").addEventListener("click", () => openChoose().catch((e) => toast(e.message, false)));

  document.getElementById("leave-later").addEventListener("click", () => show("step-pick"));

  document.addEventListener("click", async (ev) => {
    const back = ev.target.closest("[data-back]");
    if (back) {
      show(back.dataset.back === "pick" ? "step-pick" : "step-choose");
      if (back.dataset.back === "pick") loadInbox().catch(() => {});
      return;
    }
    const door = ev.target.closest("[data-door]");
    if (door) {
      if (door.dataset.door === "source") {
        show("step-source");
        loadIntake().catch(() => {});
      } else {
        show("step-ask");
        renderAsk(state.session);
      }
      return;
    }
    const pick = ev.target.closest("[data-pick]");
    if (pick) {
      state.geoid = pick.dataset.pick;
      state.category = pick.dataset.cat;
      state.name = pick.textContent.trim();
      document.getElementById("m-geoid").value = state.geoid;
      document.getElementById("m-picked").textContent = pick.textContent.trim();
      cats();
      enableContinue();
      openChoose().catch((e) => toast(e.message, false));
      return;
    }
    const sug = ev.target.closest("#m-results li");
    if (sug) {
      state.geoid = sug.dataset.geoid;
      state.name = sug.dataset.name;
      document.getElementById("m-geoid").value = state.geoid;
      document.getElementById("m-picked").textContent = sug.dataset.name + " · " + sug.dataset.meta;
      document.getElementById("m-results").hidden = true;
      enableContinue();
      return;
    }
    const st = ev.target.closest("#q-status [data-status]");
    if (st) {
      document.querySelectorAll("#q-status .chip").forEach((c) => c.classList.toggle("on", c === st));
      toggleRate(st.dataset.status);
      return;
    }
    const cite = ev.target.closest("[data-cite]");
    if (cite) {
      document.getElementById("q-source").value = cite.dataset.cite;
      return;
    }
    const run = ev.target.closest("[data-run]");
    if (run) {
      try {
        await api("/api/intake/" + run.dataset.run + "/run", {});
        toast("Processing…");
        setTimeout(loadIntake, 2000);
      } catch (e) { toast(e.message, false); }
    }
  });

  const search = document.getElementById("m-search");
  let t;
  search.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = search.value.trim();
      const box = document.getElementById("m-results");
      if (q.length < 2) { box.hidden = true; return; }
      const rows = await api("/api/jurisdictions?q=" + encodeURIComponent(q));
      box.hidden = rows.length === 0;
      box.innerHTML = rows.map((r) =>
        `<li data-geoid="${esc(r.geoid)}" data-name="${esc(r.name)}" data-meta="${esc(r.state_usps + " " + r.kind)}">${esc(r.name)} <span class="dim">${esc(r.state_usps)} ${esc(r.kind)} · ${esc(r.geoid)}</span></li>`
      ).join("");
    }, 200);
  });

  const drop = document.getElementById("drop");
  const fileInput = document.getElementById("source-files");
  function listFiles(files) {
    state.files = Array.from(files);
    document.getElementById("drop-list").textContent = state.files.map((f) => f.name).join(", ");
  }
  drop.addEventListener("click", () => fileInput.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("hot"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("hot"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("hot");
    listFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener("change", () => listFiles(fileInput.files));
  fileInput.addEventListener("click", (e) => e.stopPropagation());

  document.getElementById("source-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    fd.set("geoid", state.geoid);
    fd.set("category", state.category);
    state.files.forEach((f) => fd.append("files", f));
    try {
      const r = await api("/api/intake", fd);
      toast("Queued " + r.queued + " source(s) for the AI");
      state.files = [];
      fileInput.value = "";
      document.getElementById("drop-list").textContent = "";
      loadIntake();
    } catch (e) { toast(e.message, false); }
  });

  document.getElementById("q-save").addEventListener("click", () => answer("answered"));
  document.getElementById("q-skip").addEventListener("click", () => answer("skipped"));
  document.getElementById("q-unknown").addEventListener("click", () => answer("unknown"));
  document.getElementById("q-skip-rest").addEventListener("click", () => answer("skip_rest"));

  cats();
  loadInbox().catch(() => {});

  if (window.START_GEOID && window.START_CATEGORY) {
    state.geoid = window.START_GEOID;
    state.category = window.START_CATEGORY;
    document.getElementById("m-geoid").value = state.geoid;
    document.getElementById("m-picked").textContent = state.geoid;
    cats();
    enableContinue();
    openChoose().catch((e) => toast(e.message, false));
  }
})();
