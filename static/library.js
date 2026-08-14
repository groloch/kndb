"use strict";

/* The library page: the tree on the left, the document in the middle, the notes
 * on the right.
 *
 * The personal workspace and a project's Library tab *are* the same page — same
 * markup, same tree, same three viewers, same editor. They differ only in what a
 * note is allowed to be and where it is written, and that difference is what
 * `cfg` carries.
 *
 * cfg = {
 *   projectId:  () => pid,          // read late: the workspace learns its own id
 *   root:       () => element,      // the row holding the panes (splitter geometry)
 *   keys:       "kndb.",            // localStorage namespace: widths, fold state
 *   noteMode:   "preview"|"edit",   // what a source's note opens as
 *
 *   tree:       {…},                // passed to makeTree, minus what we own
 *   anchors:    {…},                // merged over the defaults below
 *
 *   onTree:     t => …,             // the /tree payload, for what the page keeps too
 *   onSelect:   ref => …,           // panes are filled: enable the page's buttons
 *   canSelect:  () => bool,         // veto a change of selection (mid-summary…)
 *
 *   loadNotes:  async source => note|null,  // the note pane beside a document
 *   loadNote:   async nid => note|null,     // one page, into #note-editor
 *   persist:    async (content, nid) => …,  // write it; throw to report a failure
 *   onSaveError:(err, nid) => …,
 *   saveState:  err => "save error",
 *   onNoteMode: edit => …,          // the page redraws what hangs off the editor
 *   searchPredicate: async (q, local) => pred,
 * }
 *
 * A "note" is anything with {id, name, content}. Loading one is the page's job
 * because only the page knows the route and the permissions; filling #note-editor
 * is part of that job. What happens to it afterwards — dirty flag, debounce,
 * preview, links — is ours.
 */

function makeLibrary(cfg) {
  const L = {
    sources: [], folders: [], notes: [],
    sel: null,          // {kind:"source"|"note"|"folder", id} — mirrors TREE.sel
    source: null,       // the selected source row, or null
    noteId: null,
    noteName: "",
    noteMode: cfg.noteMode || "preview",
    noteDirty: false,
    saveTimer: null,
    compileTimer: null,
  };

  const pid = () => (cfg.projectId ? cfg.projectId() : "");
  const key = suffix => (cfg.keys || "kndb.") + suffix;

  // What the page shipped in its markup, kept so that closing a selection puts
  // back that page's own wording instead of a string invented here.
  const EMPTY_RES = $("#res-empty").innerHTML;
  const EMPTY_NOTE = $("#note-preview").innerHTML;
  const closeBtn = $("#btn-close-res");

  const TREE = makeTree(Object.assign({
    host: $("#dir-list"),
    reload: refreshTree,
    onSelect: openRef,
  }, cfg.tree || {}));

  async function refreshTree() {
    /* Refetches the whole tree, redraws it, and hands the payload to the page
    */
    let t;
    try {
      t = await api(`/api/projects/${pid()}/tree`);
    } catch (e) {
      toast("Failed to load the library: " + e.message, "err");
      return;
    }
    L.sources = t.sources;
    L.folders = t.folders;
    L.notes = t.notes;
    const n = L.sources.length;
    $("#dir-count").textContent = n ? `${n} source${n === 1 ? "" : "s"}` : "";
    if (cfg.onTree) cfg.onTree(t);
    TREE.setData({ folders: t.folders, sources: t.sources, notes: t.notes });
    TREE.render();
    // setData drops a selection whose row is gone; the panes have to follow.
    if (L.sel && !TREE.sel) await resetSelection();
  }

  async function openRef(ref) {
    /* Every selection lands here, whatever opened it: a click in the tree, an
    * import that just finished, a link followed from somewhere else.
    * A null ref closes the current one
    */
    if (cfg.canSelect && !cfg.canSelect()) return;
    await flushNote();
    L.sel = ref;
    // Closing is about the selection, not about having a document: a folder and
    // a standalone note can be closed too.
    if (closeBtn) closeBtn.disabled = !ref;
    if (!ref) return resetSelection();
    if (ref.kind === "source") await openSource(ref.id);
    else if (ref.kind === "note") await openStandalone(ref.id);
    else await openFolder(ref.id);
    if (cfg.onSelect) cfg.onSelect(ref);
  }

  function select(ref) { return TREE.setSelection(ref); }

  function selectSource(id) { return TREE.setSelection({ kind: "source", id }); }

  async function openSource(id) {
    /* Fills the viewer with a source, and the note pane with the note the page
    * loads beside it
    */
    const s = L.sources.find(x => x.id === id);
    if (!s) return;
    L.source = s;
    await loadResource(s);
    await adoptNote(cfg.loadNotes ? await cfg.loadNotes(s) : null);
  }

  async function openFolder(path) {
    /* A folder shows its README, which is what makes a folder worth selecting
    */
    L.source = null;
    await adoptNote(null);
    $("#note-preview").innerHTML =
      `<p class="muted">Folder <b>${escapeHtml(path)}</b>. Its README is shown on the left; `
      + "add one with <b>+ Note</b> named <code>README</code>.</p>";
    const readme = (await api(`/api/projects/${pid()}/readme?folder=`
                              + encodeURIComponent(path))).note;
    showCompiled(path, readme ? readme.content : "*No README in this folder yet.*");
  }

  async function openStandalone(nid) {
    /* Opens a note that has no document beside it: the viewer renders the note
    */
    L.source = null;
    const note = cfg.loadNote ? await cfg.loadNote(nid) : null;
    // A standalone note has no document to show, so the viewer renders the note
    // itself — which turns the editor into a live preview, and leaves Ctrl+D
    // with nothing to toggle. applyNoteMode forces edit mode for those, so the
    // toggle the user last chose for a source survives the detour.
    await adoptNote(note);
    if (note) showCompiled(note.name, note.content);
  }

  async function resetSelection() {
    /* Empties both panes, back to the empty-state markup the page shipped
    */
    L.sel = null;
    L.source = null;
    if (closeBtn) closeBtn.disabled = true;
    $("#res-title").textContent = "Resource";
    $("#res-meta").textContent = "";
    $("#res-meta").classList.add("hidden");
    clearViewers();
    $("#res-md").innerHTML = "";
    $("#res-empty").innerHTML = EMPTY_RES;
    $("#res-empty").classList.remove("hidden");
    await adoptNote(null);
    $("#note-editor").value = "";
    $("#note-preview").innerHTML = EMPTY_NOTE;
    if (cfg.onSelect) cfg.onSelect(null);
  }

  function clearViewers() {
    /* Hides every viewer.
    * The three are exclusive, and the PDF one holds a worker and a pile of
    * canvases, so leaving it costs more than a class
    */
    PDFView.destroy();
    $("#res-frame").classList.add("hidden");
    $("#res-frame").src = "about:blank";
    $("#res-md").classList.add("hidden");
    $("#res-empty").classList.add("hidden");
  }

  function showCompiled(title, markdown) {
    /* Renders markdown into the viewer, under a title
    */
    $("#res-title").textContent = title || "Note";
    $("#res-meta").classList.add("hidden");
    clearViewers();
    const md = $("#res-md");
    md.innerHTML = renderMarkdown(markdown);
    md.classList.remove("hidden");
  }

  async function loadResource(s) {
    /* Shows a source: markdown rendered here, a PDF in our own renderer,
    * anything else in the frame
    */
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

  function recompileNote() {
    /* Recompiles the viewer from the textarea, keeping the reader where they were
    */
    const md = $("#res-md");
    const top = md.scrollTop;
    md.innerHTML = renderMarkdown($("#note-editor").value);
    md.scrollTop = top;
  }

  function scheduleCompile() {
    /* Redraws a standalone note's viewer once the typing pauses.
    * The viewer *is* the note when there is no document, and markdown plus
    * sanitising is not free on a long one
    */
    if (!L.sel || L.sel.kind !== "note") return;
    clearTimeout(L.compileTimer);
    L.compileTimer = setTimeout(recompileNote, 150);
  }

  async function adoptNote(note) {
    /* Takes over a note the page has just loaded into the textarea.
    * null lets go of the last one
    */
    L.noteId = note ? note.id : null;
    L.noteName = note ? (note.name || "") : "";
    L.noteDirty = false;
    setSaveState("");
    applyNoteMode();
    // Nothing to anchor without a note, and the doc surface has to let go of a
    // viewer that is about to hold something else.
    if (note) await ANCHORS.load(); else ANCHORS.clear();
    return note;
  }

  function setSaveState(text) { $("#note-save-state").textContent = text; }

  function onNoteTyped() {
    /* Marks the note dirty and pushes the autosave further out
    */
    if (!L.noteId) return;
    L.noteDirty = true;
    setSaveState("unsaved…");
    clearTimeout(L.saveTimer);
    L.saveTimer = setTimeout(saveNote, 1200);
    scheduleCompile();
  }

  async function saveNote() {
    /* Writes the note out through the page's persist.
    * Stays dirty on a failure, so the next pause tries again
    */
    if (!L.noteId || !L.noteDirty) return;
    const content = $("#note-editor").value;
    const nid = L.noteId;
    L.noteDirty = false;
    try {
      await cfg.persist(content, nid);
      setSaveState("saved");
      // A sentence that is gone from the saved text takes its link with it.
      await ANCHORS.pruneLost();
    } catch (e) {
      L.noteDirty = true;
      setSaveState(cfg.saveState ? cfg.saveState(e) : "save error");
      if (cfg.onSaveError) await cfg.onSaveError(e, nid);
      else toast("Note save failed: " + e.message, "err");
    }
  }

  async function flushNote() {
    /* Writes out a pending note now, before whatever is about to replace it
    */
    clearTimeout(L.saveTimer);
    if (L.noteDirty) await saveNote();
  }

  function applyNoteMode() {
    /* Shows either the textarea or the rendered preview.
    * A standalone note is always in edit mode, its preview being the viewer
    */
    const standalone = L.sel && L.sel.kind === "note";
    // With no note open there is nothing to edit, whatever the mode says: the
    // preview pane is where the page explains why.
    const edit = !!L.noteId && (standalone || L.noteMode === "edit");
    $("#note-editor").classList.toggle("hidden", !edit);
    $("#note-preview").classList.toggle("hidden", edit);
    $("#note-hint").textContent = standalone
      ? "Live preview — the compiled note is shown on the left"
      : "Ctrl+D toggles preview";
    // With no note open the preview holds a message from the page, and rendering
    // an empty textarea over it would only wipe it.
    if (!edit && L.noteId) {
      $("#note-preview").innerHTML = renderMarkdown($("#note-editor").value);
    }
    if (cfg.onNoteMode) cfg.onNoteMode(edit);
    ANCHORS.draw();     // links are drawn over the textarea, so preview hides them
  }

  function toggleNoteMode() {
    /* Swaps edit and preview, for a source's note only
    */
    // Nothing to toggle without a note, and a standalone one is already
    // compiled into the viewer as it is typed.
    if (!L.noteId || !L.sel || L.sel.kind !== "source") return;
    L.noteMode = L.noteMode === "edit" ? "preview" : "edit";
    applyNoteMode();
  }

  $("#note-editor").addEventListener("input", onNoteTyped);
  $("#note-editor").addEventListener("blur", () => { if (L.noteDirty) saveNote(); });
  window.addEventListener("beforeunload", () => { if (L.noteDirty) saveNote(); });

  document.addEventListener("keydown", e => {
    if (!e.ctrlKey) return;
    const k = e.key.toLowerCase();
    if (k === "s" && document.activeElement === $("#note-editor")) {
      // The reflex of a lifetime of text editors, and the note is saved on its
      // own anyway: swallow it rather than let the browser offer to save the page.
      e.preventDefault();
      return;
    }
    if (k === "d") {
      // Swallowed either way, so the browser keeps its bookmark dialog out of
      // the way of a shortcut this page claims.
      e.preventDefault();
      if (cfg.canSelect && !cfg.canSelect()) return;
      toggleNoteMode();
    }
  });

  function docTarget() {
    /* What the viewer is showing, in the terms the anchoring module needs.
    * Our own PDF renderer, or a piece of HTML it can measure and draw over,
    * null when there is nothing to link into
    */
    const s = L.source;
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

  const ANCHORS = makeAnchors(Object.assign({
    editor: () => $("#note-editor"),
    mirror: () => $("#note-mirror"),
    layer: () => $("#anchor-layer"),
    wrap: () => $(".note-wrap"),
    preview: () => $("#note-preview"),
    docPane: () => $("#panel-res"),
    docTarget,
    projectId: pid,
    noteId: () => L.noteId || "",
    // A standalone note has no document beside it, so nothing to link into.
    sourceId: () => (L.source ? L.source.id : ""),
    colors: () => ({}),
    canLink: () => true,
    inPreview: () => $("#note-editor").classList.contains("hidden"),
    ensureEditMode: async () => {
      if (!$("#note-editor").classList.contains("hidden")) return;
      L.noteMode = "edit";
      applyNoteMode();
    },
    openNote: async nid => nid === L.noteId,
    onChange: ({ lost }) => {
      $("#note-anchor-state").textContent = lost
        ? lost + (lost === 1 ? " link no longer resolves" : " links no longer resolve")
        : "";
    },
  }, cfg.anchors || {}));

  $("#btn-new-folder").addEventListener("click", () => TREE.newFolder());
  $("#btn-new-note").addEventListener("click", () => TREE.newNote());

  // Deselecting is the same path as selecting, so a pending note is saved and
  // the tree drops its highlight along with the viewer and the notes.
  if (closeBtn) closeBtn.addEventListener("click", () => TREE.setSelection(null));

  let _searchTimer;
  $("#search").addEventListener("input", () => {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(runSearch, 250);
  });

  async function runSearch() {
    /* Filters the tree from the search box.
    * "@ml" matches tags only, anything else the name or a tag — see
    * tree.js:queryPredicate. A page whose search box understands more than
    * that resolves the rest itself
    */
    const q = $("#search").value.trim();
    if (!q) return TREE.applyFilter(null);
    const local = TREE.queryPredicate(q);
    const pred = cfg.searchPredicate ? await cfg.searchPredicate(q, local) : local;
    TREE.applyFilter(pred || local);
  }

  function setupSplitter(handle, panel, storageKey, min, max) {
    /* Drag-to-resize one pane, its width remembered under storageKey
    */
    const saved = parseFloat(localStorage.getItem(storageKey));
    if (saved) panel.style.flex = `0 0 ${(saved * 100).toFixed(2)}%`;
    handle.addEventListener("mousedown", e => {
      e.preventDefault();
      document.body.classList.add("resizing");
      const onMove = ev => {
        const rect = (cfg.root ? cfg.root() : $("#main")).getBoundingClientRect();
        let f = (ev.clientX - rect.left) / rect.width;
        f = Math.max(min, Math.min(max, f));
        panel.style.flex = `0 0 ${(f * 100).toFixed(2)}%`;
        localStorage.setItem(storageKey, String(f));
      };
      const onUp = () => {
        document.body.classList.remove("resizing");
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        if (cfg.onNoteMode) cfg.onNoteMode(!$("#note-editor").classList.contains("hidden"));
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  }

  function setDirFolded(folded) {
    /* Folds or unfolds the tree pane, and remembers which
    */
    $("#panel-dir").classList.toggle("folded", folded);
    const btn = $("#btn-fold-dir");
    btn.setAttribute("aria-expanded", String(!folded));
    btn.title = folded ? "Show the library" : "Hide the library";
    localStorage.setItem(key("fold.dir"), folded ? "1" : "0");
  }

  $("#btn-fold-dir").addEventListener("click", () => {
    setDirFolded(!$("#panel-dir").classList.contains("folded"));
  });

  function start() {
    /* Call once the project id is known: the tree's fold state is stored per
    * project, so it cannot be read before that
    */
    TREE.setProject(pid());
    setupSplitter($('.splitter[data-split="dir"]'), $("#panel-dir"), key("w.dir"), 0.14, 0.72);
    setupSplitter($('.splitter[data-split="res"]'), $("#panel-res"), key("w.res"), 0.2, 0.8);
    setDirFolded(localStorage.getItem(key("fold.dir")) === "1");
    return refreshTree();
  }

  return {
    state: L, TREE, ANCHORS,
    start, refreshTree, select, selectSource, openRef, resetSelection,
    clearViewers, showCompiled, loadResource, recompileNote,
    adoptNote, flushNote, saveNote, setSaveState, applyNoteMode, setDirFolded,
    get source() { return L.source; },
    get noteId() { return L.noteId; },
    get sel() { return L.sel; },
  };
}
