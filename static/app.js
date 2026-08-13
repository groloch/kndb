"use strict";

/* Personal workspace page. $, api, toast, renderMarkdown, modals and the
 * identity header live in common.js, which must load first. */

const S = {
  pid: "",            // the workspace is a project like any other
  sources: [],
  standalone: [],     // standalone note pages in the workspace tree
  sel: null,          // {kind:"source"|"note"|"folder", id} — mirrors TREE.sel
  current: null,      // the selected source row, or null
  noteId: null,       // the note page being edited, whatever selected it
  noteVersion: 1,
  noteMode: "preview",
  noteDirty: false,
  saveTimer: null,
  streaming: false,
  quiz: null,          // quiz hub: the loaded quiz
  quizStats: null,     // quiz hub: its spaced-repetition stats
  qm: null,
  qmEditId: null,
};


function streamSSE(path, body, onToken) {
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
            if (ev.type === "token" && onToken) onToken(ev.text || "");
            if (ev.type === "error") { reject(new Error(ev.error || "LLM task failed")); return; }
            if (ev.type === "done") { resolve(ev); return; }
          }
        }
      }
      reject(new Error("LLM stream ended without a result"));
    }).catch(reject);
  });
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

/* ---------- The library tree ---------- */

const TREE = makeTree({
  host: $("#dir-list"),
  readOnly: () => false,
  role: () => KNDB.ownerRole,   // you own your own workspace
  showQuestions: () => true,
  removeTitle: "Delete source",
  empty: '<div class="empty">No sources yet.<br>Add one with <b>+ Import</b>.</div>',
  reload: refreshTree,
  onSelect: openRef,
  onRemoveSource: deleteSource,
  onDeleteNote: deleteNotePage,
});

$("#btn-new-folder").addEventListener("click", () => TREE.newFolder());
$("#btn-new-note").addEventListener("click", () => TREE.newNote());

async function refreshTree() {
  let t;
  try {
    t = await api(`/api/projects/${S.pid}/tree`);
  } catch (e) {
    toast("Failed to load sources: " + e.message, "err");
    return;
  }
  S.sources = t.sources;
  S.standalone = t.notes;
  const n = S.sources.length;
  $("#dir-count").textContent = n ? `${n} source${n === 1 ? "" : "s"}` : "";
  TREE.setData({ folders: t.folders, sources: t.sources, notes: t.notes });
  TREE.render();
  if (S.sel && !TREE.sel) resetNoSource();
}


/* ---------- Selection ---------- */

function selectSource(id, noteMode = "preview") {
  S.noteMode = noteMode;
  return TREE.setSelection({ kind: "source", id });
}

/** Every selection lands here, whatever opened it. */
async function openRef(ref) {
  if (S.streaming) { toast("Wait for the summary to finish first", "warn"); return; }
  if (S.noteDirty) await saveNote();
  S.sel = ref;
  // Closing is about the selection, not about having a document: a folder and a
  // standalone note can be closed too.
  $("#btn-close-res").disabled = !ref;
  if (!ref) return resetNoSource();
  if (ref.kind === "source") return openSource(ref.id);
  if (ref.kind === "note") return openStandalone(ref.id);
  return openFolder(ref.id);
}

async function openSource(id) {
  const s = S.sources.find(x => x.id === id);
  if (!s) return;
  S.current = s;
  setToolbar(s);
  await loadResource(s);
  await loadNote(s);
  applyNoteMode();
}

async function openFolder(path) {
  S.current = null;
  S.noteId = null;
  setToolbar(null);
  $("#note-editor").classList.add("hidden");
  $("#note-preview").classList.remove("hidden");
  $("#note-preview").innerHTML =
    `<p class="muted">Folder <b>${escapeHtml(path)}</b>. Its README is shown on the left; `
    + "add one with <b>+ Note</b> named <code>README</code>.</p>";
  const readme = (await api(`/api/projects/${S.pid}/readme?folder=`
                            + encodeURIComponent(path))).note;
  showCompiled(path, readme ? readme.content : "*No README in this folder yet.*");
}

async function openStandalone(nid) {
  S.current = null;
  setToolbar(null);
  const d = await api(`/api/notes/${nid}`);
  S.noteId = d.note.id;
  S.noteVersion = d.note.version;
  S.noteName = d.note.name;
  $("#note-editor").value = d.note.content;
  $("#note-save-state").textContent = "";
  S.noteDirty = false;
  // A standalone note has no document to show, so the viewer renders the note
  // itself — which makes the editor a live preview.
  S.noteMode = "edit";
  applyNoteMode();
  showCompiled(d.note.name, d.note.content);
  await ANCHORS.load();   // a standalone note has no source: this clears them
}

/** Hide every viewer. Each of the three is exclusive, and the PDF one holds a
 *  worker and a pile of canvases, so leaving it costs more than a class. */
function clearViewers() {
  PDFView.destroy();
  $("#res-frame").classList.add("hidden");
  $("#res-frame").src = "about:blank";
  $("#res-md").classList.add("hidden");
  $("#res-empty").classList.add("hidden");
}

function showCompiled(title, markdown) {
  $("#res-title").textContent = title || "Note";
  $("#res-meta").classList.add("hidden");
  clearViewers();
  const md = $("#res-md");
  md.innerHTML = renderMarkdown(markdown);
  md.classList.remove("hidden");
}

function setToolbar(s) {
  const on = !!s;
  ["btn-edit-tags", "btn-quiz", "btn-summarize"]
    .forEach(id => { $(`#${id}`).disabled = !on; });
}

function resetNoSource() {
  S.current = null;
  S.sel = null;
  S.noteId = null;
  $("#res-title").textContent = "Resource";
  $("#res-meta").textContent = "";
  $("#res-meta").classList.add("hidden");
  clearViewers();
  $("#res-md").innerHTML = "";
  $("#res-empty").classList.remove("hidden");
  $("#note-editor").value = "";
  $("#note-preview").innerHTML = '<p class="muted">Select a source in the directory to read and edit its notes.</p>';
  $("#note-editor").classList.add("hidden");
  $("#note-preview").classList.remove("hidden");
  $("#note-save-state").textContent = "";
  setToolbar(null);
  $("#btn-close-res").disabled = true;
  ANCHORS.clear();
}

// Deselecting is the same path as selecting, so a pending note is saved and the
// tree drops its highlight along with the viewer and the notes.
$("#btn-close-res").addEventListener("click", () => TREE.setSelection(null));

async function loadResource(s) {
  $("#res-title").textContent = s.title;
  $("#res-meta").textContent = s.source_type;
  $("#res-meta").classList.remove("hidden");
  const md = $("#res-md"), empty = $("#res-empty");
  clearViewers();
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
  } else if (isPdf(s)) {
    await PDFView.open(src, $("#res-pdf"));
  } else {
    $("#res-frame").src = src;
    $("#res-frame").classList.remove("hidden");
  }
}



async function loadNote(s) {
  try {
    const data = await api(`/api/note/${s.id}`);
    S.noteContent = data.content;
    S.noteId = data.note_id;
    S.noteVersion = data.version;
    $("#note-editor").value = S.noteContent;
    $("#note-preview").innerHTML = renderMarkdown(S.noteContent);
    $("#note-save-state").textContent = "";
    await ANCHORS.load();
  } catch (e) {
    toast("Failed to load note: " + e.message, "err");
  }
}

function onNoteTyped() {
  if (!S.noteId || S.streaming) return;
  S.noteDirty = true;
  $("#note-save-state").textContent = "unsaved…";
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(saveNote, 1200);
}

async function saveNote() {
  if (!S.noteId || !S.noteDirty) return;
  const content = $("#note-editor").value;
  S.noteDirty = false;
  try {
    if (S.current) {
      // The source route resolves the workspace's single page for us.
      const d = await api(`/api/note/${S.current.id}`, {
        method: "PUT",
        body: { content, base_version: S.noteVersion },
      });
      S.noteVersion = d.version;
    } else {
      const d = await api(`/api/notes/${S.noteId}`, {
        method: "PUT",
        body: { content, base_version: S.noteVersion },
      });
      S.noteVersion = d.note.version;
      showCompiled(d.note.name, d.note.content);   // the viewer *is* this note
    }
    $("#note-save-state").textContent = "saved";
    // A sentence that is gone from the saved text takes its link with it.
    await ANCHORS.pruneLost();
  } catch (e) {
    S.noteDirty = true;
    $("#note-save-state").textContent = "save error";
    toast("Note save failed: " + e.message, "err");
  }
}

function applyNoteMode() {
  const standalone = S.sel && S.sel.kind === "note";
  const edit = standalone || S.noteMode === "edit";
  $("#note-editor").classList.toggle("hidden", !edit);
  $("#note-preview").classList.toggle("hidden", edit);
  $("#note-hint").textContent = standalone
    ? "Live preview — the compiled note is shown on the left"
    : "Ctrl+D toggles preview";
  if (!edit) {
    $("#note-preview").innerHTML = renderMarkdown($("#note-editor").value);
  } else if (S.noteId) {
    $("#note-editor").focus();
  }
  ANCHORS.draw();
}


/* ---------- Anchors: note sentences grounded in the document ---------- */

/** What the viewer is showing, in the terms the anchoring module needs. */
function docTarget() {
  const s = S.current;
  if (!s) return null;
  if (isPdf(s)) return { kind: "pdf" };
  if (s.source_type === "md") {
    const root = $("#res-md");
    return root.classList.contains("hidden")
      ? null : { kind: "html", root, scroller: root };
  }
  const frame = $("#res-frame");
  if (frame.classList.contains("hidden")) return null;
  try {
    const d = frame.contentDocument;
    if (d && d.body) {
      return { kind: "html", root: d.body,
               scroller: d.scrollingElement || d.documentElement,
               win: frame.contentWindow };
    }
  } catch (_) { /* cross-origin: not linkable */ }
  return null;
}

const ANCHORS = makeAnchors({
  editor: () => $("#note-editor"),
  mirror: () => $("#note-mirror"),
  layer: () => $("#anchor-layer"),
  wrap: () => $(".note-wrap"),
  preview: () => $("#note-preview"),
  docPane: () => $("#panel-res"),
  docTarget,
  projectId: () => (KNDB.personal ? KNDB.personal.id : ""),
  noteId: () => S.noteId || "",
  // A standalone note has no document beside it, so nothing to link into.
  sourceId: () => (S.current ? S.current.id : ""),
  // The workspace has one member; without their colour every link would be
  // drawn in the "someone else" grey.
  colors: () => Object.fromEntries(
    ((KNDB.personal && KNDB.personal.members) || []).map(m => [m.name, m.color])),
  canLink: () => true,          // you own your workspace
  inPreview: () => $("#note-editor").classList.contains("hidden"),
  ensureEditMode: async () => {
    if (!$("#note-editor").classList.contains("hidden")) return;
    S.noteMode = "edit";
    applyNoteMode();
  },
  // One note per source here, so every link on this document is on this page.
  openNote: async nid => nid === S.noteId,
  onChange: ({ lost }) => {
    $("#note-anchor-state").textContent = lost
      ? lost + (lost === 1 ? " link no longer resolves" : " links no longer resolve")
      : "";
  },
});

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
      await refreshTree();
      await selectSource(data.id, "preview");
    } else {
      toast(`Imported “${data.title}” (${data.source_type})`, "ok");
      await refreshTree();
      await selectSource(data.id, "edit");
    }
  } catch (e) {
    toast("Import failed: " + e.message, "err", 9000);
  } finally {
    setBusy(btn, false, "Import");
  }
});

async function deleteSource(id) {
  const s = S.sources.find(x => x.id === id);
  if (!await confirmModal({
    title: "Delete source",
    body: `Delete “${s ? s.title : id}”, its notes, quiz and stats?`,
    hint: "This removes it from every project that holds it.",
    confirm: "Delete source",
    danger: true,
  })) return;
  try {
    await api("/api/source/" + id, { method: "DELETE" });
    if (S.current && S.current.id === id) TREE.sel = null;
    await refreshTree();
    toast("Source deleted", "ok");
  } catch (e) {
    toast("Delete failed: " + e.message, "err");
  }
}

async function deleteNotePage(nid) {
  const page = S.standalone.find(n => n.id === nid);
  if (!await confirmModal({
    title: "Delete note page",
    body: `Delete the note page “${page ? page.name : nid}”?`,
    hint: "Its text is gone for good.",
    confirm: "Delete page",
    danger: true,
  })) return;
  try {
    await api(`/api/notes/${nid}`, { method: "DELETE" });
    if (S.sel && S.sel.kind === "note" && S.sel.id === nid) TREE.sel = null;
    await refreshTree();
    toast("Page deleted", "ok");
  } catch (e) {
    toast(e.message, "err", 7000);
  }
}

/* ---------- Quiz hub ---------- */

/* One dialog for everything a source's quiz is: how it is going, and the three
 * things you can do about it. The generator, the player and the question
 * manager are opened from here rather than from the top bar. */

const BOXES = [
  { label: "New", max: 0, color: "var(--border-strong)" },
  { label: "Learning", max: 2, color: "var(--warn)" },
  { label: "Familiar", max: 4, color: "var(--accent)" },
  { label: "Mastered", max: Infinity, color: "var(--ok)" },
];

async function openQuizHub() {
  if (!S.current) return;
  S.quiz = null;
  S.quizStats = null;
  openModal("#modal-quiz");
  $("#quiz-hub-source").textContent = S.current.title;
  $("#quiz-hub-stats").innerHTML = "";
  $("#quiz-hub-progress").innerHTML = '<p class="muted">Loading…</p>';
  $("#btn-quiz-play").disabled = true;
  try {
    const [quiz, stats] = await Promise.all([
      api(`/api/quiz/${S.current.id}`),
      api(`/api/quiz/${S.current.id}/stats`),
    ]);
    S.quiz = quiz;
    S.quizStats = stats.stats || {};
  } catch (e) {
    $("#quiz-hub-progress").innerHTML =
      `<p class="muted">Could not load the quiz: ${escapeHtml(e.message)}</p>`;
    return;
  }
  renderQuizHub();
}

/** Fold the per-question spaced-repetition stats into the few numbers worth
 *  showing. A question with no `next_review` has never been answered, which
 *  counts as due. */
function quizSummary() {
  const questions = (S.quiz && S.quiz.questions) || [];
  const per = (S.quizStats && S.quizStats.questions) || {};
  const now = new Date().toISOString().slice(0, 19) + "Z";
  const buckets = BOXES.map(() => 0);
  let played = 0, correct = 0, due = 0, last = "";
  for (const q of questions) {
    const st = per[q.id] || {};
    played += st.times_played || 0;
    correct += st.times_successful || 0;
    if (!st.next_review || st.next_review <= now) due++;
    if ((st.last_played || "") > last) last = st.last_played || "";
    buckets[BOXES.findIndex(b => (st.box || 0) <= b.max)]++;
  }
  return { questions, played, correct, due, last, buckets };
}

function renderQuizHub() {
  const { questions, played, correct, due, last, buckets } = quizSummary();
  const total = questions.length;
  $("#quiz-hub-stats").innerHTML = [
    ["questions", total],
    ["due now", due],
    ["answers", played],
    ["avg. score", played ? Math.round(100 * correct / played) + "%" : "–"],
  ].map(([lbl, num]) =>                            // every value is our own number
    `<div class="stat"><div class="stat-num">${num}</div>`
    + `<div class="stat-lbl">${lbl}</div></div>`).join("");

  const prog = $("#quiz-hub-progress");
  if (!total) {
    prog.innerHTML = '<p class="muted">No questions yet — generate a quiz from '
      + "this source, or write questions yourself.</p>";
  } else {
    prog.innerHTML =
      '<div class="qh-bar">'
      + BOXES.map((b, i) => buckets[i]
          ? `<span style="flex:${buckets[i]};background:${b.color}"></span>` : "").join("")
      + '</div><div class="qh-legend">'
      + BOXES.map((b, i) => `<span><i class="dot" style="background:${b.color}"></i>`
          + `${b.label} <b>${buckets[i]}</b></span>`).join("")
      + "</div>"
      + `<p class="muted qh-when">${last ? "Last answered " + fmtDate(last)
                                         : "Never played yet"}</p>`;
  }
  $("#btn-quiz-play").disabled = !total;
  $("#quiz-hub-hint").textContent = total
    ? "Due questions come first; a wrong answer sends one back to the start."
    : "A quiz is built from your notes, the document, or both.";
}

$("#btn-quiz").addEventListener("click", openQuizHub);

$("#btn-quiz-manage").addEventListener("click", () => {
  closeModal("#modal-quiz");
  openQuizManager();
});

$("#btn-quiz-gen").addEventListener("click", async () => {
  if (((S.quiz && S.quiz.questions) || []).length && !await confirmModal({
    title: "Generate a new quiz",
    body: "This source already has a quiz. Generating replaces its questions.",
    hint: "The answers you have given to the current questions are lost with them.",
    confirm: "Replace the quiz",
    danger: true,
  })) return;
  closeModal("#modal-quiz");
  openModal("#modal-quiz-gen");
});

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
    await refreshTree();
    await openQuizHub();     // back to the hub, now showing the new quiz
  } catch (e) {
    toast("Quiz generation failed: " + e.message, "err", 10000);
  } finally {
    setBusy(btn, false, "Generate");
  }
});

$("#btn-summarize").addEventListener("click", () => openModal("#modal-summarize"));

$("#summarize-submit").addEventListener("click", async () => {
  if (!S.current) return;
  if (S.streaming) { toast("Already summarizing…", "warn"); return; }
  closeModal("#modal-summarize");

  await saveNote();   // the summary is appended server-side, under what is saved there
  // The summary is written into a specific note page, so send its id — the
  // server no longer has a notion of "the" note for a source.
  const body = {
    length: $("#su-length").value,
    language: $("#su-language").value.trim() || "English",
    note_id: S.noteId,
  };
  const editor = $("#note-editor");
  const saveState = $("#note-save-state");

  // Turn on notes preview and lock editing while the summary streams in.
  S.streaming = true;
  S.noteMode = "preview";
  applyNoteMode();
  editor.disabled = true;
  saveState.textContent = "summarizing…";

  // What the note already holds stays on screen: the summary lands under it.
  const before = editor.value.trim();
  let summary = "";
  const paint = () => {
    const preview = $("#note-preview");
    const body = summary ? `${before}\n\n${summary}`.trim() : before;
    preview.innerHTML = renderMarkdown(body) || '<p class="muted">Summarizing…</p>';
    preview.scrollTop = preview.scrollHeight;
  };
  paint();

  try {
    await streamSSE(`/api/summarize/${S.current.id}`, body, tok => {
      summary += tok;
      paint();
    });
    await loadNote(S.current);  // reload the finalized note (summary already written)
    toast("Summary appended to your note", "ok");
  } catch (e) {
    if (summary) paint();  // keep whatever streamed so far visible
    toast("Summarize failed: " + e.message, "err", 10000);
  } finally {
    S.streaming = false;
    editor.disabled = false;
    saveState.textContent = "";
  }
});

$("#btn-quiz-play").addEventListener("click", async () => {
  if (!S.current || !S.quiz || !S.quiz.questions.length) return;
  try {
    const order = await api(`/api/quiz/${S.current.id}/order`);
    closeModal("#modal-quiz");
    startQuizSession(S.quiz, order.questions);
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
    refreshTree();
  }
}


$("#note-editor").addEventListener("input", onNoteTyped);
$("#note-editor").addEventListener("blur", () => { if (S.noteDirty) saveNote(); });

document.addEventListener("keydown", e => {
  if (e.key === "Escape") hideCtx();
  if (e.ctrlKey && e.key.toLowerCase() === "d") {
    e.preventDefault();
    if (!S.current || S.streaming) return;
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

/* ---------- Tags ---------- */

/* The manager itself lives in common.js, so the project pages open the exact
 * same dialog. Tags are stored on the source row, which is why one added here
 * shows up in every project holding that source. */
function editTags() {
  if (!S.current) return;
  openTagsModal({
    source: S.current,
    onSaved: () => TREE.render(),   // the unfolded row shows the same tags
  });
}

$("#btn-edit-tags").addEventListener("click", editTags);

/* ---------- Quiz management ---------- */

async function openQuizManager() {
  if (!S.current) return;
  S.qm = null;
  S.qmEditId = null;
  openModal("#modal-quiz-manage");
  try {
    S.qm = await api(`/api/quiz/${S.current.id}`);
  } catch (e) {
    $("#qm-list").innerHTML = `<p class="muted">Could not load quiz: ${escapeHtml(e.message)}</p>`;
    return;
  }
  renderQuizList();
  resetQuizForm();
}

function renderQuizList() {
  const list = $("#qm-list");
  const questions = (S.qm && S.qm.questions) || [];
  if (!questions.length) {
    list.innerHTML =
      '<p class="muted">No questions yet — add one below, or generate a quiz from the Quiz dialog.</p>';
    return;
  }
  list.innerHTML = "";
  questions.forEach((q, i) => {
    const el = document.createElement("div");
    el.className = "qm-item";
    el.dataset.qid = q.id;
    const opts = (q.answers || []).map((a, idx) =>
      `<span class="qm-opt${idx === q.answer_index ? " ok" : ""}">${escapeHtml(a)}${idx === q.answer_index ? " ✓" : ""}</span>`
    ).join("");
    el.innerHTML = `
      <div class="qm-q"><strong>${i + 1}.</strong> ${escapeHtml(q.question)}</div>
      <div class="qm-opts">${opts}</div>
      <div class="qm-actions">
        <button class="btn small" data-act="edit">Edit</button>
        <button class="btn small danger" data-act="del">Delete</button>
      </div>`;
    list.appendChild(el);
  });
}

function resetQuizForm() {
  S.qmEditId = null;
  $("#qm-question").value = "";
  ["qm-a1", "qm-a2", "qm-a3", "qm-a4"].forEach(id => { $(`#${id}`).value = ""; });
  $("#qm-correct").value = "1";
  $("#qm-add-btn").textContent = "Add question";
  $("#qm-cancel").classList.add("hidden");
}

function loadQuizForm(qid) {
  const q = ((S.qm && S.qm.questions) || []).find(x => x.id === qid);
  if (!q) return;
  S.qmEditId = qid;
  $("#qm-question").value = q.question;
  (q.answers || []).forEach((a, i) => { if (i < 4) $(`#qm-a${i + 1}`).value = a; });
  $("#qm-correct").value = String((q.answer_index ?? 0) + 1);
  $("#qm-add-btn").textContent = "Save changes";
  $("#qm-cancel").classList.remove("hidden");
  $("#qm-question").focus();
}

async function refreshQuizManager() {
  try {
    S.qm = await api(`/api/quiz/${S.current.id}`);
  } catch (_) {}
  renderQuizList();
  resetQuizForm();
  refreshTree();
}

async function saveQuizForm() {
  if (!S.current) return;
  const question = $("#qm-question").value.trim();
  const answers = [1, 2, 3, 4].map(i => $(`#qm-a${i}`).value.trim());
  const ansCount = answers.filter(Boolean).length;
  const answer_index = parseInt($("#qm-correct").value, 10) - 1;
  if (!question) { toast("Enter a question", "warn"); return; }
  if (ansCount < 2) { toast("Enter at least two answers", "warn"); return; }
  if (answer_index >= ansCount) { toast("Correct answer must be one of the filled answers", "warn"); return; }
  const btn = $("#qm-add-btn");
  const editing = !!S.qmEditId;
  setBusy(btn, true, "Saving…");
  try {
    if (editing) {
      await api(`/api/quiz/${S.current.id}/questions/${encodeURIComponent(S.qmEditId)}`, {
        method: "PUT", body: { question, answers, answer_index },
      });
    } else {
      await api(`/api/quiz/${S.current.id}/questions`, {
        method: "POST", body: { question, answers, answer_index },
      });
    }
    await refreshQuizManager();
    toast(editing ? "Question updated" : "Question added", "ok");
  } catch (e) {
    toast("Could not save question: " + e.message, "err");
  } finally {
    setBusy(btn, false, editing ? "Save changes" : "Add question");
  }
}

$("#qm-add-btn").addEventListener("click", saveQuizForm);
$("#qm-cancel").addEventListener("click", resetQuizForm);
$("#qm-question").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); $("#qm-add-btn").click(); }
});
$("#qm-list").addEventListener("click", async e => {
  const act = e.target.closest("[data-act]");
  if (!act || !S.current) return;
  const item = e.target.closest(".qm-item");
  if (!item) return;
  const qid = item.dataset.qid;
  if (act.dataset.act === "del") {
    if (!await confirmModal({
      title: "Delete question",
      body: "Delete this question and its stats?",
      hint: "The answers you have already given to it are dropped too.",
      confirm: "Delete question",
      danger: true,
    })) return;
    try {
      await api(`/api/quiz/${S.current.id}/questions/${encodeURIComponent(qid)}`, { method: "DELETE" });
      await refreshQuizManager();
      toast("Question deleted", "ok");
    } catch (err) {
      toast("Delete failed: " + err.message, "err");
    }
  } else if (act.dataset.act === "edit") {
    loadQuizForm(qid);
  }
});

/* ---------- Source context menu (right click) ---------- */

function hideCtx() { $("#ctx-menu").classList.add("hidden"); }

function showCtxMenu(x, y, s) {
  const menu = $("#ctx-menu");
  menu.innerHTML = "";
  const items = [
    { label: "Quiz…", danger: false },
    { label: "Tags…", danger: false },
    { label: "Delete source", danger: true },
  ];
  for (const it of items) {
    const b = document.createElement("button");
    b.className = "ctx-item" + (it.danger ? " danger" : "");
    b.textContent = it.label;
    b.addEventListener("click", async () => {
      hideCtx();
      if (!S.current || S.current.id !== s.id) await selectSource(s.id, "preview");
      if (it.label === "Quiz…") openQuizHub();
      else if (it.label === "Tags…") editTags();
      else deleteSource(s.id);
    });
    menu.appendChild(b);
  }
  menu.classList.remove("hidden");
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  if (x + mw > window.innerWidth - 8) x = Math.max(8, window.innerWidth - mw - 8);
  if (y + mh > window.innerHeight - 8) y = Math.max(8, window.innerHeight - mh - 8);
  menu.style.left = x + "px";
  menu.style.top = y + "px";
}

document.addEventListener("contextmenu", e => {
  const item = e.target.closest(".tree-row.source");
  if (!item) { hideCtx(); return; }
  e.preventDefault();
  const s = S.sources.find(x => x.id === item.dataset.id);
  if (!s) return;
  if (!S.current || S.current.id !== s.id) selectSource(s.id, "preview");  // preload target
  showCtxMenu(e.clientX, e.clientY, s);
});

document.addEventListener("click", e => {
  if (!e.target.closest("#ctx-menu")) hideCtx();
});

let _searchTimer;
$("#search").addEventListener("input", () => {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(applySearch, 300);
});

/** This box also understands "/project", which only the server can resolve, so
 *  sources are filtered by the ids it hands back; folders and standalone notes
 *  fall back to the name match both pages share. */
async function applySearch() {
  const q = $("#search").value.trim();
  if (!q) return TREE.applyFilter(null);
  const local = TREE.queryPredicate(q);
  let ids = null;
  try {
    ids = new Set((await api("/api/sources?q=" + encodeURIComponent(q))).sources
      .map(s => s.id));
  } catch (e) { /* offline or a bad query: filter in the browser instead */ }
  TREE.applyFilter((kind, item) =>
    (kind === "source" && ids) ? ids.has(item.id) : local(kind, item));
}

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

function setDirFolded(folded) {
  $("#panel-dir").classList.toggle("folded", folded);
  const btn = $("#btn-fold-dir");
  btn.setAttribute("aria-expanded", String(!folded));
  btn.title = folded ? "Show the library" : "Hide the library";
  localStorage.setItem("kndb.fold.dir", folded ? "1" : "0");
}

$("#btn-fold-dir").addEventListener("click", () => {
  setDirFolded(!$("#panel-dir").classList.contains("folded"));
});

async function init() {
  await initIdentity();
  S.pid = (KNDB.personal && KNDB.personal.id) || "";
  if (!S.pid) {
    toast("Could not reach the server — reload to try again", "err", 9000);
    return;
  }
  TREE.setProject(S.pid);
  setupSplitter($('.splitter[data-split="dir"]'), $("#panel-dir"), "kndb.w.dir");
  setupSplitter($('.splitter[data-split="res"]'), $("#panel-res"), "kndb.w.res");
  setDirFolded(localStorage.getItem("kndb.fold.dir") === "1");
  await refreshTree();
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
