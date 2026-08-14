"use strict";

/* Personal workspace. The library page itself — tree, viewers, note editor —
 * is library.js, shared with the project pages; what is left here is what only
 * the workspace does: importing, summarizing, and the quiz.
 *
 * $, api, toast, renderMarkdown, modals and the identity header live in
 * common.js, which must load first. */

const S = {
  pid: "",            // the workspace is a project like any other
  noteVersion: 1,
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


/* ---------- The library page ---------- */

/* One note per source here, and it is yours: no pages, no roles, no blame. */

const LIB = makeLibrary({
  projectId: () => S.pid,
  keys: "kndb.",
  noteMode: "preview",

  tree: {
    readOnly: () => false,
    role: () => KNDB.ownerRole,   // you own your own workspace
    showQuestions: () => true,
    removeTitle: "Delete source",
    empty: '<div class="empty">No sources yet.<br>Add one with <b>+ Import</b>.</div>',
    onRemoveSource: deleteSource,
    onDeleteNote: deleteNotePage,
  },

  anchors: {
    // The workspace has one member; without their colour every link would be
    // drawn in the "someone else" grey.
    colors: () => Object.fromEntries(
      ((KNDB.personal && KNDB.personal.members) || []).map(m => [m.name, m.color])),
  },

  // Nothing may move while a summary is streaming into the note it would leave.
  canSelect: () => {
    if (!S.streaming) return true;
    toast("Wait for the summary to finish first", "warn");
    return false;
  },

  loadNotes: async s => {
    try {
      const d = await api(`/api/note/${s.id}`);
      S.noteVersion = d.version;
      $("#note-editor").value = d.content;
      return { id: d.note_id, name: s.title, content: d.content };
    } catch (e) {
      toast("Failed to load note: " + e.message, "err");
      return null;
    }
  },

  loadNote: async nid => {
    try {
      const d = await api(`/api/notes/${nid}`);
      S.noteVersion = d.note.version;
      $("#note-editor").value = d.note.content;
      return d.note;
    } catch (e) {
      toast("Failed to load note: " + e.message, "err");
      return null;
    }
  },

  persist: async (content, nid) => {
    if (LIB.source) {
      // The source route resolves the workspace's single page for us.
      const d = await api(`/api/note/${LIB.source.id}`, {
        method: "PUT",
        body: { content, base_version: S.noteVersion },
      });
      S.noteVersion = d.version;
    } else {
      const d = await api(`/api/notes/${nid}`, {
        method: "PUT",
        body: { content, base_version: S.noteVersion },
      });
      S.noteVersion = d.note.version;
      // The viewer already follows the editor as it is typed; redrawing it from
      // the response would only throw the reader back to the top of the note.
    }
  },

  onSelect: ref => {
    const src = !!ref && ref.kind === "source";
    ["btn-edit-tags", "btn-quiz", "btn-summarize"]
      .forEach(id => { $(`#${id}`).disabled = !src; });
  },

  /* This box also understands "/project", which only the server can resolve, so
   * sources are filtered by the ids it hands back; folders and standalone notes
   * fall back to the name match every library shares. */
  searchPredicate: async (q, local) => {
    let ids = null;
    try {
      ids = new Set((await api("/api/sources?q=" + encodeURIComponent(q))).sources
        .map(s => s.id));
    } catch (e) { /* offline or a bad query: filter in the browser instead */ }
    return (kind, item) =>
      (kind === "source" && ids) ? ids.has(item.id) : local(kind, item);
  },
});

const refreshTree = LIB.refreshTree;

/** Freshly imported sources open ready to write in; everything else opens on
 *  what is already there. */
async function selectSource(id, noteMode = "preview") {
  await LIB.selectSource(id);
  if (noteMode === "edit" && LIB.state.noteMode !== "edit") {
    LIB.state.noteMode = "edit";
    LIB.applyNoteMode();
  }
}


/* ---------- Import ---------- */

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
  const s = LIB.state.sources.find(x => x.id === id);
  if (!await confirmModal({
    title: "Delete source",
    body: `Delete “${s ? s.title : id}”, its notes, quiz and stats?`,
    hint: "This removes it from every project that holds it.",
    confirm: "Delete source",
    danger: true,
  })) return;
  try {
    await api("/api/source/" + id, { method: "DELETE" });
    if (LIB.source && LIB.source.id === id) LIB.TREE.sel = null;
    await refreshTree();
    toast("Source deleted", "ok");
  } catch (e) {
    toast("Delete failed: " + e.message, "err");
  }
}

async function deleteNotePage(nid) {
  const page = LIB.state.notes.find(n => n.id === nid);
  if (!await confirmModal({
    title: "Delete note page",
    body: `Delete the note page “${page ? page.name : nid}”?`,
    hint: "Its text is gone for good.",
    confirm: "Delete page",
    danger: true,
  })) return;
  try {
    await api(`/api/notes/${nid}`, { method: "DELETE" });
    if (LIB.sel && LIB.sel.kind === "note" && LIB.sel.id === nid) LIB.TREE.sel = null;
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
  if (!LIB.source) return;
  S.quiz = null;
  S.quizStats = null;
  openModal("#modal-quiz");
  $("#quiz-hub-source").textContent = LIB.source.title;
  $("#quiz-hub-stats").innerHTML = "";
  $("#quiz-hub-progress").innerHTML = '<p class="muted">Loading…</p>';
  $("#btn-quiz-play").disabled = true;
  try {
    const [quiz, stats] = await Promise.all([
      api(`/api/quiz/${LIB.source.id}`),
      api(`/api/quiz/${LIB.source.id}/stats`),
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
  if (!LIB.source) return;
  const btn = $("#quiz-gen-submit");
  const body = {
    scope: $('input[name="qz-scope"]:checked').value,
    num_questions: parseInt($("#qz-num").value, 10),
    difficulty: $("#qz-difficulty").value,
    language: $("#qz-language").value.trim() || "English",
  };
  setBusy(btn, true, "Generating…");
  try {
    await LIB.flushNote();
    const done = await streamSSE(`/api/quiz/${LIB.source.id}/generate`, body);
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
  const source = LIB.source;
  if (!source) return;
  if (S.streaming) { toast("Already summarizing…", "warn"); return; }
  closeModal("#modal-summarize");

  await LIB.flushNote();   // the summary is appended server-side, under what is saved there
  // The summary is written into a specific note page, so send its id — the
  // server no longer has a notion of "the" note for a source.
  const body = {
    length: $("#su-length").value,
    language: $("#su-language").value.trim() || "English",
    note_id: LIB.noteId,
  };
  const editor = $("#note-editor");

  // Turn on notes preview and lock editing while the summary streams in.
  S.streaming = true;
  LIB.state.noteMode = "preview";
  LIB.applyNoteMode();
  editor.disabled = true;
  LIB.setSaveState("summarizing…");

  // What the note already holds stays on screen: the summary lands under it.
  const before = editor.value.trim();
  let summary = "";
  const paint = () => {
    const preview = $("#note-preview");
    const text = summary ? `${before}\n\n${summary}`.trim() : before;
    preview.innerHTML = renderMarkdown(text) || '<p class="muted">Summarizing…</p>';
    preview.scrollTop = preview.scrollHeight;
  };
  paint();

  try {
    await streamSSE(`/api/summarize/${source.id}`, body, tok => {
      summary += tok;
      paint();
    });
    S.streaming = false;
    // Pick up the finalized note (the summary is already written into it).
    await LIB.selectSource(source.id);
    toast("Summary appended to your note", "ok");
  } catch (e) {
    if (summary) paint();  // keep whatever streamed so far visible
    toast("Summarize failed: " + e.message, "err", 10000);
  } finally {
    S.streaming = false;
    editor.disabled = false;
    LIB.setSaveState("");
  }
});

$("#btn-quiz-play").addEventListener("click", async () => {
  if (!LIB.source || !S.quiz || !S.quiz.questions.length) return;
  try {
    const order = await api(`/api/quiz/${LIB.source.id}/order`);
    closeModal("#modal-quiz");
    startQuizSession(S.quiz, order.questions);
  } catch (e) {
    toast("Could not start quiz: " + e.message, "err");
  }
});

function startQuizSession(quiz, questions) {
  const body = $("#quiz-body");
  const queue = [...questions];
  const sid = LIB.source.id;
  let total = 0, correct = 0;
  const attempted = new Set();
  const requeued = new Set();

  openModal("#modal-quiz-play");
  renderIntro();

  function renderIntro() {
    body.innerHTML = `
      <div class="quiz-summary">
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
    api(`/api/quiz/${sid}/answer`, {
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


document.addEventListener("keydown", e => {
  if (e.key === "Escape") hideCtx();
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
  if (!LIB.source) return;
  openTagsModal({
    source: LIB.source,
    onSaved: () => LIB.TREE.render(),   // the unfolded row shows the same tags
  });
}

$("#btn-edit-tags").addEventListener("click", editTags);

/* ---------- Quiz management ---------- */

async function openQuizManager() {
  if (!LIB.source) return;
  S.qm = null;
  S.qmEditId = null;
  openModal("#modal-quiz-manage");
  try {
    S.qm = await api(`/api/quiz/${LIB.source.id}`);
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
    S.qm = await api(`/api/quiz/${LIB.source.id}`);
  } catch (_) {}
  renderQuizList();
  resetQuizForm();
  refreshTree();
}

async function saveQuizForm() {
  if (!LIB.source) return;
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
      await api(`/api/quiz/${LIB.source.id}/questions/${encodeURIComponent(S.qmEditId)}`, {
        method: "PUT", body: { question, answers, answer_index },
      });
    } else {
      await api(`/api/quiz/${LIB.source.id}/questions`, {
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
  if (!act || !LIB.source) return;
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
      await api(`/api/quiz/${LIB.source.id}/questions/${encodeURIComponent(qid)}`, { method: "DELETE" });
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
      if (!LIB.source || LIB.source.id !== s.id) await selectSource(s.id, "preview");
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
  const s = LIB.state.sources.find(x => x.id === item.dataset.id);
  if (!s) return;
  if (!LIB.source || LIB.source.id !== s.id) selectSource(s.id, "preview");  // preload target
  showCtxMenu(e.clientX, e.clientY, s);
});

document.addEventListener("click", e => {
  if (!e.target.closest("#ctx-menu")) hideCtx();
});


async function init() {
  await initIdentity();
  S.pid = (KNDB.personal && KNDB.personal.id) || "";
  if (!S.pid) {
    toast("Could not reach the server — reload to try again", "err", 9000);
    return;
  }
  await LIB.start();
  const m = location.hash.match(/^#src=([\w]+)/);
  if (m) {
    const srcId = decodeURIComponent(m[1]);
    if (LIB.state.sources.some(s => s.id === srcId)) await selectSource(srcId, "preview");
  }
  api("/api/llm/status").then(d => {
    const el = $("#llm-status");
    el.textContent = d.loaded ? "LLM ready" : (d.last_error ? "LLM: offline" : "LLM: connecting…");
    el.title = (d.last_error || d.model || "");
  }).catch(() => {});
}

init();
