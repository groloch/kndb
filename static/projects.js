"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const S = { projects: [] };

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

function modalEl(id) { return $(id.startsWith("#") ? id : `#${id}`); }
function openModal(id) { modalEl(id).classList.remove("hidden"); }
function closeModal(id) { modalEl(id).classList.add("hidden"); }

function fmtDate(s) {
  if (!s) return "";
  const d = new Date(String(s).length === 10 ? Number(s) * 1000 : s);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

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
    list.className = "prj-list grid";
    list.innerHTML = '<div class="empty card-empty">No projects yet.<br>Create one with <b>+ New project</b>.</div>';
    return;
  }
  list.innerHTML = "";
  for (const p of S.projects) {
    const el = document.createElement("a");
    el.className = "prj-card";
    el.href = "/projects/" + encodeURIComponent(p.id);
    const desc = (p.description || "").trim();
    el.innerHTML = `
      <div class="prj-card-head">
        <span class="prj-card-title" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>
        ${p.completed ? '<span class="pill type-completed">completed</span>' : ""}
      </div>
      <div class="prj-card-desc">${desc ? escapeHtml(desc) : '<span class="muted">No description.</span>'}</div>
      <div class="prj-card-foot">
        <span class="chip">${p.source_count} source${p.source_count === 1 ? "" : "s"}</span>
        <span class="chip">${p.member_count} member${p.member_count === 1 ? "" : "s"}</span>
        ${p.created_at ? `<span class="prj-card-date">created ${escapeHtml(fmtDate(p.created_at))}</span>` : ""}
      </div>`;
    list.appendChild(el);
  }
}

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
    location.href = "/projects/" + encodeURIComponent(d.project.id);
  } catch (e) {
    toast("Create failed: " + e.message, "err");
    btn.disabled = false;
  }
});

let _st;
$("#prj-search").addEventListener("input", () => {
  clearTimeout(_st);
  _st = setTimeout(refreshProjects, 300);
});

document.addEventListener("click", e => {
  const b = e.target.closest("[data-close]");
  if (b) closeModal(b.dataset.close);
});
$$(".modal").forEach(m => m.addEventListener("mousedown", e => {
  if (e.target === m) m.classList.add("hidden");
}));

refreshProjects();
