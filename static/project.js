"use strict";

/* Project workspace. Shared helpers ($, api, toast, renderMarkdown, blame…)
 * come from common.js. */

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
  sel: null,         // {kind:"source"|"note"|"folder", id} — mirrors TREE.sel
  pages: [],         // note pages for the selected source
  note: null,        // the loaded page {id, content, blame, version…}
  // Projects open in edit mode: the blame gutter is line-exact and therefore
  // only drawn over the textarea, and knowing who wrote what is the point of
  // reading someone else's page. Ctrl+D still flips to rendered preview.
  noteMode: "edit",
  noteDirty: false,
  saveTimer: null,
};


/* ---------- Tab switching ---------- */

function switchTab(tab) {
  $$(".subtab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  ["overview", "library", "latex", "settings"].forEach(t => {
    $("#tab-" + t).classList.toggle("hidden", t !== tab);
  });
  if (tab === "library") scheduleGutter();
}

$$(".subtab").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));

document.addEventListener("click", e => {
  const srcRow = e.target.closest(".ov-src-row");
  if (srcRow) {
    switchTab("library");
    selectSource(srcRow.dataset.id);
  }
});


/* ---------- The library tree (shared with the personal workspace) ---------- */

const TREE = makeTree({
  host: $("#dir-list"),
  readOnly: () => S.readOnly,
  role: () => S.role,
  showQuestions: () => !!S.caps.allow_quiz,
  removeTitle: "Remove from project",
  empty: '<div class="empty">Nothing here yet.<br>'
       + "Use <b>+ Import</b> to bring in a source.</div>",
  reload: refreshTree,
  onSelect: openRef,
  onRemoveSource: removeSource,
  onDeleteNote: deletePage,
});

TREE.setProject(PID);
$("#btn-new-folder").addEventListener("click", () => TREE.newFolder());
$("#btn-new-note").addEventListener("click", () => TREE.newNote());


/* ---------- Load ---------- */

async function loadProject() {
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
  loadTexDraft();
  await refreshTree();
  setupSplitters();
  renderOverview();
}

async function refreshTree() {
  let t;
  try {
    t = await api("/api/projects/" + PID + "/tree");
  } catch (e) {
    toast("Failed to load the library: " + e.message, "err");
    return;
  }
  S.rows = t.sources;
  S.folders = t.folders;
  S.standalone = t.notes;
  S.colors = t.colors || {};
  const n = S.rows.length;
  $("#dir-count").textContent = n ? n + " source" + (n === 1 ? "" : "s") : "";
  TREE.setData({ folders: t.folders, sources: t.sources, notes: t.notes });
  TREE.render();
  // setData drops a selection whose row is gone; the panels have to follow.
  if (S.sel && !TREE.sel) resetSelection();
}



/* ---------- Selection ---------- */

function resetSelection() {
  S.sel = null; S.note = null; S.pages = [];
  $("#res-title").textContent = "Resource";
  $("#res-meta").classList.add("hidden");
  $("#btn-edit-tags").disabled = true;
  const empty = $("#res-empty");
  clearViewers();
  $("#res-md").innerHTML = "";
  empty.classList.remove("hidden");
  empty.textContent = "Select a source from the library to read it and take notes next to it.";
  $("#note-tabs").classList.add("hidden");
  $("#note-editor").value = "";
  $("#note-editor").classList.add("hidden");
  $("#note-preview").classList.remove("hidden");
  $("#note-preview").innerHTML = '<p class="muted">Select something in the library.</p>';
  $("#blame-gutter").classList.add("hidden");
  $("#blame-legend").classList.add("hidden");
  $("#note-save-state").textContent = "";
  ANCHORS.clear();
}

/** Every selection lands here, whether it came from a click in the tree, the
 *  overview, or an import that just finished. */
async function openRef(ref) {
  await flushNote();
  S.sel = ref;
  if (!ref) return resetSelection();
  $("#btn-edit-tags").disabled = ref.kind !== "source";
  if (ref.kind === "source") return selectedSource(ref.id);
  if (ref.kind === "note") return selectedStandalone(ref.id);
  return selectedFolder(ref.id);
}

function selectSource(id) { return TREE.setSelection({ kind: "source", id }); }

async function selectedSource(id) {
  const s = S.rows.find(x => x.id === id);
  if (!s) return;
  await loadResource(s);
  await loadPages(id);
}

async function selectedFolder(path) {
  S.pages = []; S.note = null;
  ANCHORS.clear();
  $("#note-tabs").classList.add("hidden");
  $("#blame-gutter").classList.add("hidden");
  $("#blame-legend").classList.add("hidden");
  $("#note-editor").classList.add("hidden");
  $("#note-preview").classList.remove("hidden");
  $("#note-preview").innerHTML =
    '<p class="muted">Folder <b>' + escapeHtml(path) + '</b>. Its README is shown on the left; '
    + 'add one with <b>+ Note</b> named <code>README</code>.</p>';

  const readme = (await api("/api/projects/" + PID + "/readme?folder="
                            + encodeURIComponent(path))).note;
  showCompiled(path, readme
    ? readme.content
    : "*No README in this folder yet.*");
}

async function selectedStandalone(nid) {
  S.pages = [];
  $("#note-tabs").classList.add("hidden");
  await loadNote(nid);
  // A standalone note has no document to show, so the viewer renders the note
  // itself. That makes the editor a live preview and Ctrl+D pointless.
  S.noteMode = "edit";
  applyNoteMode();
  showCompiled(S.note.name, S.note.content);
}

$("#btn-edit-tags").addEventListener("click", () => {
  const src = S.sel && S.sel.kind === "source" ? S.rows.find(s => s.id === S.sel.id) : null;
  if (!src) return;
  openTagsModal({
    source: src,
    readOnly: S.readOnly,
    // The unfolded tree rows show the same tags, so they redraw with the modal.
    onSaved: () => TREE.render(),
  });
});

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

async function loadResource(s) {
  $("#res-title").textContent = s.title;
  $("#res-meta").textContent = s.source_type;
  $("#res-meta").classList.remove("hidden");
  const md = $("#res-md"), empty = $("#res-empty");
  clearViewers();
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
  } else if (isPdf(s)) {
    await PDFView.open(src, $("#res-pdf"));
  } else {
    $("#res-frame").src = src;
    $("#res-frame").classList.remove("hidden");
  }
}



/* ---------- Note pages ---------- */

async function loadPages(sid) {
  const d = await api("/api/projects/" + PID + "/sources/" + sid + "/notes");
  S.pages = d.notes;
  S.caps = d.capabilities || S.caps;
  S.colors = d.colors || S.colors;
  renderNoteTabs();
  if (S.pages.length) {
    await loadNote(S.pages[0].id);
    renderNoteTabs();          // now that there is a current page to mark active
  } else {
    S.note = null;
    $("#note-editor").classList.add("hidden");
    $("#note-preview").classList.remove("hidden");
    $("#note-preview").innerHTML =
      '<p class="muted">No note pages yet — create one with <b>+</b> above.</p>';
    $("#blame-gutter").classList.add("hidden");
  }
}

function renderNoteTabs() {
  const bar = $("#note-tabs");
  if (S.sel && S.sel.kind !== "source") { bar.classList.add("hidden"); return; }
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
  const del = e.target.closest("[data-delpage]");
  if (del) { e.stopPropagation(); return deletePage(del.dataset.delpage); }
  if (e.target.closest("#note-tab-add")) return newPage();
  const tab = e.target.closest(".note-tab");
  if (!tab || !tab.dataset.id) return;
  // Clicking the name of the page you are already on renames it in place.
  if (tab.classList.contains("active") && e.target.closest(".note-tab-name")) {
    return startRename(tab);
  }
  await flushNote();
  await loadNote(tab.dataset.id);
  renderNoteTabs();
});

function canRename(page) {
  return !S.readOnly && (page.created_by === KNDB.user
    || KNDB.may(S.role, "edit_others"));
}

/** Rename a page from its tab. A modal for a two-word title was more ceremony
 *  than the act deserves, and the tab is where the name already is. */
function startRename(tab) {
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
    if (S.sel && S.sel.kind === "note" && S.sel.id === nid) {
      TREE.sel = null;
      resetSelection();
      await refreshTree();
    } else if (S.sel && S.sel.kind === "source") {
      await loadPages(S.sel.id);
    } else {
      await refreshTree();
    }
  } catch (e) { toast(e.message, "err", 7000); }
}

async function newPage() {
  const src = S.rows.find(s => s.id === S.sel.id);
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
      { method: "POST", body: { source_id: S.sel.id, name } });
    await loadPages(S.sel.id);
    await loadNote(d.note.id);
    renderNoteTabs();
  } catch (e) { toast(e.message, "err"); }
}

async function loadNote(nid) {
  try {
    const d = await api("/api/notes/" + nid);
    S.note = d.note;
    S.canEdit = d.can_edit;
    S.colors = d.colors || S.colors;
    $("#note-editor").value = S.note.content;
    $("#note-editor").readOnly = !d.can_edit;
    $("#note-save-state").textContent = "";
    S.noteDirty = false;
    applyNoteMode();
    renderBlameLegend(d.show_blame);
    await ANCHORS.load();
    return true;
  } catch (e) {
    toast("Failed to load the note: " + e.message, "err");
    return false;
  }
}

function renderBlameLegend(show) {
  const el = $("#blame-legend");
  if (!show || !S.note) { el.classList.add("hidden"); return; }
  const authors = [...new Set((S.note.blame || []).map(r => r.author).filter(Boolean))];
  if (!authors.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.innerHTML = authors.map(a =>
    '<span class="legend-item"><span class="dot" style="background:'
    + blameColor(S.colors, a) + '"></span>' + escapeHtml(a) + "</span>").join("");
}

function onNoteTyped() {
  if (!S.note) return;
  S.noteDirty = true;
  $("#note-save-state").textContent = "unsaved…";
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(saveNote, 1200);
  scheduleGutter();
}

async function flushNote() {
  clearTimeout(S.saveTimer);
  if (S.noteDirty) await saveNote();
}

async function saveNote() {
  if (!S.note || !S.noteDirty) return;
  const content = $("#note-editor").value;
  const nid = S.note.id;
  S.noteDirty = false;
  try {
    const d = await api("/api/notes/" + nid, {
      method: "PUT",
      body: { content, base_version: S.note.version },
    });
    if (S.note && S.note.id === nid) {
      S.note = d.note;
      renderBlameLegend(true);
      renderGutter();
      if (S.sel && S.sel.kind === "note") showCompiled(S.note.name, S.note.content);
    }
    // Writing into a page can change who holds a line in it, and that is what
    // decides whether the tab offers a delete button.
    const page = S.pages.find(x => x.id === nid);
    if (page && String(page.authors) !== String(d.note.authors)) {
      page.authors = d.note.authors;
      renderNoteTabs();
    }
    $("#note-save-state").textContent = "saved";
    // A sentence that is gone from the saved text takes its link with it.
    await ANCHORS.pruneLost();
  } catch (e) {
    S.noteDirty = true;
    $("#note-save-state").textContent = e.status === 403 ? "not allowed" : "save error";
    if (e.status === 403) {
      toast(e.message, "err", 7000);
      // Put back what the server still holds so the editor cannot drift out of
      // sync with a save that never landed.
      await loadNote(nid);
    } else if (e.status === 409) {
      toast(e.message + " — reloading the page", "warn", 6000);
      await loadNote(nid);
    } else {
      toast("Note save failed: " + e.message, "err");
    }
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
  if (!edit) $("#note-preview").innerHTML = renderMarkdown($("#note-editor").value);
  scheduleGutter();
  ANCHORS.draw();     // links are drawn over the textarea, so preview hides them
}

$("#note-editor").addEventListener("input", onNoteTyped);
$("#note-editor").addEventListener("blur", () => { if (S.noteDirty) saveNote(); });
$("#note-editor").addEventListener("scroll", syncGutterScroll);
window.addEventListener("resize", scheduleGutter);

document.addEventListener("keydown", e => {
  if (e.ctrlKey && e.key.toLowerCase() === "d") {
    e.preventDefault();
    // Standalone notes are already live-previewed in the viewer pane.
    if (!S.note || (S.sel && S.sel.kind === "note")) return;
    S.noteMode = S.noteMode === "edit" ? "preview" : "edit";
    applyNoteMode();
  }
});


/* ---------- Blame gutter ---------- */

let _gutterTimer;
function scheduleGutter() {
  clearTimeout(_gutterTimer);
  _gutterTimer = setTimeout(renderGutter, 60);
}

/** Per-line pixel heights, measured through a mirror element so that wrapped
 *  lines line up with their blame stripe instead of drifting down the page. */
function measureLines(text) {
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
  const track = $("#blame-gutter .blame-track");
  if (!track) return;
  track.style.transform = "translateY(" + -$("#note-editor").scrollTop + "px)";
}


/* ---------- Anchors: note sentences grounded in the document ---------- */

/** The source whose document is in the viewer right now. */
function currentSource() {
  const sid = S.note ? S.note.source_id : (S.sel && S.sel.kind === "source" ? S.sel.id : "");
  return sid ? S.rows.find(r => r.id === sid) || null : null;
}

/** What the viewer is showing, in the terms the anchoring module needs: our
 *  own PDF renderer, or a piece of HTML it can measure and draw over. */
function docTarget() {
  const s = currentSource();
  if (!s) return null;
  if (isPdf(s)) return { kind: "pdf" };
  if (s.source_type === "md") {
    const root = $("#res-md");
    return root.classList.contains("hidden")
      ? null : { kind: "html", root, scroller: root };
  }
  // An HTML source is served from our own origin, so its frame is reachable.
  const frame = $("#res-frame");
  if (frame.classList.contains("hidden")) return null;
  try {
    const d = frame.contentDocument;
    if (d && d.body) {
      return { kind: "html", root: d.body,
               scroller: d.scrollingElement || d.documentElement,
               win: frame.contentWindow };
    }
  } catch (_) { /* cross-origin: not linkable, and nothing we can do */ }
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
  projectId: () => PID,
  noteId: () => (S.note ? S.note.id : ""),
  sourceId: () => (S.note ? S.note.source_id : ""),
  colors: () => S.colors,
  canLink: () => !S.readOnly && S.canEdit !== false,
  inPreview: () => $("#note-editor").classList.contains("hidden"),
  ensureEditMode: async () => {
    if (!$("#note-editor").classList.contains("hidden")) return;
    S.noteMode = "edit";
    applyNoteMode();
  },
  openNote: async nid => {
    await flushNote();
    const ok = await loadNote(nid);
    renderNoteTabs();
    return ok;
  },
  onChange: ({ lost }) => {
    const el = $("#note-anchor-state");
    el.textContent = lost
      ? lost + (lost === 1 ? " link no longer resolves" : " links no longer resolve")
      : "";
  },
});


/* ---------- Import & sources ---------- */

$("#btn-import").addEventListener("click", () => {
  $("#imp-url").value = ""; $("#imp-file").value = "";
  $("#imp-folder").value = (S.sel && S.sel.kind === "folder") ? S.sel.id : "";
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
    await selectSource(d.id);
  } catch (e) {
    toast("Import failed: " + e.message, "err", 7000);
  } finally {
    btn.disabled = false; btn.textContent = "Import";
  }
});

async function removeSource(sid) {
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
    if (S.sel && S.sel.kind === "source" && S.sel.id === sid) TREE.sel = null;
    await refreshTree();
    toast("Removed from project", "ok");
  } catch (e) { toast(e.message, "err"); }
}



/* ---------- Transfers ---------- */

function currentSelectionText() {
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
  if (!S.sel || S.sel.kind !== "source") {
    toast("Select a source first", "warn"); return;
  }
  const list = $("#import-notes-list");
  list.innerHTML = '<p class="muted">Looking…</p>';
  openModal("modal-import-notes");
  try {
    const where = (await api("/api/transfer/elsewhere/" + S.sel.id)).projects;
    const rows = [];
    for (const p of where.filter(x => x.project_id !== PID && x.member)) {
      const pages = (await api("/api/projects/" + p.project_id
        + "/sources/" + S.sel.id + "/notes")).notes;
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
      await loadPages(S.sel.id);
    } catch (e) { toast(e.message, "err", 6000); }
  };
});


/* ---------- Overview ---------- */

function renderOverview() {
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

function roleOption(role, selected) {
  return '<option value="' + escapeHtml(role) + '"'
    + (role === selected ? " selected" : "") + ">" + escapeHtml(role) + "</option>";
}

function renderMembers() {
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


/* ---------- Add existing sources from the workspace ---------- */

async function openAddSources() {
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


/* ---------- Splitters ---------- */

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
      scheduleGutter();
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
}

function setupSplitters() {
  setupSplitter($('.splitter[data-split="dir"]'), $("#panel-dir"), "kndb.w.prj.dir", 0.14, 0.72);
  setupSplitter($('.splitter[data-split="res"]'), $("#panel-res"), "kndb.w.prj.res", 0.2, 0.8);
}


/* ---------- Search ---------- */

let _searchTimer;
$("#search").addEventListener("input", function () {
  clearTimeout(_searchTimer);
  // "@ml" matches tags only, anything else the name or a tag — see
  // tree.js:queryPredicate, which both pages filter through.
  _searchTimer = setTimeout(() => {
    TREE.applyFilter(TREE.queryPredicate($("#search").value));
  }, 200);
});


/* ---------- Init ---------- */

document.addEventListener("click", e => {
  const b = e.target.closest("[data-close]");
  if (b) closeModal(b.dataset.close);
});
$$(".modal").forEach(m => m.addEventListener("mousedown", e => {
  if (e.target === m) m.classList.add("hidden");
}));
window.addEventListener("beforeunload", () => { if (S.noteDirty) saveNote(); });

loadProject();
