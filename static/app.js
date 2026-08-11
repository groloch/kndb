"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const S = {
  sources: [],
  current: null,
  noteMode: "preview",
  noteDirty: false,
  saveTimer: null,
};


function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

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

function streamSSE(path, body) {
  return new Promise((resolve, reject) => {
    fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(async res => {
      if (!res.ok || !res.body) {
        let data = null;
        try { data = await res.json(); } catch (_) {}
        reject(new Error((data && data.error) || `HTTP ${res.status}`));
        return;
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of block.split("\n")) {
            if (!line.startsWith("data:")) continue;
            let ev;
            try { ev = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
            if (ev.type === "error") { reject(new Error(ev.error || "LLM task failed")); return; }
            if (ev.type === "done") { resolve(ev); return; }
          }
        }
      }
      reject(new Error("LLM stream ended without a result"));
    }).catch(reject);
  });
}

let _toastTimer;

function toast(msg, kind = "info", ms = 4200) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = "toast"; }, ms);
}

function setBusy(btn, busy, label) {
  if (!btn) return;
  if (busy) {
    if (!btn.dataset.orig) btn.dataset.orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = label;
  } else {
    btn.disabled = false;
    btn.textContent = btn.dataset.orig || label;
  }
}

function modalEl(id) {
  return $(id.startsWith("#") ? id : `#${id}`);
}

function openModal(id) { modalEl(id).classList.remove("hidden"); }
function closeModal(id) { modalEl(id).classList.add("hidden"); }


function renderMarkdown(md) {
  if (!md) return "";
  let html;
  if (window.marked) {
    html = window.marked.parse(md, { breaks: true, gfm: true });
  } else {
    html = miniMarkdown(md);
  }
  if (window.DOMPurify) return DOMPurify.sanitize(html);
  return html.replace(/<script[\s\S]*?<\/script>/gi, "");
}

function miniMarkdown(md) {
  const esc = md.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = esc.split(/\r?\n/);
  const out = [];
  let inCode = false;
  let para = [];
  const flushP = () => {
    if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; }
  };
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

function inline(t) {
  return t
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

async function refreshDirectory() {
  const q = $("#search").value.trim();
  let data;
  try {
    data = await api("/api/sources" + (q ? "?q=" + encodeURIComponent(q) : ""));
  } catch (e) {
    toast("Failed to load sources: " + e.message, "err");
    return;
  }
  S.sources = data.sources;
  renderDirectory();
}

function renderDirectory() {
  const list = $("#dir-list");
  if (!S.sources.length) {
    list.innerHTML = '<div class="empty">No sources yet.<br>Add one with <b>+ Import</b>.</div>';
    return;
  }
  list.innerHTML = "";
  for (const s of S.sources) {
    const el = document.createElement("div");
    el.className = "dir-item" + (S.current && s.id === S.current.id ? " active" : "");
    el.dataset.id = s.id;
    const tags = (s.tags || "").split(",").map(t => t.trim()).filter(Boolean)
      .map(t => `<span class="chip">@${escapeHtml(t)}</span>`).join("");
    const cat = s.category ? `<span class="cat">${escapeHtml(s.category)}</span>` : "";
    el.innerHTML = `
      <div class="dir-item-top">
        <span class="dir-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</span>
        <span class="pill type-${s.source_type.replace("+", "\\+")}">${escapeHtml(s.source_type)}</span>
        <button class="del" data-del="${s.id}" title="Delete source">×</button>
      </div>
      <div class="dir-sub">${cat}${tags}<span class="qcount" title="questions in quiz">${s.num_questions}Q</span></div>`;
    list.appendChild(el);
  }
}

async function selectSource(id, noteMode = "preview") {
  const s = S.sources.find(x => x.id === id);
  if (!s) return;
  if (S.noteDirty) await saveNote();
  S.current = s;
  renderDirectory();
  setToolbar(s);
  await loadResource(s);
  await loadNote(s);
  S.noteMode = noteMode;
  applyNoteMode();
  $("#meta-tags").value = s.tags || "";
  $("#meta-category").value = s.category || "";
  $("#res-footer").classList.remove("hidden");
}

function setToolbar(s) {
  const on = !!s;
  ["btn-quiz-gen", "btn-quiz-play", "btn-summarize"].forEach(id => {
    $(`#${id}`).disabled = !on;
  });
}

function resetNoSource() {
  S.current = null;
  $("#res-title").textContent = "Resource";
  $("#res-meta").textContent = "";
  $("#res-meta").classList.add("hidden");
  $("#res-frame").classList.add("hidden");
  $("#res-frame").src = "about:blank";
  $("#res-md").classList.add("hidden");
  $("#res-md").innerHTML = "";
  $("#res-empty").classList.remove("hidden");
  $("#res-footer").classList.add("hidden");
  $("#note-editor").value = "";
  $("#note-preview").innerHTML = '<p class="muted">Select a source in the directory to read and edit its notes.</p>';
  $("#note-editor").classList.add("hidden");
  $("#note-preview").classList.remove("hidden");
  $("#note-save-state").textContent = "";
  setToolbar(null);
}

async function loadResource(s) {
  $("#res-title").textContent = s.title;
  $("#res-meta").textContent = s.source_type;
  $("#res-meta").classList.remove("hidden");
  const frame = $("#res-frame"), md = $("#res-md"), empty = $("#res-empty");
  empty.classList.add("hidden");
  frame.classList.add("hidden");
  md.classList.add("hidden");
  const src = `/api/source/${s.id}/content`;
  if (s.source_type === "md") {
    try {
      const res = await fetch(src);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
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



async function loadNote(s) {
  try {
    const data = await api(`/api/note/${s.id}`);
    S.noteContent = data.content;
    $("#note-editor").value = S.noteContent;
    $("#note-preview").innerHTML = renderMarkdown(S.noteContent);
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
    await api(`/api/note/${S.current.id}`, { method: "PUT", body: { content } });
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
  if (!edit) {
    $("#note-preview").innerHTML = renderMarkdown($("#note-editor").value);
  } else if (S.current) {
    $("#note-editor").focus();
  }
}

$("#btn-import").addEventListener("click", () => {
  $("#import-url").value = "";
  $("#import-file").value = "";
  openModal("#modal-import");
});

$("#import-submit").addEventListener("click", async () => {
  const url = $("#import-url").value.trim();
  const file = $("#import-file").files[0];
  if (!url && !file) { toast("Enter a URL or choose a file", "warn"); return; }
  const fd = new FormData();
  if (url) fd.append("url", url);
  if (file) fd.append("file", file);
  const btn = $("#import-submit");
  setBusy(btn, true, "Importing…");
  try {
    const data = await api("/api/import", { method: "POST", body: fd });
    closeModal("#modal-import");
    if (data.duplicate) {
      toast(`Already imported — opened existing “${data.title}”`, "warn");
      await refreshDirectory();
      await selectSource(data.id, "preview");
    } else {
      toast(`Imported “${data.title}” (${data.source_type})`, "ok");
      await refreshDirectory();
      await selectSource(data.id, "edit");
    }
  } catch (e) {
    toast("Import failed: " + e.message, "err", 9000);
  } finally {
    setBusy(btn, false, "Import");
  }
});

$("#meta-save").addEventListener("click", async () => {
  if (!S.current) return;
  try {
    const d = await api(`/api/source/${S.current.id}/meta`, {
      method: "POST",
      body: { tags: $("#meta-tags").value, category: $("#meta-category").value },
    });
    S.current.tags = d.tags;
    S.current.category = d.category;
    await refreshDirectory();
    toast("Metadata saved", "ok");
  } catch (e) {
    toast("Failed to save metadata: " + e.message, "err");
  }
});

async function deleteSource(id) {
  if (!confirm("Delete this source, its notes, quiz and stats?")) return;
  try {
    await api("/api/source/" + id, { method: "DELETE" });
    if (S.current && S.current.id === id) resetNoSource();
    await refreshDirectory();
    toast("Source deleted", "ok");
  } catch (e) {
    toast("Delete failed: " + e.message, "err");
  }
}

$("#dir-list").addEventListener("click", async e => {
  const del = e.target.closest("[data-del]");
  if (del) { e.stopPropagation(); await deleteSource(del.dataset.del); return; }
  const item = e.target.closest(".dir-item");
  if (item) await selectSource(item.dataset.id, "preview");
});

$("#btn-quiz-gen").addEventListener("click", () => openModal("#modal-quiz-gen"));

$("#quiz-gen-submit").addEventListener("click", async () => {
  if (!S.current) return;
  const btn = $("#quiz-gen-submit");
  const body = {
    scope: $('input[name="qz-scope"]:checked').value,
    num_questions: parseInt($("#qz-num").value, 10),
    difficulty: $("#qz-difficulty").value,
    language: $("#qz-language").value.trim() || "English",
  };
  setBusy(btn, true, "Generating…");
  try {
    await saveNote();
    const done = await streamSSE(`/api/quiz/${S.current.id}/generate`, body);
    closeModal("#modal-quiz-gen");
    toast(`Quiz ready: ${done.questions} questions`, "ok");
    await refreshDirectory();
  } catch (e) {
    toast("Quiz generation failed: " + e.message, "err", 10000);
  } finally {
    setBusy(btn, false, "Generate");
  }
});

$("#btn-summarize").addEventListener("click", () => openModal("#modal-summarize"));

$("#summarize-submit").addEventListener("click", async () => {
  if (!S.current) return;
  const btn = $("#summarize-submit");
  const body = {
    scope: $('input[name="su-scope"]:checked').value,
    length: $("#su-length").value,
    language: $("#su-language").value.trim() || "English",
  };
  setBusy(btn, true, "Summarizing…");
  try {
    await saveNote();
    await streamSSE(`/api/summarize/${S.current.id}`, body);
    closeModal("#modal-summarize");
    toast("Summary appended to your notes", "ok");
    await loadNote(S.current);
    applyNoteMode();
  } catch (e) {
    toast("Summarize failed: " + e.message, "err", 10000);
  } finally {
    setBusy(btn, false, "Summarize");
  }
});

$("#btn-quiz-play").addEventListener("click", async () => {
  if (!S.current) return;
  try {
    const quiz = await api(`/api/quiz/${S.current.id}`);
    if (!quiz.questions || !quiz.questions.length) {
      toast("No quiz yet — click 'Generate quiz' first", "warn");
      return;
    }
    const order = await api(`/api/quiz/${S.current.id}/order`);
    startQuizSession(quiz, order.questions);
  } catch (e) {
    toast("Could not start quiz: " + e.message, "err");
  }
});

function startQuizSession(quiz, questions) {
  const body = $("#quiz-body");
  const queue = [...questions];
  let total = 0, correct = 0;
  const attempted = new Set();
  const requeued = new Set();

  openModal("#modal-quiz-play");
  renderIntro();

  function renderIntro() {
    body.innerHTML = `
      <div class="quiz-summary">
        <p class="muted">${escapeHtml(quiz.source_id ? "" : "")}</p>
        <p><strong>${questions.length}</strong> questions · due-for-review first · wrong answers come back once</p>
        <button class="btn primary" id="qz-start">Start session</button>
      </div>`;
    $("#qz-start").addEventListener("click", next);
  }

  function next() {
    if (!queue.length) return renderSummary();
    const q = queue.shift();
    renderQuestion(q);
  }

  function renderQuestion(q) {
    const opts = q.answers.map((text, idx) => ({ text, idx }));
    for (let i = opts.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [opts[i], opts[j]] = [opts[j], opts[i]];
    }
    body.innerHTML = `
      <div class="quiz-toolbar"><span>${total + 1}/${questions.length}</span>
        <span class="quiz-progress">success so far: ${correct}</span></div>
      <h3 class="quiz-question">${escapeHtml(q.question)}</h3>
      <div class="quiz-answers"></div>
      <div class="quiz-footer">
        <span class="quiz-feedback"></span>
        <button class="btn hidden" id="qz-next">Next</button>
      </div>`;
    const answersEl = $(".quiz-answers", body);
    for (const o of opts) {
      const b = document.createElement("button");
      b.className = "btn ans";
      b.dataset.idx = o.idx;
      b.textContent = o.text;
      b.addEventListener("click", () => answer(q, o, b));
      answersEl.appendChild(b);
    }
    $("#quiz-body").scrollTop = 0;
  }

  function answer(q, chosen, btn) {
    const ok = chosen.idx === q.answer_index;
    attempted.add(q.id);
    total++;
    if (ok) correct++;
    else if (!requeued.has(q.id)) { requeued.add(q.id); queue.push(q); }
    $$(".ans", body).forEach(b => { b.disabled = true; });
    $$(".ans", body).forEach(b => {
      if (+b.dataset.idx === q.answer_index) b.classList.add("correct");
      else if (b === btn) b.classList.add("wrong");
    });
    const fb = $(".quiz-feedback", body);
    fb.textContent = ok
      ? "✓ Correct"
      : `✗ Incorrect — correct answer: ${q.answers[q.answer_index]}`;
    fb.className = "quiz-feedback " + (ok ? "ok" : "no");
    const nxt = $("#qz-next");
    nxt.classList.remove("hidden");
    nxt.addEventListener("click", next);
    api(`/api/quiz/${S.current.id}/answer`, {
      method: "POST",
      body: { question_id: q.id, success: ok },
    }).catch(() => {});
  }

  function renderSummary() {
    const pct = Math.round(100 * correct / Math.max(total, 1));
    body.innerHTML = `
      <div class="quiz-summary">
        <h3>Session finished</h3>
        <p class="big">${correct} / ${total} correct (${pct}%)</p>
        <p class="muted">Results are stored in the source stats — questions you missed
          are scheduled for review sooner.</p>
        <button class="btn primary" id="qz-restart">Play again</button>
        <button class="btn" data-close="modal-quiz-play">Close</button>
      </div>`;
    $("#qz-restart").addEventListener("click", () => startQuizSession(quiz, questions));
    refreshDirectory();
  }
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

document.addEventListener("click", e => {
  const b = e.target.closest("[data-close]");
  if (b) closeModal(b.dataset.close);
});
$$(".modal").forEach(m => m.addEventListener("mousedown", e => {
  if (e.target === m) m.classList.add("hidden");
}));

let _searchTimer;
$("#search").addEventListener("input", () => {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(refreshDirectory, 300);
});

function setupSplitter(handle, leftPanel, storageKey, min = 0.14, max = 0.72) {
  const saved = parseFloat(localStorage.getItem(storageKey));
  if (saved) leftPanel.style.flex = `0 0 ${(saved * 100).toFixed(2)}%`;
  handle.addEventListener("mousedown", e => {
    e.preventDefault();
    document.body.classList.add("resizing");
    const onMove = ev => {
      const rect = $("#main").getBoundingClientRect();
      let f = (ev.clientX - rect.left) / rect.width;
      f = Math.max(min, Math.min(max, f));
      leftPanel.style.flex = `0 0 ${(f * 100).toFixed(2)}%`;
      localStorage.setItem(storageKey, String(f));
    };
    const onUp = () => {
      document.body.classList.remove("resizing");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
}

async function init() {
  setupSplitter($('.splitter[data-split="dir"]'), $("#panel-dir"), "kndb.w.dir");
  setupSplitter($('.splitter[data-split="res"]'), $("#panel-res"), "kndb.w.res");
  await refreshDirectory();
  const m = location.hash.match(/^#src=([\w]+)/);
  if (m) {
    const srcId = decodeURIComponent(m[1]);
    if (S.sources.some(s => s.id === srcId)) await selectSource(srcId, "preview");
  }
  api("/api/llm/status").then(d => {
    const el = $("#llm-status");
    el.textContent = d.loaded ? "LLM ready" : (d.last_error ? "LLM: offline" : "LLM: connecting…");
    el.title = (d.last_error || d.model || "");
  }).catch(() => {});
}

init();
