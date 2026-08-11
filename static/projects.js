"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const ROLES = ["owner", "maintainer", "contributor", "spectator"];
const S = { projects: [], sources: [], current: null };



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

function modalEl(id) {
  return $(id.startsWith("#") ? id : `#${id}`);
}

function openModal(id) { modalEl(id).classList.remove("hidden"); }
function closeModal(id) { modalEl(id).classList.add("hidden"); }


async function refreshProjects() {
  const q = $("#prj-search").value.trim();
  try {
    const d = await api("/api/projects" + (q ? "?q=" + encodeURIComponent(q) : ""));
    S.projects = d.projects;
  } catch (e) {
    toast("Failed to load projects: " + e.message, "err");
    return;
  }
  renderList();
}

function renderList() {
  const list = $("#prj-list");
  if (!S.projects.length) {
    list.innerHTML = '<div class="empty">No projects yet.<br>Create one with <b>+ New project</b>.</div>';
    return;
  }
  list.innerHTML = "";
  for (const p of S.projects) {
    const el = document.createElement("div");
    el.className = "prj-item";
    el.innerHTML = `
      <div class="dir-item-top">
        <span class="dir-title">${escapeHtml(p.name)}</span>
        ${p.completed ? '<span class="pill type-completed">completed</span>' : ""}
      </div>
      <div class="dir-sub">
        <span class="cat">${escapeHtml(p.description || "—")}</span>
      </div>
      <div class="dir-sub">
        <span class="chip">${p.member_count} member${p.member_count === 1 ? "" : "s"}</span>
        <span class="chip">${p.source_count} source${p.source_count === 1 ? "" : "s"}</span>
      </div>`;
    el.addEventListener("click", () => openProject(p.id));
    list.appendChild(el);
  }
}


async function openProject(id) {
  try {
    S.current = (await api(`/api/projects/${id}`)).project;
    S.sources = (await api("/api/sources")).sources;
  } catch (e) {
    toast("Failed to open project: " + e.message, "err");
    return;
  }
  $("#view-list").classList.add("hidden");
  $("#view-detail").classList.remove("hidden");
  renderDetail();
}

function renderDetail() {
  const p = S.current;
  $("#prj-title").textContent = p.name;
  const badge = $("#prj-badge");
  if (p.completed) {
    badge.textContent = "completed";
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
  $("#prj-desc").value = p.description || "";
  const tc = $("#btn-toggle-complete");
  tc.textContent = p.completed ? "Reopen project" : "Mark completed";
  renderMembers();
  renderProjectSources();
  loadTexDraft();
}

function renderMembers() {
  const el = $("#prj-members");
  const p = S.current;
  el.innerHTML = "";
  for (const m of p.members || []) {
    const row = document.createElement("div");
    row.className = "mini-row";
    const isYou = m.name === "me";
    row.innerHTML = `
      <span class="mini-name">${escapeHtml(m.name)}${isYou ? ' <span class="muted">(you)</span>' : ""}</span>
      <select class="input small role-sel" ${isYou ? "disabled" : ""}>
        ${ROLES.map(r => `<option value="${r}" ${r === m.role ? "selected" : ""}>${r}</option>`).join("")}
      </select>
      ${isYou ? "" : '<button class="btn small del" title="Remove member">×</button>'}`;
    const sel = $(".role-sel", row);
    sel.addEventListener("change", async () => {
      try {
        const d = await api(`/api/projects/${p.id}/members/${encodeURIComponent(m.name)}/role`,
          { method: "POST", body: { role: sel.value } });
        S.current.members = d.members;
        renderMembers();
        toast(`Role updated to ${sel.value}`, "ok");
      } catch (e) {
        toast(e.message, "err");
        renderMembers();
      }
    });
    const del = $(".del", row);
    if (del) del.addEventListener("click", async () => {
      if (!confirm(`Remove ${m.name} from the project?`)) return;
      try {
        const d = await api(`/api/projects/${p.id}/members/${encodeURIComponent(m.name)}`,
          { method: "DELETE" });
        S.current.members = d.members;
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

async function renderProjectSources() {
  const el = $("#prj-sources");
  const p = S.current;
  let rows;
  try {
    rows = (await api(`/api/projects/${p.id}/sources`)).sources;
  } catch (e) {
    el.innerHTML = '<p class="muted">Error loading sources.</p>';
    return;
  }
  p.sourceRows = rows;
  if (!rows.length) {
    el.innerHTML = '<p class="muted">No sources yet — add some from your library.</p>';
    return;
  }
  el.innerHTML = "";
  for (const s of rows) {
    const row = document.createElement("div");
    row.className = "mini-row";
    row.innerHTML = `
      <span class="mini-name" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</span>
      <span class="pill type-${s.source_type.replace("+", "\\+")}">${escapeHtml(s.source_type)}</span>
      <span class="mini-actions">
        <a class="btn small" href="/#src=${s.id}" target="_blank">open</a>
        <button class="btn small del" title="Remove from project">×</button>
      </span>`;
    $(".del", row).addEventListener("click", async () => {
      try {
        const d = await api(`/api/projects/${p.id}/sources/${s.id}`, { method: "DELETE" });
        p.sourceRows = d.sources;
        renderProjectSources();
        toast("Source removed from project", "ok");
      } catch (e) {
        toast(e.message, "err");
      }
    });
    el.appendChild(row);
  }
}


function texKey() {
  return "kndb.tex." + (S.current && S.current.id);
}

function loadTexDraft() {
  $("#tex-editor").value = localStorage.getItem(texKey()) || "";
}

let _texTimer;
$("#tex-editor").addEventListener("input", () => {
  clearTimeout(_texTimer);
  _texTimer = setTimeout(() => {
    localStorage.setItem(texKey(), $("#tex-editor").value);
    toast("Draft saved (browser only)", "ok", 1500);
  }, 600);
});


$("#btn-new").addEventListener("click", () => {
  $("#new-name").value = "";
  $("#new-desc").value = "";
  openModal("#modal-new");
});

$("#new-submit").addEventListener("click", async () => {
  const name = $("#new-name").value.trim();
  if (!name) { toast("Project name required", "warn"); return; }
  const btn = $("#new-submit");
  btn.disabled = true;
  try {
    const d = await api("/api/projects", { method: "POST", body: { name, description: $("#new-desc").value } });
    closeModal("#modal-new");
    toast("Project created", "ok");
    await refreshProjects();
    openProject(d.project.id);
  } catch (e) {
    toast("Create failed: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
});

$("#btn-back").addEventListener("click", () => {
  S.current = null;
  $("#view-detail").classList.add("hidden");
  $("#view-list").classList.remove("hidden");
  refreshProjects();
});

$("#btn-save-desc").addEventListener("click", async () => {
  try {
    const d = await api(`/api/projects/${S.current.id}`, {
      method: "POST", body: { description: $("#prj-desc").value },
    });
    S.current.description = d.project.description;
    toast("Description saved", "ok");
  } catch (e) {
    toast("Save failed: " + e.message, "err");
  }
});

$("#btn-toggle-complete").addEventListener("click", async () => {
  const next = !S.current.completed;
  if (next && !confirm("Archive this project as completed? (read-only state)") ) return;
  if (!next && !confirm("Reopen this project?")) return;
  try {
    const d = await api(`/api/projects/${S.current.id}`, { method: "POST", body: { completed: next } });
    S.current.completed = d.project.completed;
    renderDetail();
    toast(next ? "Project archived as completed" : "Project reopened", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-del").addEventListener("click", async () => {
  if (!confirm("Delete this project? Its members/links are removed; sources stay in your library.")) return;
  try {
    await api(`/api/projects/${S.current.id}`, { method: "DELETE" });
    toast("Project deleted", "ok");
    $("#btn-back").click();
  } catch (e) {
    toast(e.message, "err");
  }
});


$("#btn-add-member").addEventListener("click", async () => {
  const name = $("#member-name").value.trim();
  if (!name) { toast("Member name required", "warn"); return; }
  const role = $("#member-role").value;
  try {
    const d = await api(`/api/projects/${S.current.id}/members`, { method: "POST", body: { name, role } });
    S.current.members = d.members;
    $("#member-name").value = "";
    renderMembers();
    toast("Member added", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
});


$("#btn-add-source").addEventListener("click", () => {
  const inProject = new Set((S.current.sourceRows || []).map(s => s.id));
  const list = $("#add-src-list");
  const filter = $("#add-src-filter");
  list.innerHTML = "";
  const render = () => {
    const f = filter.value.trim().toLowerCase();
    list.innerHTML = "";
    for (const s of S.sources) {
      if (inProject.has(s.id)) continue;
      if (f && !s.title.toLowerCase().includes(f)) continue;
      const row = document.createElement("label");
      row.className = "check-row";
      row.innerHTML = `<input type="checkbox" value="${s.id}"><span>${escapeHtml(s.title)}</span>
        <span class="pill type-${s.source_type.replace("+", "\\+")}">${escapeHtml(s.source_type)}</span>`;
      list.appendChild(row);
    }
    if (!list.children.length) list.innerHTML = '<p class="muted">No more sources to add.</p>';
  };
  filter.value = "";
  filter.oninput = render;
  render();
  openModal("#modal-add-src");
});

$("#add-src-submit").addEventListener("click", async () => {
  const ids = $$("#add-src-list input:checked").map(i => i.value);
  if (!ids.length) { toast("Select at least one source", "warn"); return; }
  try {
    for (const sid of ids) await api(`/api/projects/${S.current.id}/sources`, { method: "POST", body: { source_id: sid } });
    closeModal("#modal-add-src");
    toast(`${ids.length} source${ids.length === 1 ? "" : "s"} added`, "ok");
    await renderProjectSources();
  } catch (e) {
    toast("Add failed: " + e.message, "err");
  }
});


$("#prj-search").addEventListener("input", () => {
  clearTimeout(S._st);
  S._st = setTimeout(refreshProjects, 300);
});

document.addEventListener("click", e => {
  const b = e.target.closest("[data-close]");
  if (b) closeModal(b.dataset.close);
});
$$(".modal").forEach(m => m.addEventListener("mousedown", e => {
  if (e.target === m) m.classList.add("hidden");
}));

refreshProjects();