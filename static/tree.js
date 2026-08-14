"use strict";

/* The library tree: folders, sources and standalone notes.
 *
 * The personal workspace and the project pages draw the same tree, so it lives
 * here once rather than twice. The page owns the data and decides what a click
 * means; this module owns the DOM, the fold state and drag-and-drop filing, and
 * talks to the page through `cfg`.
 *
 * cfg = {
 *   host,                    // the .dir-list element to draw into
 *   readOnly:      () => bool,
 *   showQuestions: () => bool,   // draw the quiz count on the unfolded line
 *   onSelect:      ref => …,     // {kind:"source"|"note"|"folder", id}
 *   onRemoveSource: id => …,     // the × on a source row (meaning differs per page)
 *   onDeleteNote:   id => …,     // the × on a standalone note row
 *   reload:        () => …,      // re-fetch the tree after a move
 *   empty:         "html shown when the library is empty",
 * }
 */

const TREE_ICON = { source: "📄", note: "📝", readme: "📖" };

function makeTree(cfg) {
  const T = {
    pid: "",
    folders: [],
    sources: [],
    notes: [],
    sel: null,
    collapsed: new Set(),
    expanded: new Set(),   // sources whose tag line is unfolded
    pred: null,            // active filter, or null for "show everything"
    drag: null,
  };

  const host = cfg.host;
  const readOnly = () => (cfg.readOnly ? cfg.readOnly() : false);

  function readSet(key) {
    /* Set stored under a localStorage key, empty when missing or corrupt
    */
    try { return new Set(JSON.parse(localStorage.getItem(key) || "[]")); }
    catch (_) { return new Set(); }
  }

  function saveSets() {
    /* Persists both fold sets, per project and per browser
    */
    localStorage.setItem("kndb.collapsed." + T.pid, JSON.stringify([...T.collapsed]));
    localStorage.setItem("kndb.expanded." + T.pid, JSON.stringify([...T.expanded]));
  }

  function setProject(pid) {
    /* Switches to another project, loading its fold state
    */
    T.pid = pid || "";
    T.collapsed = readSet("kndb.collapsed." + T.pid);
    T.expanded = readSet("kndb.expanded." + T.pid);
  }

  function setData(d) {
    /* Replaces the tree contents.
    * A selection on a source or a note that is gone is dropped
    */
    T.folders = d.folders || [];
    T.sources = d.sources || [];
    T.notes = d.notes || [];
    if (T.sel && T.sel.kind === "source" && !source(T.sel.id)) T.sel = null;
    if (T.sel && T.sel.kind === "note" && !note(T.sel.id)) T.sel = null;
  }

  const source = id => T.sources.find(s => s.id === id) || null;
  const note = id => T.notes.find(n => n.id === id) || null;

  function parentFolder(path) {
    return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  }

  function refFolder(ref) {
    /* Folder an item currently lives in, "" at the root
    */
    if (!ref) return "";
    if (ref.kind === "folder") return parentFolder(ref.id);
    const item = ref.kind === "note" ? note(ref.id) : source(ref.id);
    return (item && item.folder) || "";
  }

  function treeModel() {
    /* Folders, sources and notes as one nested structure, rooted at "".
    * The folder list already holds every ancestor, so a flat pass suffices
    */
    const nodes = new Map([["", { path: "", children: [], sources: [], notes: [] }]]);
    for (const path of T.folders) {
      nodes.set(path, { path, children: [], sources: [], notes: [] });
    }
    for (const path of T.folders) {
      (nodes.get(parentFolder(path)) || nodes.get("")).children.push(nodes.get(path));
    }
    for (const s of T.sources) (nodes.get(s.folder || "") || nodes.get("")).sources.push(s);
    for (const n of T.notes) (nodes.get(n.folder || "") || nodes.get("")).notes.push(n);
    return nodes.get("");
  }

  function applyFilter(pred) {
    /* Installs a row filter and redraws.
    * pred(kind, item) decides one row, null shows everything
    */
    T.pred = pred || null;
    render();
  }

  function visibleSet() {
    /* Keys ("f:path", "s:id", "n:id") of the rows the filter keeps, null when
    * there is no filter.
    * A folder survives if it matches or if anything under it did, so a hit
    * deep in the tree stays reachable
    */
    if (!T.pred) return null;
    const keep = new Set();
    const keepAncestors = path => {
      const parts = path ? path.split("/") : [];
      for (let i = 1; i <= parts.length; i++) keep.add("f:" + parts.slice(0, i).join("/"));
    };
    for (const s of T.sources) {
      if (!T.pred("source", s)) continue;
      keep.add("s:" + s.id);
      keepAncestors(s.folder || "");
    }
    for (const n of T.notes) {
      if (!T.pred("note", n)) continue;
      keep.add("n:" + n.id);
      keepAncestors(n.folder || "");
    }
    for (const f of T.folders) {
      if (T.pred("folder", { path: f, name: f.split("/").pop() })) keepAncestors(f);
    }
    return keep;
  }

  function queryPredicate(q) {
    /* Default filter: a plain word matches the name or a tag, "@word" matches
    * tags only.
    * Only sources carry tags. Null for an empty query
    */
    const f = String(q || "").trim().toLowerCase();
    if (!f) return null;
    const tagOnly = f.startsWith("@");
    const needle = tagOnly ? f.slice(1) : f;
    if (!needle) return null;
    return (kind, item) => {
      const tags = kind === "source" ? splitTags(item.tags).map(t => t.toLowerCase()) : [];
      if (tags.some(t => t.includes(needle))) return true;
      if (tagOnly) return false;
      const name = kind === "source" ? item.title : (item.name || item.path || "");
      return String(name).toLowerCase().includes(needle);
    };
  }

  function render() {
    /* Redraws the tree, or the empty and no-match placeholders
    */
    if (!T.sources.length && !T.notes.length && !T.folders.length) {
      host.innerHTML = cfg.empty || '<div class="empty">Nothing here yet.</div>';
      return;
    }
    const keep = visibleSet();
    host.innerHTML = "";
    drawNode(treeModel(), 0, keep);
    if (keep && !host.children.length) {
      host.innerHTML = '<div class="empty">Nothing matches that filter.</div>';
    }
  }

  function drawNode(node, depth, keep) {
    /* Draws one node's folders, then its notes, then its sources.
    * Rows go flat into the host, indented by depth, and open folders recurse
    */
    const pad = 6 + depth * 14 + "px";

    for (const child of node.children.sort((a, b) => a.path.localeCompare(b.path))) {
      if (keep && !keep.has("f:" + child.path)) continue;
      // While filtering, folders are forced open: a hit the user cannot see is
      // the same as no hit at all.
      const open = !!keep || !T.collapsed.has(child.path);
      const row = document.createElement("div");
      row.className = "tree-row folder" + (isSel("folder", child.path) ? " active" : "");
      row.dataset.folder = child.path;
      row.draggable = !readOnly();
      row.style.paddingLeft = pad;
      row.innerHTML =
        '<span class="twisty">' + (open ? "▾" : "▸") + "</span>" +
        '<span class="tree-name">' + escapeHtml(child.path.split("/").pop()) + "</span>" +
        '<button class="del" data-delfolder="' + escapeHtml(child.path)
        + '" title="Remove folder (keeps its contents)">×</button>';
      host.appendChild(row);
      if (open) drawNode(child, depth + 1, keep);
    }

    for (const n of node.notes.sort((a, b) => a.name.localeCompare(b.name))) {
      if (keep && !keep.has("n:" + n.id)) continue;
      const row = document.createElement("div");
      row.className = "tree-row note" + (isSel("note", n.id) ? " active" : "");
      row.dataset.note = n.id;
      row.draggable = !readOnly();
      row.style.paddingLeft = pad;
      // A standalone note has nothing to unfold, so it keeps a plain row.
      row.innerHTML =
        '<span class="twisty"></span>' +
        '<span class="tree-icon">'
        + (n.name.toLowerCase() === "readme" ? TREE_ICON.readme : TREE_ICON.note) + "</span>" +
        '<span class="tree-name" title="' + escapeHtml(n.name) + '">'
        + escapeHtml(n.name) + "</span>" +
        (canDeleteNote(n) ? '<button class="del" data-delnote="' + n.id
          + '" title="Delete this note page">×</button>' : "");
      host.appendChild(row);
    }

    for (const s of node.sources) {
      if (keep && !keep.has("s:" + s.id)) continue;
      const open = T.expanded.has(s.id);
      const row = document.createElement("div");
      row.className = "tree-row source" + (open ? " unfolded" : "")
        + (isSel("source", s.id) ? " active" : "");
      row.dataset.id = s.id;
      row.draggable = !readOnly();
      row.style.paddingLeft = pad;
      row.innerHTML =
        '<span class="twisty">' + (open ? "▾" : "▸") + "</span>" +
        '<span class="tree-icon">' + TREE_ICON.source + "</span>" +
        '<span class="tree-name" title="' + escapeHtml(s.title) + '">'
        + escapeHtml(s.title) + "</span>" +
        typePill(s.source_type) +
        '<button class="del" data-remove="' + s.id + '" title="'
        + escapeHtml(cfg.removeTitle || "Remove") + '">×</button>';
      host.appendChild(row);
      if (open) host.appendChild(detailRow(s, depth));
    }
  }

  function detailRow(s, depth) {
    /* Unfolded half of a source row: the tags the search box matches, and the
    * quiz count when the page asks for it
    */
    const el = document.createElement("div");
    el.className = "tree-detail" + (isSel("source", s.id) ? " active" : "");
    el.dataset.for = s.id;
    el.style.paddingLeft = 6 + depth * 14 + 20 + "px";
    const tags = splitTags(s.tags)
      .map(t => '<span class="chip">@' + escapeHtml(t) + "</span>").join("");
    const q = (cfg.showQuestions && cfg.showQuestions())
      ? '<span class="qcount" title="questions in quiz">' + (s.num_questions || 0) + "Q</span>"
      : "";
    el.innerHTML = (tags || '<span class="muted tree-none">no tags</span>') + q;
    return el;
  }

  function isSel(kind, id) {
    return T.sel && T.sel.kind === kind && T.sel.id === id;
  }

  async function setSelection(ref) {
    /* Selects programmatically, down the same path as a click, so the page has
    * one way to react to a selection
    */
    T.sel = ref;
    render();
    if (cfg.onSelect) await cfg.onSelect(ref);
  }

  function canDeleteNote(page) {
    return !readOnly() && cfg.onDeleteNote && noteDeletable(page, cfg.role ? cfg.role() : "");
  }

  host.addEventListener("click", async e => {
    const rm = e.target.closest("[data-remove]");
    if (rm) { e.stopPropagation(); return cfg.onRemoveSource && cfg.onRemoveSource(rm.dataset.remove); }
    const dn = e.target.closest("[data-delnote]");
    if (dn) { e.stopPropagation(); return cfg.onDeleteNote && cfg.onDeleteNote(dn.dataset.delnote); }
    const df = e.target.closest("[data-delfolder]");
    if (df) { e.stopPropagation(); return deleteFolder(df.dataset.delfolder); }

    // The tag line reads as part of the row above it, so it selects like one.
    const detail = e.target.closest(".tree-detail");
    if (detail) return setSelection({ kind: "source", id: detail.dataset.for });

    const row = e.target.closest(".tree-row");
    if (!row) return;
    const ref = rowRef(row);
    if (!ref) return;

    if (e.target.closest(".twisty")) {
      if (ref.kind === "folder") {
        T.collapsed.has(ref.id) ? T.collapsed.delete(ref.id) : T.collapsed.add(ref.id);
      } else if (ref.kind === "source") {
        T.expanded.has(ref.id) ? T.expanded.delete(ref.id) : T.expanded.add(ref.id);
      } else {
        return;
      }
      saveSets();
      render();
      return;
    }
    await setSelection(ref);
  });

  /* A drop always resolves to a folder *path*, never to a row: dropping onto a
   * folder files into it, dropping onto a source or a note files beside it, and
   * dropping on the blank space under the tree files at the root. That keeps
   * the root reachable even when the tree fills the panel. */

  let _dropHint = null;
  let _expandTimer = null;
  let _expandPath = null;

  function rowRef(row) {
    /* {kind, id} of a tree row, null for anything else
    */
    if (!row) return null;
    if (row.classList.contains("folder")) return { kind: "folder", id: row.dataset.folder };
    if (row.classList.contains("note")) return { kind: "note", id: row.dataset.note };
    if (row.classList.contains("source")) return { kind: "source", id: row.dataset.id };
    return null;
  }

  function dropFolderFor(target) {
    /* Folder path a drop on this node files into, "" for the root
    */
    if (!target || !target.closest) return "";
    // An unfolded tag line belongs to the row above it, not to the root.
    const detail = target.closest(".tree-detail");
    if (detail) return (source(detail.dataset.for) || {}).folder || "";
    const ref = rowRef(target.closest(".tree-row"));
    if (!ref) return "";
    return ref.kind === "folder" ? ref.id : refFolder(ref);
  }

  function canDrop(ref, dest) {
    /* Whether the drag may land here.
    * A folder never moves into itself or into its own subtree
    */
    if (!ref || readOnly()) return false;
    if (refFolder(ref) === dest) return false;             // already filed there
    if (ref.kind !== "folder") return true;
    return dest !== ref.id && !(dest + "/").startsWith(ref.id + "/");  // not into itself
  }

  function folderRow(path) {
    return $$(".tree-row.folder", host).find(r => r.dataset.folder === path) || null;
  }

  function showDropHint(dest) {
    /* Marks the drop target, the whole list when it is the root
    */
    const el = dest ? folderRow(dest) : host;
    if (el === _dropHint) return;
    clearDropHint();
    if (!el) return;
    el.classList.add(dest ? "drop-into" : "drop-root");
    _dropHint = el;
  }

  function clearDropHint() {
    if (_dropHint) _dropHint.classList.remove("drop-into", "drop-root");
    _dropHint = null;
  }

  function queueExpand(dest) {
    /* Opens a collapsed folder hovered mid-drag, so a nested target can be
    * reached without dropping and starting over
    */
    if (dest === _expandPath) return;
    clearTimeout(_expandTimer);
    _expandPath = dest;
    if (!dest || !T.collapsed.has(dest)) return;
    _expandTimer = setTimeout(() => {
      T.collapsed.delete(dest);
      saveSets();
      render();
      _dropHint = null;        // the old row is gone
      showDropHint(dest);
    }, 600);
  }

  host.addEventListener("dragstart", e => {
    const row = e.target.closest(".tree-row");
    const ref = rowRef(row);
    if (!ref || readOnly()) { e.preventDefault(); return; }
    T.drag = ref;
    row.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", ref.kind + ":" + ref.id);  // Firefox needs a payload
  });

  host.addEventListener("dragend", () => {
    T.drag = null;
    clearTimeout(_expandTimer);
    _expandPath = null;
    clearDropHint();
    $$(".dragging", host).forEach(el => el.classList.remove("dragging"));
  });

  host.addEventListener("dragover", e => {
    if (!T.drag) return;
    const dest = dropFolderFor(e.target);
    if (!canDrop(T.drag, dest)) { clearDropHint(); return; }
    e.preventDefault();                      // preventDefault == "I accept this"
    e.dataTransfer.dropEffect = "move";
    showDropHint(dest);
    queueExpand(dest);
  });

  host.addEventListener("dragleave", e => {
    if (e.target === host) clearDropHint();
  });

  host.addEventListener("drop", async e => {
    const ref = T.drag;
    const dest = dropFolderFor(e.target);
    clearTimeout(_expandTimer);
    clearDropHint();
    T.drag = null;
    if (!canDrop(ref, dest)) return;
    e.preventDefault();
    await fileInto(ref, dest);
  });

  async function fileInto(ref, dest) {
    /* Files an item into a folder on the server, then reloads the tree.
    * A folder landing on a name already taken there asks to merge first
    */
    try {
      if (ref.kind === "source") {
        await api("/api/projects/" + T.pid + "/sources/" + ref.id + "/folder",
          { method: "POST", body: { folder: dest } });
      } else if (ref.kind === "note") {
        await api("/api/notes/" + ref.id, { method: "PATCH", body: { folder: dest } });
      } else {
        const name = ref.id.split("/").pop();
        const target = dest ? dest + "/" + name : name;
        if (T.folders.includes(target) && !await confirmModal({
          title: "Merge folders?",
          body: 'There is already a folder named "' + name + '" there.',
          hint: "Everything in the folder you dragged joins it. Nothing is deleted.",
          confirm: "Merge",
        })) return;
        await api("/api/projects/" + T.pid + "/folders",
          { method: "POST", body: { path: ref.id, new_path: target } });
        remapPaths(ref.id, target);
      }
      await cfg.reload();
    } catch (err) {
      toast("Could not move that: " + err.message, "err", 6000);
    }
  }

  function remapPaths(from, to) {
    /* Follows a folder rename: it takes its descendants with it, so the fold
    * state and the current selection move too
    */
    const move = p => (p === from ? to
      : p.startsWith(from + "/") ? to + p.slice(from.length) : null);
    T.collapsed = new Set([...T.collapsed].map(p => move(p) || p));
    saveSets();
    if (T.sel && T.sel.kind === "folder") {
      const dest = move(T.sel.id);
      if (dest) T.sel = { kind: "folder", id: dest };
    }
  }

  async function newFolder() {
    /* Asks for a path and creates the folder, prefilled under the selected one
    */
    const base = (T.sel && T.sel.kind === "folder") ? T.sel.id + "/" : "";
    const path = await askModal({
      title: "New folder",
      label: "Folder path",
      value: base,
      selectAll: false,
      placeholder: "reading/week 1",
      submit: "Create folder",
      hint: "Slashes nest: \"reading/week 1\" creates both levels. "
          + "Drag sources onto a folder to file them.",
    });
    if (!path) return;
    try {
      await api("/api/projects/" + T.pid + "/folders", { method: "POST", body: { path } });
      await cfg.reload();
    } catch (e) { toast(e.message, "err"); }
  }

  async function newNote() {
    /* Creates a standalone note in the selected folder, then selects it
    */
    const folder = (T.sel && T.sel.kind === "folder") ? T.sel.id : "";
    const name = await askModal({
      title: "New standalone note",
      label: "Note name",
      value: "Note",
      placeholder: "README",
      submit: "Create note",
      hint: "Lands in " + (folder ? '"' + folder + '"' : "the root")
          + ". Name it README and it is shown whenever that folder is selected.",
    });
    if (!name) return;
    try {
      const d = await api("/api/projects/" + T.pid + "/notes",
        { method: "POST", body: { name, folder } });
      await cfg.reload();
      await setSelection({ kind: "note", id: d.note.id });
    } catch (e) { toast(e.message, "err"); }
  }

  async function deleteFolder(path) {
    /* Removes a folder once confirmed, its contents moving up one level
    */
    if (!await confirmModal({
      title: "Remove folder",
      body: 'Remove the folder "' + path + '"?',
      hint: "Everything inside it moves up one level — nothing is deleted.",
      confirm: "Remove folder",
    })) return;
    try {
      await api("/api/projects/" + T.pid + "/folders?path=" + encodeURIComponent(path),
        { method: "DELETE" });
      if (isSel("folder", path)) T.sel = null;
      await cfg.reload();
    } catch (e) { toast(e.message, "err"); }
  }

  return {
    state: T,
    setProject, setData, render, applyFilter, queryPredicate, setSelection,
    newFolder, newNote,
    get sel() { return T.sel; },
    set sel(v) { T.sel = v; },
    source, note, refFolder,
  };
}
