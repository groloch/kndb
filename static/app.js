"use strict";

/* Personal workspace. The library page itself — tree, viewers, note editor,
 * summarize — is library.js, shared with the project pages.
 * What is left here is what only the workspace does: importing and the quiz.
 *
 * $, api, toast, renderMarkdown, streamSSE, modals and the identity header live
 * in common.js, which must load first.
 */

const S = {
  pid: "",            // the workspace is a project like any other
  noteVersion: 1,
  quiz: null,          // quiz hub: the loaded quiz
  quizStats: null,     // quiz hub: its spaced-repetition stats
  qm: null,
  qmEditId: null,
};

function setBusy(btn, busy, label) {
  /* Disables a button under a temporary label, restores its own on release
  */
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

  loadNotes: async s => {
    /* The single note page of a source, into the editor.
    * Remembers its version for the next save, null when it will not load
    */
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
    /* A standalone note page by id, null when it will not load
    */
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
    /* Saves the editor under the version it was loaded at.
    * A selected source saves through its own route, a standalone page by id
    */
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
    /* Tags and quiz act on a source, on nothing else — Summarize is the
    * library's own, enabled with the note it writes into
    */
    const src = !!ref && ref.kind === "source";
    ["btn-edit-tags", "btn-quiz"].forEach(id => { $(`#${id}`).disabled = !src; });
  },

  searchPredicate: async (q, local) => {
    /* Sources are filtered by the ids the server hands back, as it alone
    * resolves a query like "/project".
    * Folders and standalone notes fall back to the name match every library
    * shares
    */
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

async function selectSource(id, noteMode = "preview") {
  /* Opens a source, switching the note to edit mode when asked for it.
  * Any other mode leaves the one already in use, so only a fresh import opens
  * ready to write in
  */
  await LIB.selectSource(id);
  if (noteMode === "edit" && LIB.state.noteMode !== "edit") {
    LIB.state.noteMode = "edit";
    LIB.applyNoteMode();
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
  /* Deletes a source after confirmation, out of every project holding it
  */
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
  /* Deletes a note page after confirmation, its text with it
  */
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

/* One dialog for everything a source's quiz is: how it is going, and the three
 * things you can do about it. The generator, the player and the question
 * manager are opened from here rather than from the top bar.
 */

const BOXES = [
  { label: "New", max: 0, color: "var(--border-strong)" },
  { label: "Learning", max: 2, color: "var(--warn)" },
  { label: "Familiar", max: 4, color: "var(--accent)" },
  { label: "Mastered", max: Infinity, color: "var(--ok)" },
];

async function openQuizHub() {
  /* Opens the quiz dialog on the selected source, quiz and stats loaded
  * together
  */
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

function quizSummary() {
  /* Folds the per-question spaced-repetition stats into the few numbers worth
  * showing.
  * A question with no next_review has never been answered, and counts as due
  */
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
  /* Paints the counters, the box bar and the hint.
  * Play stays disabled while the source has no question
  */
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
  /* Runs one play-through, in the order the server gave.
  * A missed question comes back once, at the end of the queue
  */
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
    /* One question, its answers shuffled so the right one moves about
    */
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
    /* Marks the answer, requeues a missed question, and reports the result to
    * the server
    */
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
    /* Final score, and the tree picks up the new stats
    */
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

function editTags() {
  /* Opens the tag manager, the very dialog common.js gives the project pages.
  * Tags are stored on the source row, so one added here shows up in every
  * project holding that source
  */
  if (!LIB.source) return;
  openTagsModal({
    source: LIB.source,
    onSaved: () => LIB.TREE.render(),   // the unfolded row shows the same tags
  });
}

$("#btn-edit-tags").addEventListener("click", editTags);

async function openQuizManager() {
  /* Opens the question manager on the selected source's quiz
  */
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
  /* Lists the questions, the correct answer marked
  */
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
  /* Empties the form and leaves edit mode
  */
  S.qmEditId = null;
  $("#qm-question").value = "";
  ["qm-a1", "qm-a2", "qm-a3", "qm-a4"].forEach(id => { $(`#${id}`).value = ""; });
  $("#qm-correct").value = "1";
  $("#qm-add-btn").textContent = "Add question";
  $("#qm-cancel").classList.add("hidden");
}

function loadQuizForm(qid) {
  /* Fills the form with a question, for the next save to go over it
  */
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
  /* Reloads the quiz after a change, and the tree with it
  */
  try {
    S.qm = await api(`/api/quiz/${LIB.source.id}`);
  } catch (_) {}
  renderQuizList();
  resetQuizForm();
  refreshTree();
}

async function saveQuizForm() {
  /* Adds the form's question, or saves it over the one being edited.
  * Two answers at least, and the right one must be among those filled in
  */
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

function hideCtx() { $("#ctx-menu").classList.add("hidden"); }

function showCtxMenu(x, y, s) {
  /* Right-click menu of a source row, clamped inside the window
  */
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
  /* Identity first: the workspace's project id comes with it, and nothing
  * works without it.
  * A #src= fragment opens that source once the tree is up
  */
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
}

init();
