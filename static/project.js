"use strict";

/* The project workspace page.
 *
 * Its Library tab is the personal workspace page, drawn by library.js. What
 * lives here is what a project adds to it — several note pages per source,
 * blame, transfers — plus the tabs around it. Shared helpers ($, api, toast,
 * renderMarkdown, blame…) come from common.js
 */

// project id comes from the URL /projects/<pid>
const PID = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");

const S = {
  project: null,
  caps: {},
  colors: {},
  role: "",
  workspace: [],     // the user's own library, for the add-source modal
  rows: [],          // sources in this project
  folders: [],
  standalone: [],    // standalone note pages
  readOnly: false,
  pages: [],         // note pages for the selected source
  note: null,        // the loaded page {id, content, blame, version…}
};

function switchTab(tab) {
  /* Shows one subtab, hides the others.
  * The blame gutter can only be measured while its tab is visible
  */
  $$(".subtab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  ["overview", "library", "agent", "settings"].forEach(t => {
    $("#tab-" + t).classList.toggle("hidden", t !== tab);
  });
  if (tab === "library") scheduleGutter();
}

$$(".subtab").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));

document.addEventListener("click", e => {
  const srcRow = e.target.closest(".ov-src-row");
  if (srcRow) {
    switchTab("library");
    LIB.selectSource(srcRow.dataset.id);
  }
});

/* Projects open a note in edit mode: the blame gutter is line-exact and only
 * drawn over the textarea, and knowing who wrote what is the point of reading
 * someone else's page. Ctrl+D still flips to the preview
 */

const LIB = makeLibrary({
  projectId: () => PID,
  root: () => $("#tab-library"),
  keys: "kndb.prj.",
  noteMode: "edit",

  tree: {
    readOnly: () => S.readOnly,
    role: () => S.role,
    showQuestions: () => !!S.caps.allow_quiz,
    removeTitle: "Remove from project",
    empty: '<div class="empty">Nothing here yet.<br>'
         + "Use <b>+ Import</b> to bring in a source.</div>",
    onRemoveSource: removeSource,
    onDeleteNote: deletePage,
  },

  anchors: {
    colors: () => S.colors,
    canLink: () => !S.readOnly && S.canEdit !== false,
    // A link can point into a page other than the one on screen, so following
    // it has to be able to open that page first.
    openNote: async nid => {
      await LIB.flushNote();
      const ok = !!await loadNote(nid);
      renderNoteTabs();
      return ok;
    },
  },

  onTree: t => {
    S.rows = t.sources;
    S.folders = t.folders;
    S.standalone = t.notes;
    S.colors = t.colors || {};
  },

  loadNotes: s => loadPages(s.id),
  loadNote,

  // The summary is written into the open page as the caller, so a read-only
  // project — or someone else's page they may not touch — has nothing to offer.
  canSummarize: () => !S.readOnly && S.canEdit !== false,

  persist: async (content, nid) => {
    const d = await api("/api/notes/" + nid, {
      method: "PUT",
      body: { content, base_version: S.note.version },
    });
    if (!S.note || S.note.id !== nid) return;
    S.note = d.note;
    renderBlameLegend(true);
    renderGutter();
    // Writing into a page can change who holds a line in it, and that is what
    // decides whether the tab offers a delete button.
    const page = S.pages.find(x => x.id === nid);
    if (page && String(page.authors) !== String(d.note.authors)) {
      page.authors = d.note.authors;
      renderNoteTabs();
    }
  },

  saveState: e => (e.status === 403 ? "not allowed" : "save error"),

  onSaveError: async (e, nid) => {
    if (e.status === 403) {
      toast(e.message, "err", 7000);
      // Put back what the server still holds so the editor cannot drift out of
      // sync with a save that never landed.
      await LIB.adoptNote(await loadNote(nid));
    } else if (e.status === 409) {
      toast(e.message + " — reloading the page", "warn", 6000);
      await LIB.adoptNote(await loadNote(nid));
    } else {
      toast("Note save failed: " + e.message, "err");
    }
  },

  onSelect: ref => {
    $("#btn-edit-tags").disabled = !ref || ref.kind !== "source";
    if (!ref || ref.kind !== "source") {
      S.pages = [];
      $("#note-tabs").classList.add("hidden");
    }
    if (!ref || ref.kind === "folder") {
      S.note = null;
      $("#blame-gutter").classList.add("hidden");
      $("#blame-legend").classList.add("hidden");
    }
  },

  // The gutter lines up with the textarea, so it is redrawn whenever the
  // textarea is shown, hidden or resized.
  onNoteMode: () => scheduleGutter(),
});

async function loadProject() {
  /* Boots the page: project, role and capabilities, then the library.
  * Bounces back to /projects when the project cannot be opened
  */
  await initIdentity();
  let p;
  try {
    p = (await api("/api/projects/" + PID)).project;
  } catch (e) {
    toast("Could not open this project: " + e.message, "err", 6000);
    setTimeout(() => { location.href = "/projects"; }, 900);
    return;
  }
  S.project = p;
  S.caps = p.capabilities || {};
  // no membership means no role at all — every grant check below then fails
  S.role = (p.members || []).find(m => m.name === KNDB.user)?.role || "";
  document.title = p.name + " — KNDB";
  $("#sub-name").textContent = p.name;
  $("#sub-name").title = p.description || "";
  $("#prj-badge").classList.toggle("hidden", !p.completed);

  S.readOnly = !KNDB.may(S.role, "write") || p.completed;
  $$("#panel-dir .head-actions .btn").forEach(b => { b.disabled = S.readOnly; });

  renderSettings();
  await LIB.start();
  renderOverview();
}

const refreshTree = () => LIB.refreshTree();

$("#btn-edit-tags").addEventListener("click", () => {
  const sel = LIB.sel;
  const src = sel && sel.kind === "source" ? S.rows.find(s => s.id === sel.id) : null;
  if (!src) return;
  openTagsModal({
    source: src,
    readOnly: S.readOnly,
    // The unfolded tree rows show the same tags, so they redraw with the modal.
    onSaved: () => LIB.TREE.render(),
  });
});

async function loadPages(sid) {
  /* Pages written on one source, as tabs above the editor.
  * Returns the page the library should open, null when there is none
  */
  const d = await api("/api/projects/" + PID + "/sources/" + sid + "/notes");
  S.pages = d.notes;
  S.caps = d.capabilities || S.caps;
  S.colors = d.colors || S.colors;
  renderNoteTabs();
  if (!S.pages.length) {
    S.note = null;
    $("#note-preview").innerHTML =
      '<p class="muted">No note pages yet — create one with <b>+</b> above.</p>';
    $("#blame-gutter").classList.add("hidden");
    return null;
  }
  const note = await loadNote(S.pages[0].id);
  renderNoteTabs();            // now that there is a current page to mark active
  return note;
}

function renderNoteTabs() {
  /* Redraws the page tabs above the editor.
  * Hidden when something other than a source is selected, but not when
  * nothing is selected at all
  */
  const bar = $("#note-tabs");
  const sel = LIB.sel;
  if (sel && sel.kind !== "source") { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  bar.innerHTML = "";
  for (const p of S.pages) {
    const t = document.createElement("div");
    t.className = "note-tab" + (S.note && p.id === S.note.id ? " active" : "");
    t.dataset.id = p.id;
    t.title = "by " + p.created_by + (canRename(p) ? " — click the name to rename" : "");
    t.innerHTML =
      '<span class="dot" style="background:' + blameColor(S.colors, p.created_by) + '"></span>'
      + '<span class="note-tab-name">' + escapeHtml(p.name || "Notes") + "</span>"
      + (noteDeletable(p, S.role) && !S.readOnly
        ? '<button class="del" data-delpage="' + p.id + '" title="Delete this page">×</button>'
        : "");
    bar.appendChild(t);
  }
  if (S.caps.multi_notes) {
    const add = document.createElement("div");
    add.className = "note-tab add";
    add.id = "note-tab-add";
    add.textContent = "+";
    add.title = "New note page for this source";
    bar.appendChild(add);
  }
}

$("#note-tabs").addEventListener("click", async e => {
  // The tabs are the one way to change page without going through the tree, so
  // they answer to the summary stream themselves.
  if (LIB.busy()) return;
  const del = e.target.closest("[data-delpage]");
  if (del) { e.stopPropagation(); return deletePage(del.dataset.delpage); }
  if (e.target.closest("#note-tab-add")) return newPage();
  const tab = e.target.closest(".note-tab");
  if (!tab || !tab.dataset.id) return;
  // Clicking the name of the page you are already on renames it in place.
  if (tab.classList.contains("active") && e.target.closest(".note-tab-name")) {
    return startRename(tab);
  }
  await LIB.flushNote();
  await LIB.adoptNote(await loadNote(tab.dataset.id));
  renderNoteTabs();
});

function canRename(page) {
  return !S.readOnly && (page.created_by === KNDB.user
    || KNDB.may(S.role, "edit_others"));
}

function startRename(tab) {
  /* Renames a page in place, from its tab.
  * Enter and blur commit, Escape and a failed save put the old name back
  */
  const page = S.pages.find(p => p.id === tab.dataset.id);
  const el = $(".note-tab-name", tab);
  if (!page || !el || el.isContentEditable || !canRename(page)) return;
  const before = page.name || "";
  let closed = false;

  const stop = async commit => {
    if (closed) return;
    closed = true;
    el.contentEditable = "false";
    tab.classList.remove("renaming");
    const name = el.textContent.trim();
    if (!commit || !name || name === before) { el.textContent = before; return; }
    try {
      await api("/api/notes/" + page.id, { method: "PATCH", body: { name } });
      page.name = name;
      if (S.note && S.note.id === page.id) S.note.name = name;
      renderNoteTabs();
    } catch (err) {
      el.textContent = before;
      toast(err.message, "err", 6000);
    }
  };

  el.addEventListener("keydown", e => {
    e.stopPropagation();               // Ctrl+D toggles preview page-wide
    if (e.key === "Enter") { e.preventDefault(); stop(true); }
    if (e.key === "Escape") { e.preventDefault(); stop(false); }
  });
  el.addEventListener("blur", () => stop(true));

  el.contentEditable = "true";
  tab.classList.add("renaming");
  el.focus();
  document.getSelection().selectAllChildren(el);
}

async function deletePage(nid) {
  /* Deletes a note page, of a source or standalone, after confirmation.
  * Leaves the library on a selection that still exists
  */
  const page = S.pages.find(p => p.id === nid)
    || S.standalone.find(n => n.id === nid);
  if (!await confirmModal({
    title: "Delete note page",
    body: 'Delete the note page "' + (page ? page.name || "Notes" : nid) + '"?',
    hint: "Its text is gone for good.",
    confirm: "Delete page",
    danger: true,
  })) return;
  try {
    await api("/api/notes/" + nid, { method: "DELETE" });
    toast("Page deleted", "ok");
    const sel = LIB.sel;
    if (sel && sel.kind === "note" && sel.id === nid) {
      LIB.TREE.sel = null;
      await LIB.resetSelection();
      await refreshTree();
    } else if (sel && sel.kind === "source") {
      await LIB.adoptNote(await loadPages(sel.id));
    } else {
      await refreshTree();
    }
  } catch (e) { toast(e.message, "err", 7000); }
}

async function newPage() {
  /* Creates a note page on the selected source, and opens it.
  * Does nothing unless a source is selected
  */
  const sel = LIB.sel;
  if (!sel || sel.kind !== "source") return;
  const src = S.rows.find(s => s.id === sel.id);
  const name = await askModal({
    title: "New note page",
    label: "Page name",
    value: "Notes",
    submit: "Create page",
    hint: "Pages on " + (src ? '"' + src.title + '"' : "this source")
        + " sit side by side, and everyone in the project sees them all.",
  });
  if (!name) return;
  try {
    const d = await api("/api/projects/" + PID + "/notes",
      { method: "POST", body: { source_id: sel.id, name } });
    await loadPages(sel.id);
    await LIB.adoptNote(await loadNote(d.note.id));
    renderNoteTabs();
  } catch (e) { toast(e.message, "err"); }
}

async function loadNote(nid) {
  /* Fetches one page into the editor, null when it cannot be loaded.
  * The editor turns read-only when the server refuses edits
  */
  try {
    const d = await api("/api/notes/" + nid);
    S.note = d.note;
    S.canEdit = d.can_edit;
    S.colors = d.colors || S.colors;
    $("#note-editor").value = S.note.content;
    $("#note-editor").readOnly = !d.can_edit;
    renderBlameLegend(d.show_blame);
    // Text written into the page while it was closed — an appended summary, an
    // imported snippet — can change who holds a line in it, and that is what
    // decides whether its tab offers a delete button.
    const page = S.pages.find(p => p.id === nid);
    if (page && String(page.authors) !== String(S.note.authors)) {
      page.authors = S.note.authors;
      renderNoteTabs();
    }
    return S.note;
  } catch (e) {
    toast("Failed to load the note: " + e.message, "err");
    return null;
  }
}

function renderBlameLegend(show) {
  /* Color key of the page's authors.
  * Hidden when blame is off, or when nobody is attributed
  */
  const el = $("#blame-legend");
  if (!show || !S.note) { el.classList.add("hidden"); return; }
  const authors = [...new Set((S.note.blame || []).map(r => r.author).filter(Boolean))];
  if (!authors.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.innerHTML = authors.map(a =>
    '<span class="legend-item"><span class="dot" style="background:'
    + blameColor(S.colors, a) + '"></span>' + escapeHtml(a) + "</span>").join("");
}

$("#note-editor").addEventListener("input", scheduleGutter);
$("#note-editor").addEventListener("scroll", syncGutterScroll);
window.addEventListener("resize", scheduleGutter);

let _gutterTimer;
function scheduleGutter() {
  /* Coalesces bursts of gutter redraws
  */
  clearTimeout(_gutterTimer);
  _gutterTimer = setTimeout(renderGutter, 60);
}

function measureLines(text) {
  /* Pixel height of every line, measured through a mirror of the editor.
  * A wrapped line is taller than one line, and its stripe has to follow
  */
  const ta = $("#note-editor");
  const mirror = $("#note-mirror");
  const cs = getComputedStyle(ta);
  for (const prop of ["fontFamily", "fontSize", "fontWeight", "lineHeight",
                      "letterSpacing", "whiteSpace", "wordBreak",
                      "paddingLeft", "paddingRight", "textIndent"]) {
    mirror.style[prop] = cs[prop];
  }
  mirror.style.width = ta.clientWidth + "px";
  mirror.innerHTML = "";
  const lines = text.split("\n");
  const spans = lines.map(line => {
    const d = document.createElement("div");
    d.textContent = line === "" ? " " : line;
    mirror.appendChild(d);
    return d;
  });
  return spans.map(d => d.offsetHeight);
}

function renderGutter() {
  /* Draws the blame stripes beside the editor.
  * Line-exact, so nothing is drawn while the textarea is hidden
  */
  const gutter = $("#blame-gutter");
  const ta = $("#note-editor");
  const on = S.note && S.caps.show_blame !== false && !ta.classList.contains("hidden");
  gutter.classList.toggle("hidden", !on);
  if (!on) return;

  const text = ta.value;
  const heights = measureLines(text);
  const perLine = expandBlame(S.note.blame, text.split("\n").length);

  // The gutter clips; an inner track scrolls with the textarea.
  const track = document.createElement("div");
  track.className = "blame-track";
  track.style.paddingTop = getComputedStyle(ta).paddingTop;

  let i = 0;
  while (i < perLine.length) {
    const author = perLine[i].author;
    let h = 0, j = i;
    while (j < perLine.length && perLine[j].author === author) {
      h += heights[j] || 0;
      j++;
    }
    const seg = document.createElement("div");
    seg.className = "blame-seg";
    seg.style.height = h + "px";
    seg.style.background = blameColor(S.colors, author);
    seg.title = author ? author + " — " + (perLine[i].ts || "") : "unattributed";
    track.appendChild(seg);
    i = j;
  }
  gutter.innerHTML = "";
  gutter.appendChild(track);
  syncGutterScroll();
}

function syncGutterScroll() {
  /* Follows the editor scroll: shifts the blame track
  */
  const track = $("#blame-gutter .blame-track");
  if (!track) return;
  track.style.transform = "translateY(" + -$("#note-editor").scrollTop + "px)";
}

$("#btn-import").addEventListener("click", () => {
  $("#imp-url").value = ""; $("#imp-file").value = "";
  const sel = LIB.sel;
  $("#imp-folder").value = (sel && sel.kind === "folder") ? sel.id : "";
  openModal("modal-import");
});

$("#imp-submit").addEventListener("click", async () => {
  const url = $("#imp-url").value.trim();
  const file = $("#imp-file").files[0];
  if (!url && !file) { toast("Give a link or pick a file", "warn"); return; }
  const fd = new FormData();
  if (url) fd.append("url", url);
  if (file) fd.append("file", file);
  fd.append("project", PID);
  fd.append("folder", $("#imp-folder").value.trim());
  const btn = $("#imp-submit");
  btn.disabled = true; btn.textContent = "Importing…";
  try {
    const d = await api("/api/import", { method: "POST", body: fd });
    closeModal("modal-import");
    toast(d.already_here ? "Already in this project"
      : d.duplicate ? "Known source — linked without re-downloading" : "Imported", "ok");
    await refreshTree();
    await LIB.selectSource(d.id);
  } catch (e) {
    toast("Import failed: " + e.message, "err", 7000);
  } finally {
    btn.disabled = false; btn.textContent = "Import";
  }
});

async function removeSource(sid) {
  /* Removes a source from the project, after confirmation.
  * The note pages written here go with it, the document itself is kept
  */
  const s = S.rows.find(x => x.id === sid);
  if (!await confirmModal({
    title: "Remove from project",
    body: 'Remove "' + (s ? s.title : sid) + '" from this project?',
    hint: "Its note pages here are deleted. The document itself is kept.",
    confirm: "Remove",
    danger: true,
  })) return;
  try {
    await api("/api/projects/" + PID + "/sources/" + sid, { method: "DELETE" });
    const sel = LIB.sel;
    if (sel && sel.kind === "source" && sel.id === sid) LIB.TREE.sel = null;
    await refreshTree();
    toast("Removed from project", "ok");
  } catch (e) { toast(e.message, "err"); }
}

function currentSelectionText() {
  /* Selected text, taken from the editor when it is on screen, from the page
  * otherwise
  */
  const ta = $("#note-editor");
  if (!ta.classList.contains("hidden") && ta.selectionStart !== ta.selectionEnd) {
    return ta.value.slice(ta.selectionStart, ta.selectionEnd);
  }
  return String(window.getSelection() || "").trim();
}

$("#btn-send").addEventListener("click", async () => {
  if (!S.note) { toast("Open a note first", "warn"); return; }
  const text = currentSelectionText();
  if (!text.trim()) { toast("Select the text you want to send", "warn"); return; }

  $("#send-preview").textContent = text.length > 400 ? text.slice(0, 400) + "…" : text;
  const t = await api("/api/transfer/targets");
  const rows = [{ id: t.personal.id, name: t.personal.name + " (your workspace)" }]
    .concat(t.projects.filter(p => p.id !== PID));
  $("#send-targets").innerHTML = rows.map(p =>
    '<button class="check-row target" data-to="' + p.id + '">'
    + escapeHtml(p.name) + "</button>").join("")
    || '<p class="muted">Nowhere else to send this yet.</p>';
  openModal("modal-send");

  $("#send-targets").onclick = async ev => {
    const b = ev.target.closest("[data-to]");
    if (!b) return;
    try {
      await api("/api/transfer/snippet", {
        method: "POST",
        body: { note_id: S.note.id, to: b.dataset.to, text },
      });
      closeModal("modal-send");
      toast("Sent", "ok");
    } catch (e) { toast(e.message, "err", 6000); }
  };
});

$("#btn-import-notes").addEventListener("click", async () => {
  if (LIB.busy()) return;      // importing a page reloads the one being written into
  const sel = LIB.sel;
  if (!sel || sel.kind !== "source") {
    toast("Select a source first", "warn"); return;
  }
  const list = $("#import-notes-list");
  list.innerHTML = '<p class="muted">Looking…</p>';
  openModal("modal-import-notes");
  try {
    const where = (await api("/api/transfer/elsewhere/" + sel.id)).projects;
    const rows = [];
    for (const p of where.filter(x => x.project_id !== PID && x.member)) {
      const pages = (await api("/api/projects/" + p.project_id
        + "/sources/" + sel.id + "/notes")).notes;
      for (const pg of pages) {
        rows.push('<button class="check-row target" data-note="' + pg.id + '">'
          + escapeHtml(p.name) + " · " + escapeHtml(pg.name || "Notes")
          + ' <span class="muted">by ' + escapeHtml(pg.created_by) + "</span></button>");
      }
    }
    list.innerHTML = rows.join("")
      || '<p class="muted">No other project you belong to has notes on this source.</p>';
  } catch (e) {
    list.innerHTML = '<p class="muted">' + escapeHtml(e.message) + "</p>";
  }

  list.onclick = async ev => {
    const b = ev.target.closest("[data-note]");
    if (!b) return;
    try {
      await api("/api/transfer/note",
        { method: "POST", body: { note_id: b.dataset.note, to: PID } });
      closeModal("modal-import-notes");
      toast("Page imported", "ok");
      await LIB.adoptNote(await loadPages(sel.id));
    } catch (e) { toast(e.message, "err", 6000); }
  };
});

function renderOverview() {
  /* Fills the Overview tab from the loaded project
  */
  const p = S.project;
  if (!p) return;
  $("#ov-name").textContent = p.name;
  $("#ov-desc").textContent = (p.description || "").trim()
    || "No description yet — describe what this project is about in Settings.";
  $("#ov-badge").classList.toggle("hidden", !p.completed);
  $("#ov-count-src").textContent = String(S.rows.length);
  $("#ov-count-mem").textContent = String((p.members || []).length);
  $("#ov-created").textContent = fmtDate(p.created_at) || "–";
  renderOverviewSources();
  renderOverviewMembers();
}

function renderOverviewSources() {
  /* First five sources, then a count of the rest
  */
  const el = $("#ov-src");
  el.innerHTML = "";
  if (!S.rows.length) {
    el.innerHTML = '<p class="muted">No sources yet — head to the Library to import some.</p>';
    return;
  }
  for (const s of S.rows.slice(0, 5)) {
    const row = document.createElement("button");
    row.className = "mini-row ov-src-row";
    row.dataset.id = s.id;
    row.innerHTML = '<span class="mini-name" title="' + escapeHtml(s.title) + '">'
      + escapeHtml(s.title) + "</span>" + typePill(s.source_type);
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
  /* Every member, with their color and their role
  */
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
    row.innerHTML =
      '<span class="dot" style="background:' + escapeHtml(m.color) + '"></span>' +
      '<span class="mini-name">' + escapeHtml(m.name)
      + (m.name === KNDB.user ? ' <span class="muted">(you)</span>' : "") + "</span>" +
      '<span class="pill type-completed">' + escapeHtml(m.role) + "</span>";
    el.appendChild(row);
  }
}

function renderSettings() {
  /* Fills the Settings tab from the loaded project
  */
  const p = S.project;
  if (!p) return;
  $("#set-name").value = p.name || "";
  $("#set-desc").value = p.description || "";
  $("#btn-toggle-complete").textContent = p.completed ? "Reopen project" : "Mark completed";
  renderMembers();
}

function roleOption(role, selected) {
  return '<option value="' + escapeHtml(role) + '"'
    + (role === selected ? " selected" : "") + ">" + escapeHtml(role) + "</option>";
}

function renderMembers() {
  /* Member rows of the Settings tab.
  * A role change or a removal is saved at once, there is no save button
  */
  const el = $("#prj-members");
  const p = S.project;
  if (!p) return;
  const roleSel = $("#member-role");
  if (!roleSel.options.length) {          // filled once; keeps the user's pick
    roleSel.innerHTML = KNDB.roles.map(r => roleOption(r, KNDB.defaultRole)).join("");
  }
  el.innerHTML = "";
  for (const m of p.members || []) {
    const row = document.createElement("div");
    row.className = "mini-row";
    const isYou = m.name === KNDB.user;
    row.innerHTML =
      '<span class="dot" style="background:' + escapeHtml(m.color) + '"></span>' +
      '<span class="mini-name">' + escapeHtml(m.name)
      + (isYou ? ' <span class="muted">(you)</span>' : "") + "</span>" +
      '<select class="input small role-sel"' + (isYou ? " disabled" : "") + ">" +
        KNDB.roles.map(r => roleOption(r, m.role)).join("") +
      "</select>" +
      (isYou ? "" : '<button class="btn small del" title="Remove member">×</button>');
    const sel = row.querySelector(".role-sel");
    sel.addEventListener("change", async () => {
      try {
        const d = await api("/api/projects/" + PID + "/members/"
          + encodeURIComponent(m.name) + "/role", { method: "POST", body: { role: sel.value } });
        S.project.members = d.members;
        renderMembers();
        toast("Role updated to " + sel.value, "ok");
      } catch (e) { toast(e.message, "err"); renderMembers(); }
    });
    const delBtn = row.querySelector(".del");
    if (delBtn) delBtn.addEventListener("click", async () => {
      if (!await confirmModal({
        title: "Remove member",
        body: "Remove " + m.name + " from the project?",
        hint: "Their note pages stay; they simply lose access.",
        confirm: "Remove",
        danger: true,
      })) return;
      try {
        const d = await api("/api/projects/" + PID + "/members/"
          + encodeURIComponent(m.name), { method: "DELETE" });
        S.project.members = d.members;
        renderMembers();
        toast("Member removed", "ok");
      } catch (e) { toast(e.message, "err"); }
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
      method: "POST", body: { name, description: $("#set-desc").value },
    });
    S.project = d.project;
    $("#sub-name").textContent = d.project.name;
    document.title = d.project.name + " — KNDB";
    toast("Details saved", "ok");
  } catch (e) { toast("Save failed: " + e.message, "err"); }
});

$("#btn-toggle-complete").addEventListener("click", async () => {
  const next = !S.project.completed;
  if (!await confirmModal(next ? {
    title: "Archive project",
    body: "Mark this project as completed?",
    hint: "It turns read-only — no edits, imports or new pages until it is reopened.",
    confirm: "Archive",
  } : {
    title: "Reopen project",
    body: "Reopen this project?",
    hint: "It becomes editable again for everyone who is a member.",
    confirm: "Reopen",
  })) return;
  try {
    const d = await api("/api/projects/" + PID, { method: "POST", body: { completed: next } });
    S.project = d.project;
    $("#prj-badge").classList.toggle("hidden", !d.project.completed);
    $("#btn-toggle-complete").textContent = d.project.completed ? "Reopen project" : "Mark completed";
    toast(next ? "Project archived as completed" : "Project reopened", "ok");
  } catch (e) { toast(e.message, "err"); }
});

$("#btn-del").addEventListener("click", async () => {
  if (!await confirmModal({
    title: "Delete project",
    body: "Delete this project?",
    hint: "Every note page written in it is deleted. The documents themselves "
        + "stay in the library.",
    confirm: "Delete project",
    danger: true,
  })) return;
  try {
    await api("/api/projects/" + PID, { method: "DELETE" });
    toast("Project deleted", "ok");
    location.href = "/projects";
  } catch (e) { toast(e.message, "err"); }
});

$("#btn-add-member").addEventListener("click", async () => {
  const name = $("#member-name").value.trim();
  if (!name) { toast("Member name required", "warn"); return; }
  try {
    const d = await api("/api/projects/" + PID + "/members",
      { method: "POST", body: { name, role: $("#member-role").value } });
    S.project.members = d.members;
    $("#member-name").value = "";
    renderMembers();
    await refreshTree();
    toast("Member added", "ok");
  } catch (e) { toast(e.message, "err"); }
});

async function openAddSources() {
  /* Modal listing the workspace sources this project does not have yet.
  * The workspace is fetched once, then filtered in place
  */
  if (!S.workspace.length) {
    try { S.workspace = (await api("/api/sources")).sources; }
    catch (e) { toast("Could not load your workspace: " + e.message, "err"); return; }
  }
  const inProject = new Set(S.rows.map(s => s.id));
  const list = $("#add-src-list");
  const filter = $("#add-src-filter");
  const draw = () => {
    const f = filter.value.trim().toLowerCase();
    list.innerHTML = "";
    for (const s of S.workspace) {
      if (inProject.has(s.id)) continue;
      if (f && s.title.toLowerCase().indexOf(f) === -1) continue;
      const row = document.createElement("label");
      row.className = "check-row";
      row.innerHTML = '<input type="checkbox" value="' + s.id + '"><span>'
        + escapeHtml(s.title) + "</span>" + typePill(s.source_type);
      list.appendChild(row);
    }
    if (!list.children.length) list.innerHTML = '<p class="muted">No more sources to add.</p>';
  };
  filter.value = "";
  filter.oninput = draw;
  draw();
  openModal("modal-add-src");
}

$("#btn-add-existing").addEventListener("click", () => {
  closeModal("modal-import");
  openAddSources();
});

$("#add-src-submit").addEventListener("click", async () => {
  const ids = $$("#add-src-list input:checked").map(i => i.value);
  if (!ids.length) { toast("Select at least one source", "warn"); return; }
  try {
    for (const sid of ids) {
      await api("/api/transfer/source", {
        method: "POST",
        body: { source_id: sid, from: KNDB.personal.id, to: PID },
      });
    }
    closeModal("modal-add-src");
    toast(ids.length + " source" + (ids.length === 1 ? "" : "s") + " added", "ok");
    await refreshTree();
  } catch (e) { toast("Add failed: " + e.message, "err"); }
});

document.addEventListener("click", e => {
  const b = e.target.closest("[data-close]");
  if (b) closeModal(b.dataset.close);
});
$$(".modal").forEach(m => m.addEventListener("mousedown", e => {
  if (e.target === m) m.classList.add("hidden");
}));

loadProject();
