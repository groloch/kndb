"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const ROLES = ["owner", "maintainer", "contributor", "spectator"];

// project id comes from the URL /projects/<pid>
const PID = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");

const S = {
  project: null,
  sources: [],   // full personal library (for the add-source modal)
  rows: [],      // this project's sources (public rows)
  current: null,
  noteMode: "preview",
  noteDirty: false,
  saveTimer: null,
};


async function api(path, opts = {}) {
  const init = { headers: {}, ...opts };
  if (init.body && !(init.body instanceof FormData) && typeof init.body !== "string") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok || (data && data.ok === false)) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let _toastTimer;
function toast(msg, kind = "info", ms = 4200) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = "toast"; }, ms);
}

function openModal(id) { $(`#${id}`).classList.remove("hidden"); }
function closeModal(id) { $(`#${id}`).classList.add("hidden"); }

/* ---------- Tiny markdown ---------- */

function renderMarkdown(md) {
  if (!md) return "";
  let html;
  if (window.marked) html = window.marked.parse(md, { breaks: true, gfm: true });
  else html = miniMarkdown(md);
  if (window.DOMPurify) return DOMPurify.sanitize(html);
  return html.replace(/<script[\s\S]*?<\/script>/gi, "");
}

function miniMarkdown(md) {
  const esc = md.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = esc.split(/\r?\n/);
  const out = []; let inCode = false; let para = [];
  const flushP = () => { if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; } };
  const inline = t => t
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
  for (const raw of lines) {
    if (raw.startsWith("```")) {
      flushP();
      if (!inCode) { out.push("<pre><code>"); inCode = true; }
      else { out.push("</code></pre>"); inCode = false; }
      continue;
    }
    if (inCode) { out.push(raw); continue; }
    const h = raw.match(/^(#{1,6})\s+(.*)/);
    if (h) { flushP(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    if (/^[-*]\s+/.test(raw)) { flushP(); out.push(`<li>${inline(raw.replace(/^[-*]\s+/, ""))}</li>`); continue; }
    if (/^\d+\.\s+/.test(raw)) { flushP(); out.push(`<li>${inline(raw.replace(/^\d+\.\s+/, ""))}</li>`); continue; }
    if (/^---+\s*$/.test(raw)) { flushP(); out.push("<hr>"); continue; }
    if (raw.trim() === "") { flushP(); continue; }
    para.push(inline(raw));
  }
  flushP();
  if (inCode) out.push("</code></pre>");
  return out.join("\n");
}


/* ---------- Tab switching ---------- */

function switchTab(tab) {
  $$(".subtab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  ["overview", "library", "latex", "settings"].forEach(t => {
    const pane = $("#tab-" + t);
    if (t === tab) pane.classList.remove("hidden");
    else pane.classList.add("hidden");
  });
}

$$(".subtab").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));

// Overview landing: the source preview jumps into the Library and selects it.
document.addEventListener("click", e => {
  const srcRow = e.target.closest(".ov-src-row");
  if (srcRow) {
    switchTab("library");
    selectSource(srcRow.dataset.id);
  }
});


/* ---------- Load project parent ---------- */

async function loadProject() {
  let p;
  try {
    p = (await api("/api/projects/" + PID)).project;
  } catch (e) {
    toast("Could not load this project: " + e.message, "err", 6000);
    setTimeout(() => { location.href = "/projects"; }, 900);
    return;
  }
  S.project = p;
  document.title = p.name + " — KNDB";
  $("#sub-name").textContent = p.name;
  $("#sub-name").title = p.description || "";
  $("#prj-badge").classList.toggle("hidden", !p.completed);
  renderSettings();
  loadTexDraft();
  await refreshLibrary();
  setupSplitters();
  renderOverview();
}

function fmtDate(s) {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function renderOverview() {
  const p = S.project;
  if (!p) return;
  const desc = (p.description || "").trim();
  $("#ov-name").textContent = p.name;
  $("#ov-desc").textContent = desc || "No description yet — describe what this project is about in Settings.";
  $("#ov-badge").classList.toggle("hidden", !p.completed);
  $("#ov-count-src").textContent = String(S.rows.length);
  $("#ov-count-mem").textContent = String((p.members || []).length);
  $("#ov-created").textContent = fmtDate(p.created_at) || "–";
  renderOverviewSources();
  renderOverviewMembers();
}

function renderOverviewSources() {
  const el = $("#ov-src");
  el.innerHTML = "";
  if (!S.rows.length) {
    el.innerHTML = '<p class="muted">No sources yet — head to the Library to add some.</p>';
    return;
  }
  for (const s of S.rows.slice(0, 5)) {
    const row = document.createElement("button");
    row.className = "mini-row ov-src-row";
    row.dataset.id = s.id;
    row.innerHTML =
      '<span class="mini-name" title="' + escapeHtml(s.title) + '">' + escapeHtml(s.title) + "</span>" +
      typePill(s.source_type);
    el.appendChild(row);
  }
  if (S.rows.length > 5) {
    const more = document.createElement("div");
    more.className = "muted ov-more";
    more.textContent = "+" + (S.rows.length - 5) + " more — open the Library";
    el.appendChild(more);
  }
}

function renderOverviewMembers() {
  const el = $("#ov-mem");
  const p = S.project;
  el.innerHTML = "";
  if (!(p.members || []).length) {
    el.innerHTML = '<p class="muted">No members.</p>';
    return;
  }
  for (const m of p.members) {
    const row = document.createElement("div");
    row.className = "mini-row";
    const isYou = m.name === "me";
    row.innerHTML =
      '<span class="mini-name">' + escapeHtml(m.name) + (isYou ? ' <span class="muted">(you)</span>' : "") + "</span>" +
      '<span class="pill type-completed">' + escapeHtml(m.role) + "</span>";
    el.appendChild(row);
  }
}

async function refreshLibrary() {
  try {
    S.rows = (await api("/api/projects/" + PID + "/sources")).sources;
  } catch (e) {
    toast("Failed to load library: " + e.message, "err");
    return;
  }
  const count = $("#dir-count");
  count.textContent = S.rows.length ? S.rows.length + " source" + (S.rows.length === 1 ? "" : "s") : "";
  renderLibrary();
  if (S.current && !S.rows.find(s => s.id === S.current.id)) resetNoSource();
}

function typePill(stype) {
  return '<span class="pill type-' + stype.replace("+", "\\+") + '">' + escapeHtml(stype) + "</span>";
}

function renderLibrary() {
  const list = $("#dir-list");
  if (!S.rows.length) {
    list.innerHTML = '<div class="empty">No sources in this project yet.<br>Add some with <b>+ Add</b>.</div>';
    return;
  }
  list.innerHTML = "";
  for (const s of S.rows) {
    const el = document.createElement("div");
    el.className = "dir-item" + (S.current && s.id === S.current.id ? " active" : "");
    el.dataset.id = s.id;
    const cat = s.category ? '<span class="cat">' + escapeHtml(s.category) + "</span>" : "";
    el.innerHTML =
      '<div class="dir-item-top">' +
        '<span class="dir-title" title="' + escapeHtml(s.title) + '">' + escapeHtml(s.title) + "</span>" +
        typePill(s.source_type) +
        '<button class="del" data-remove="' + s.id + '" title="Remove from project">×</button>' +
      "</div>" +
      '<div class="dir-sub">' + cat +
        '<a class="linkbtn" href="/#src=' + s.id + '" target="_blank" title="Open in personal space">personal ↗</a>' +
      "</div>";
    list.appendChild(el);
  }
}

async function selectSource(id) {
  const s = S.rows.find(x => x.id === id);
  if (!s) return;
  if (S.noteDirty) await saveNote();
  S.current = s;
  renderLibrary();
  await loadResource(s);
  await loadNote(s);
  applyNoteMode();
}

function resetNoSource() {
  S.current = null;
  $("#res-title").textContent = "Resource";
  $("#res-meta").textContent = "";
  $("#res-meta").classList.add("hidden");
  const frame = $("#res-frame"), md = $("#res-md"), empty = $("#res-empty");
  frame.classList.add("hidden"); frame.src = "about:blank";
  md.classList.add("hidden"); md.innerHTML = "";
  empty.classList.remove("hidden");
  empty.textContent = "Select a source from the library to read it and edit the shared notes next to it.";
  $("#note-editor").value = "";
  $("#note-preview").innerHTML = '<p class="muted">Select a source in the library to read and edit its notes.</p>';
  $("#note-editor").classList.add("hidden");
  $("#note-preview").classList.remove("hidden");
  $("#note-save-state").textContent = "";
}

async function loadResource(s) {
  $("#res-title").textContent = s.title;
  $("#res-meta").textContent = s.source_type;
  $("#res-meta").classList.remove("hidden");
  const frame = $("#res-frame"), md = $("#res-md"), empty = $("#res-empty");
  empty.classList.add("hidden");
  frame.classList.add("hidden"); md.classList.add("hidden");
  const src = "/api/source/" + s.id + "/content";
  if (s.source_type === "md") {
    try {
      const res = await fetch(src);
      if (!res.ok) throw new Error("HTTP " + res.status);
      md.innerHTML = renderMarkdown(await res.text());
      md.classList.remove("hidden");
    } catch (e) {
      empty.textContent = "Could not load the markdown source: " + e.message;
      empty.classList.remove("hidden");
    }
  } else {
    frame.src = src;
    frame.classList.remove("hidden");
  }
}

/* ---------- Notes ---------- */

async function loadNote(s) {
  try {
    const data = await api("/api/note/" + s.id);
    $("#note-editor").value = data.content;
    $("#note-preview").innerHTML = renderMarkdown(data.content);
    $("#note-save-state").textContent = "";
  } catch (e) {
    toast("Failed to load note: " + e.message, "err");
  }
}

function onNoteTyped() {
  if (!S.current) return;
  S.noteDirty = true;
  $("#note-save-state").textContent = "unsaved…";
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(saveNote, 1200);
}

async function saveNote() {
  if (!S.current || !S.noteDirty) return;
  const content = $("#note-editor").value;
  S.noteDirty = false;
  try {
    await api("/api/note/" + S.current.id, { method: "PUT", body: { content } });
    $("#note-save-state").textContent = "saved";
  } catch (e) {
    S.noteDirty = true;
    $("#note-save-state").textContent = "save error";
    toast("Note save failed: " + e.message, "err");
  }
}

function applyNoteMode() {
  const edit = S.noteMode === "edit";
  $("#note-editor").classList.toggle("hidden", !edit);
  $("#note-preview").classList.toggle("hidden", edit);
  if (!edit) $("#note-preview").innerHTML = renderMarkdown($("#note-editor").value);
  else if (S.current) $("#note-editor").focus();
}

$("#note-editor").addEventListener("input", onNoteTyped);
$("#note-editor").addEventListener("blur", () => { if (S.noteDirty) saveNote(); });
document.addEventListener("keydown", e => {
  if (e.ctrlKey && e.key.toLowerCase() === "d") {
    e.preventDefault();
    if (!S.current) return;
    S.noteMode = S.noteMode === "edit" ? "preview" : "edit";
    applyNoteMode();
  }
});


/* ---------- Add / remove sources ---------- */

$("#btn-add-source").addEventListener("click", openAddSources);
$("#dir-list").addEventListener("click", async e => {
  const rm = e.target.closest("[data-remove]");
  if (rm) {
    e.stopPropagation();
    await removeSource(rm.dataset.remove);
    return;
  }
  const item = e.target.closest(".dir-item");
  if (item) await selectSource(item.dataset.id);
});

async function removeSource(sid) {
  const s = S.rows.find(x => x.id === sid);
  const title = s ? s.title : sid;
  if (!confirm('Remove "' + title + '" from this project? The source stays in your personal library.')) return;
  try {
    await api("/api/projects/" + PID + "/sources/" + sid, { method: "DELETE" });
    if (S.current && S.current.id === sid) resetNoSource();
    await refreshLibrary();
    toast("Source removed from project", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function openAddSources() {
  if (!S.sources.length) {
    try { S.sources = (await api("/api/sources")).sources; }
    catch (e) { toast("Could not load your library: " + e.message, "err"); return; }
  }
  renderAddSources();
  openModal("modal-add-src");
}

function renderAddSources() {
  const inProject = new Set(S.rows.map(s => s.id));
  const list = $("#add-src-list");
  const filter = $("#add-src-filter");
  const draw = () => {
    const f = filter.value.trim().toLowerCase();
    list.innerHTML = "";
    for (const s of S.sources) {
      if (inProject.has(s.id)) continue;
      if (f && s.title.toLowerCase().indexOf(f) === -1) continue;
      const row = document.createElement("label");
      row.className = "check-row";
      row.innerHTML = '<input type="checkbox" value="' + s.id + '"><span>' + escapeHtml(s.title) + "</span>" + typePill(s.source_type);
      list.appendChild(row);
    }
    if (!list.children.length) list.innerHTML = '<p class="muted">No more sources to add.</p>';
  };
  filter.value = "";
  filter.oninput = draw;
  draw();
}

$("#add-src-submit").addEventListener("click", async () => {
  const ids = $$("#add-src-list input:checked").map(i => i.value);
  if (!ids.length) { toast("Select at least one source", "warn"); return; }
  try {
    for (const sid of ids) await api("/api/projects/" + PID + "/sources", { method: "POST", body: { source_id: sid } });
    closeModal("modal-add-src");
    toast(ids.length + " source" + (ids.length === 1 ? "" : "s") + " added", "ok");
    await refreshLibrary();
  } catch (e) {
    toast("Add failed: " + e.message, "err");
  }
});


/* ---------- LaTeX (local placeholder) ---------- */

function texKey() { return "kndb.tex." + PID; }
function loadTexDraft() { $("#tex-editor").value = localStorage.getItem(texKey()) || ""; }

let _texTimer;
$("#tex-editor").addEventListener("input", () => {
  clearTimeout(_texTimer);
  _texTimer = setTimeout(() => {
    localStorage.setItem(texKey(), $("#tex-editor").value);
    toast("Draft saved (browser only)", "ok", 1500);
  }, 600);
});


/* ---------- Settings ---------- */

function renderSettings() {
  const p = S.project;
  if (!p) return;
  $("#set-name").value = p.name || "";
  $("#set-desc").value = p.description || "";
  $("#btn-toggle-complete").textContent = p.completed ? "Reopen project" : "Mark completed";
  renderMembers();
}

function renderMembers() {
  const el = $("#prj-members");
  const p = S.project;
  if (!p) return;
  el.innerHTML = "";
  for (const m of p.members || []) {
    const row = document.createElement("div");
    row.className = "mini-row";
    const isYou = m.name === "me";
    row.innerHTML =
      '<span class="mini-name">' + escapeHtml(m.name) + (isYou ? ' <span class="muted">(you)</span>' : "") + "</span>" +
      '<select class="input small role-sel"' + (isYou ? " disabled" : "") + ">" +
        ROLES.map(r => '<option value="' + r + '"' + (r === m.role ? " selected" : "") + ">" + r + "</option>").join("") +
      "</select>" +
      (isYou ? "" : '<button class="btn small del" title="Remove member">×</button>');
    const sel = row.querySelector(".role-sel");
    sel.addEventListener("change", async () => {
      try {
        const d = await api("/api/projects/" + PID + "/members/" + encodeURIComponent(m.name) + "/role",
          { method: "POST", body: { role: sel.value } });
        S.project.members = d.members;
        renderMembers();
        toast("Role updated to " + sel.value, "ok");
      } catch (e) {
        toast(e.message, "err");
        renderMembers();
      }
    });
    const delBtn = row.querySelector(".del");
    if (delBtn) delBtn.addEventListener("click", async () => {
      if (!confirm("Remove " + m.name + " from the project?")) return;
      try {
        const d = await api("/api/projects/" + PID + "/members/" + encodeURIComponent(m.name),
          { method: "DELETE" });
        S.project.members = d.members;
        renderMembers();
        toast("Member removed", "ok");
      } catch (e) {
        toast(e.message, "err");
      }
    });
    el.appendChild(row);
  }
  if (!(p.members || []).length) el.innerHTML = '<p class="muted">No members.</p>';
}

$("#btn-save-detail").addEventListener("click", async () => {
  const name = $("#set-name").value.trim();
  if (!name) { toast("Project name required", "warn"); return; }
  try {
    const d = await api("/api/projects/" + PID, {
      method: "POST",
      body: { name: name, description: $("#set-desc").value },
    });
    S.project = d.project;
    $("#sub-name").textContent = d.project.name;
    document.title = d.project.name + " — KNDB";
    toast("Details saved", "ok");
  } catch (e) {
    toast("Save failed: " + e.message, "err");
  }
});

$("#btn-toggle-complete").addEventListener("click", async () => {
  const next = !S.project.completed;
  if (next && !confirm("Archive this project as completed? (read-only state)")) return;
  if (!next && !confirm("Reopen this project?")) return;
  try {
    const d = await api("/api/projects/" + PID, { method: "POST", body: { completed: next } });
    S.project = d.project;
    $("#prj-badge").classList.toggle("hidden", !d.project.completed);
    $("#btn-toggle-complete").textContent = d.project.completed ? "Reopen project" : "Mark completed";
    toast(next ? "Project archived as completed" : "Project reopened", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-del").addEventListener("click", async () => {
  if (!confirm("Delete this project? Its members/links are removed; sources stay in your library.")) return;
  try {
    await api("/api/projects/" + PID, { method: "DELETE" });
    toast("Project deleted", "ok");
    location.href = "/projects";
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-add-member").addEventListener("click", async () => {
  const name = $("#member-name").value.trim();
  if (!name) { toast("Member name required", "warn"); return; }
  const role = $("#member-role").value;
  try {
    const d = await api("/api/projects/" + PID + "/members", { method: "POST", body: { name: name, role: role } });
    S.project.members = d.members;
    $("#member-name").value = "";
    renderMembers();
    toast("Member added", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
});


/* ---------- Splitters (library layout) ---------- */

function setupSplitter(handle, leftPanel, storageKey, min, max) {
  const saved = parseFloat(localStorage.getItem(storageKey));
  if (saved) leftPanel.style.flex = "0 0 " + (saved * 100).toFixed(2) + "%";
  handle.addEventListener("mousedown", function (e) {
    e.preventDefault();
    document.body.classList.add("resizing");
    const onMove = function (ev) {
      const rect = $("#tab-library").getBoundingClientRect();
      let f = (ev.clientX - rect.left) / rect.width;
      f = Math.max(min, Math.min(max, f));
      leftPanel.style.flex = "0 0 " + (f * 100).toFixed(2) + "%";
      localStorage.setItem(storageKey, String(f));
    };
    const onUp = function () {
      document.body.classList.remove("resizing");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
}

function setupSplitters() {
  setupSplitter(document.querySelector('.splitter[data-split="dir"]'), $("#panel-dir"), "kndb.w.prj.dir", 0.14, 0.72);
  setupSplitter(document.querySelector('.splitter[data-split="res"]'), $("#panel-res"), "kndb.w.prj.res", 0.2, 0.8);
}


/* ---------- Search filter ---------- */

let _searchTimer;
$("#search").addEventListener("input", function () {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(function () {
    const f = $("#search").value.trim().toLowerCase();
    $$("#dir-list .dir-item").forEach(function (el) {
      const s = S.rows.find(x => x.id === el.dataset.id);
      el.style.display = (s && (!f || s.title.toLowerCase().indexOf(f) !== -1)) ? "" : "none";
    });
  }, 200);
});


/* ---------- Modals / init ---------- */

document.addEventListener("click", e => {
  const b = e.target.closest("[data-close]");
  if (b) closeModal(b.dataset.close);
});
$$(".modal").forEach(m => m.addEventListener("mousedown", e => {
  if (e.target === m) m.classList.add("hidden");
}));

loadProject();
