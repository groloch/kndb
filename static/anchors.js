"use strict";

/* Grounding note sentences in the document.
 *
 * An anchor is an annotation, never an edit: nothing here writes to the note's
 * content. Every link is drawn from a quote locator resolved on each render,
 * which is why a sentence keeps its link through edits above it and why the
 * blame gutter never sees any of this. See plan-anchors.md.
 *
 * A link can be started from either end — select a sentence and pick a
 * passage, or select a passage and pick a sentence — and followed from either
 * end, in the editor, in the rendered note, and in the document.
 *
 * The editor is a <textarea>, so ranges cannot be styled and a floating menu
 * cannot be positioned by getClientRects(). Both are measured through the same
 * hidden mirror the blame gutter already uses to line its stripes up with
 * wrapped lines. Every other surface is ordinary HTML and goes through
 * makeTextSurface (surface.js).
 */

function makeAnchors(cfg) {

  // cfg: {editor, mirror, layer, wrap, preview, docPane, docTarget, noteId,
  //       sourceId, projectId, colors, canLink, openNote, ensureEditMode,
  //       inPreview, onChange}

  const S = {
    mine: [],        // anchors on the page being edited
    all: [],         // every anchor on this document, by anyone
    marks: [],       // resolved in the editor: {a, start, end, boxes, els}
    lost: 0,         // quoted text is gone from the note
    pending: null,   // a half-made link, from one end or the other
  };

  let docSurface = null;    // markdown / HTML documents (PDFs use PDFView)
  let preview = null;       // the rendered note
  let menu = null, docMenu = null, chip = null, chooser = null;
  // An HTML source lives in its own frame, and events inside a frame never
  // reach this document. Every listener that watches for a selection or for
  // Escape has to be attached there as well, or linking simply never fires.
  let frameDoc = null;

  const watched = () => (frameDoc ? [document, frameDoc] : [document]);

  const isPdfDoc = () => (cfg.docTarget() || {}).kind === "pdf";

  /* ---------- measuring the textarea ---------- */

  /** Rectangles for each range, in unscrolled editor coordinates. Ranges must
   *  be sorted and must not overlap. */
  function measureRanges(text, ranges) {
    const ta = cfg.editor(), mirror = cfg.mirror();
    const cs = getComputedStyle(ta);
    for (const prop of ["fontFamily", "fontSize", "fontWeight", "lineHeight",
                        "letterSpacing", "whiteSpace", "wordBreak",
                        "paddingLeft", "paddingRight", "paddingTop",
                        "textIndent"]) {
      mirror.style[prop] = cs[prop];
    }
    mirror.style.width = ta.clientWidth + "px";
    mirror.innerHTML = "";

    const spans = [];
    let at = 0;
    for (const r of ranges) {
      if (r.start > at) {
        mirror.appendChild(document.createTextNode(text.slice(at, r.start)));
      }
      const sp = document.createElement("span");
      sp.textContent = text.slice(r.start, r.end);
      mirror.appendChild(sp);
      spans.push(sp);
      at = r.end;
    }
    mirror.appendChild(document.createTextNode(text.slice(at) + "\n"));

    const box = mirror.getBoundingClientRect();
    return spans.map(sp => [...sp.getClientRects()]
      .filter(r => r.width > 0.1 && r.height > 0.1)
      .map(r => ({ x: r.left - box.left, y: r.top - box.top,
                   w: r.width, h: r.height })));
  }

  /* ---------- drawing over the editor ---------- */

  function draw() {
    const layer = cfg.layer(), ta = cfg.editor();
    layer.innerHTML = "";
    S.marks = [];
    S.lost = 0;

    // Resolving needs no layout, so it happens whichever mode we are in — the
    // textarea keeps its value while hidden. Only measuring needs the editor
    // on screen, and the rendered note is drawn either way.
    const text = ta.value;
    const hits = [];
    for (const a of S.mine) {
      const at = resolveLocator(text, a.note_loc);
      if (at) hits.push({ a, start: at.start, end: at.end });
      else S.lost++;
    }
    drawPreview();
    if (!hits.length || ta.classList.contains("hidden")) return report();
    hits.sort((x, y) => x.start - y.start);

    const clean = [];
    for (const h of hits) {
      if (!clean.length || h.start >= clean[clean.length - 1].end) clean.push(h);
    }

    const track = document.createElement("div");
    track.className = "anchor-track";
    const rects = measureRanges(text, clean);

    clean.forEach((h, i) => {
      const color = blameColor(cfg.colors(), h.a.created_by);
      const boxes = rects[i] || [];
      const els = [];
      for (const b of boxes) {
        const d = document.createElement("div");
        d.className = "anchor-mark";
        d.style.left = b.x + "px";
        d.style.top = b.y + "px";
        d.style.width = b.w + "px";
        d.style.height = b.h + "px";
        d.style.borderBottomColor = color;
        track.appendChild(d);
        els.push(d);
      }
      if (boxes.length) {
        const dot = document.createElement("button");
        dot.type = "button";
        dot.className = "anchor-dot";
        dot.dataset.anchor = h.a.id;
        dot.style.top = (boxes[0].y + 3) + "px";
        dot.style.background = color;
        dot.title = anchorTitle(h.a);
        track.appendChild(dot);
        els.push(dot);
      }
      S.marks.push({ a: h.a, start: h.start, end: h.end, boxes, els });
    });

    layer.appendChild(track);
    syncScroll();
    report();
  }

  /** The rendered note carries the same links, in the same colours. */
  function drawPreview() {
    const host = cfg.preview && cfg.preview();
    if (!host) return;
    if (!preview) {
      preview = makeTextSurface({
        root: () => cfg.preview(),
        onClick: (hits, e) => choose(hits, "toDoc", e),
      });
      preview.attach();
    }
    preview.show(S.mine.map(a => ({
      id: a.id, loc: a.note_loc, color: blameColor(cfg.colors(), a.created_by),
    })));
  }

  function anchorTitle(a) {
    const q = (a.doc_loc && a.doc_loc.exact || "").replace(/\s+/g, " ").trim();
    const page = a.doc_loc && a.doc_loc.page;
    return "→ " + (page ? "page " + page : "the document")
      + (q ? ": " + q.slice(0, 140) : "")
      + "\nclick to follow · alt-click to unlink";
  }

  function report() {
    if (cfg.onChange) cfg.onChange({ count: S.marks.length, lost: S.lost });
  }

  function syncScroll() {
    const track = cfg.layer().querySelector(".anchor-track");
    if (track) {
      track.style.transform = "translateY(" + -cfg.editor().scrollTop + "px)";
    }
    placeMenu();
  }

  /* ---------- the document surface ---------- */

  /** Rebuild the surface for whatever the viewer is showing. PDFs are drawn by
   *  PDFView; everything else is HTML and goes through makeTextSurface. */
  function bindDocSurface() {
    if (docSurface) { docSurface.detach(); docSurface = null; }
    if (frameDoc) {
      frameDoc.removeEventListener("mouseup", onSelectInDoc, true);
      frameDoc = null;
    }
    const t = cfg.docTarget();
    if (!t || t.kind !== "html") return;
    const owner = t.root.ownerDocument;
    if (owner && owner !== document) {
      frameDoc = owner;
      frameDoc.addEventListener("mouseup", onSelectInDoc, true);
    }
    docSurface = makeTextSurface({
      root: () => t.root,
      scroller: () => t.scroller || t.root,
      win: () => t.win || window,
      onClick: (hits, e) => choose(hits, "toNote", e),
    });
    docSurface.attach();
  }

  function showDocAnchors() {
    const list = S.all.map(a => ({
      id: a.id, loc: a.doc_loc, doc_loc: a.doc_loc, note_id: a.note_id,
      color: blameColor(cfg.colors(), a.created_by),
    }));
    if (isPdfDoc()) PDFView.showAnchors(list);
    else if (docSurface) docSurface.show(list);
  }

  function captureDoc() {
    if (isPdfDoc()) return PDFView.captureSelection();
    return docSurface ? docSurface.capture() : null;
  }

  function revealInDoc(a) {
    if (isPdfDoc()) return PDFView.reveal(a.doc_loc, a.id);
    return docSurface ? docSurface.reveal(a.id) : false;
  }

  /* ---------- menus ---------- */

  function ensureMenu() {
    if (menu) return menu;
    menu = document.createElement("div");
    menu.className = "anchor-menu hidden";
    menu.innerHTML = '<button type="button" class="btn small">Link to source</button>';
    menu.addEventListener("mousedown", e => e.preventDefault());
    menu.querySelector("button").addEventListener("click", startFromNote);
    cfg.wrap().appendChild(menu);
    return menu;
  }

  function showMenu() {
    const ta = cfg.editor();
    const a = ta.selectionStart, b = ta.selectionEnd;
    if (S.pending || b <= a || !cfg.canLink() || !canCapture()) return hideMenu();
    if (!ta.value.slice(a, b).trim()) return hideMenu();
    const m = ensureMenu();
    m.dataset.start = String(a);
    m.dataset.end = String(b);
    m.classList.remove("hidden");
    placeMenu();
  }

  function placeMenu() {
    if (!menu || menu.classList.contains("hidden")) return;
    const ta = cfg.editor();
    const a = Number(menu.dataset.start), b = Number(menu.dataset.end);
    if (ta.selectionStart !== a || ta.selectionEnd !== b) return hideMenu();
    const boxes = measureRanges(ta.value, [{ start: a, end: b }])[0] || [];
    const last = boxes[boxes.length - 1];
    if (!last) return hideMenu();
    const y = last.y + last.h - ta.scrollTop;
    menu.style.left = Math.max(4, Math.min(last.x + last.w - 40,
                                           ta.clientWidth - 130)) + "px";
    menu.style.top = Math.max(0, Math.min(y + 4, ta.clientHeight - 34)) + "px";
  }

  function hideMenu() { if (menu) menu.classList.add("hidden"); }

  /** The mirror of the editor's menu, for a passage picked in the document.
   *  Placed at the pointer, because a PDF page and an iframe do not share a
   *  coordinate system with this page. */
  function ensureDocMenu() {
    if (docMenu) return docMenu;
    docMenu = document.createElement("div");
    docMenu.className = "anchor-menu floating hidden";
    docMenu.innerHTML = '<button type="button" class="btn small">Link to note</button>';
    docMenu.addEventListener("mousedown", e => e.preventDefault());
    docMenu.querySelector("button").addEventListener("click", startFromDoc);
    document.body.appendChild(docMenu);
    return docMenu;
  }

  /** A selection made in the document offers the link the other way round. */
  function onSelectInDoc(e) {
    if (S.pending || !cfg.canLink() || !canCapture()) return hideDocMenu();
    if (!inDocPane(e.target)) return hideDocMenu();
    const [x, y] = toHostPoint(e);
    setTimeout(() => {
      if (S.pending) return;
      if (captureDoc()) showDocMenu(x, y);
      else hideDocMenu();
    }, 0);
  }

  /** Pointer coordinates in this page's terms: an event raised inside the
   *  source's frame is measured against that frame, not against the window. */
  function toHostPoint(e) {
    const d = e && e.target && e.target.ownerDocument;
    const frame = d && d !== document && d.defaultView && d.defaultView.frameElement;
    if (!frame) return [(e && e.clientX) || 0, (e && e.clientY) || 0];
    const r = frame.getBoundingClientRect();
    return [e.clientX + r.left, e.clientY + r.top];
  }

  function showDocMenu(x, y) {
    const m = ensureDocMenu();
    m.classList.remove("hidden");
    m.style.left = Math.min(x + 6, window.innerWidth - 130) + "px";
    m.style.top = Math.min(y + 8, window.innerHeight - 40) + "px";
  }

  function hideDocMenu() { if (docMenu) docMenu.classList.add("hidden"); }

  function canCapture() {
    const t = cfg.docTarget();
    if (!t) return false;
    if (t.kind === "pdf") return PDFView.isOpen;
    return !!docSurface;
  }

  /* ---------- making a link, from either end ---------- */

  function startFromNote() {
    const ta = cfg.editor();
    const start = Number(menu.dataset.start), end = Number(menu.dataset.end);
    hideMenu();
    ta.readOnly = true;
    S.pending = {
      dir: "fromNote", noteId: cfg.noteId(), sourceId: cfg.sourceId(),
      note_loc: quoteLocator(ta.value, start, end),
    };
    document.body.classList.add("linking");
    drawPending(start, end);
    listenDuringLink();
  }

  async function startFromDoc() {
    hideDocMenu();
    const doc_loc = captureDoc();
    if (!doc_loc) return;
    if (!cfg.noteId()) return toast("Open a note page to link into first", "warn");
    await cfg.ensureEditMode();
    S.pending = {
      dir: "fromDoc", noteId: cfg.noteId(), sourceId: cfg.sourceId(), doc_loc,
    };
    document.body.classList.add("linking-note");
    cfg.editor().focus();
    listenDuringLink();
  }

  function listenDuringLink() {
    for (const d of watched()) {
      d.addEventListener("keydown", onKeyDuringLink, true);
      d.addEventListener("mouseup", onPickDuringLink, true);
    }
  }

  function drawPending(start, end) {
    const ta = cfg.editor();
    const boxes = measureRanges(ta.value, [{ start, end }])[0] || [];
    const track = document.createElement("div");
    track.className = "anchor-track";
    for (const b of boxes) {
      const d = document.createElement("div");
      d.className = "anchor-pending";
      d.style.left = b.x + "px";
      d.style.top = b.y + "px";
      d.style.width = b.w + "px";
      d.style.height = b.h + "px";
      track.appendChild(d);
    }
    cfg.layer().appendChild(track);
    syncScroll();
  }

  function onKeyDuringLink(e) {
    if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation(); leaveLinkMode();
    }
  }

  function onPickDuringLink(e) {
    if (!S.pending) return;
    if (S.pending.dir === "fromNote") {
      if (!inDocPane(e.target)) return;
      setTimeout(() => {
        if (!S.pending) return;
        const doc_loc = captureDoc();
        if (doc_loc) commit(S.pending.note_loc, doc_loc);
      }, 0);
      return;
    }
    // fromDoc: the sentence is picked in the editor
    const ta = cfg.editor();
    if (e.target !== ta) return;
    setTimeout(() => {
      if (!S.pending) return;
      const a = ta.selectionStart, b = ta.selectionEnd;
      if (b <= a || !ta.value.slice(a, b).trim()) return;
      commit(quoteLocator(ta.value, a, b), S.pending.doc_loc);
    }, 0);
  }

  function inDocPane(node) {
    const pane = cfg.docPane();
    if (pane && pane.contains(node)) return true;
    const t = cfg.docTarget();
    // A node inside the HTML source's frame belongs to its own document.
    return !!(t && t.root && t.root.ownerDocument === (node.ownerDocument || null));
  }

  function leaveLinkMode() {
    if (!S.pending) return;
    S.pending = null;
    cfg.editor().readOnly = !cfg.canLink();
    document.body.classList.remove("linking", "linking-note");
    for (const d of watched()) {
      d.removeEventListener("keydown", onKeyDuringLink, true);
      d.removeEventListener("mouseup", onPickDuringLink, true);
    }
    draw();
  }

  async function commit(note_loc, doc_loc) {
    const p = S.pending;
    leaveLinkMode();
    try {
      // Re-linking a sentence replaces its link rather than adding a second
      // one: two links on the same words leave the reader guessing which
      // highlight belongs to what.
      await dropOverlapping(note_loc);
      const d = await api("/api/notes/" + p.noteId + "/anchors", {
        method: "POST",
        body: { source_id: p.sourceId, note_loc, doc_loc },
      });
      clearSelections();
      await load();
      const a = d.anchor;
      revealInDoc(a);
      if (preview) preview.flash(a.id);
      offerUndo(a);
    } catch (err) {
      toast("Could not link: " + err.message, "err");
    }
  }

  /** Delete any link whose sentence overlaps the one being linked now. */
  async function dropOverlapping(note_loc) {
    const text = cfg.editor().value;
    const at = resolveLocator(text, note_loc);
    if (!at) return;
    const doomed = [];
    for (const a of S.mine) {
      const other = resolveLocator(text, a.note_loc);
      if (!other) continue;
      if (at.end > other.start && at.start < other.end) doomed.push(a.id);
    }
    for (const id of doomed) {
      try { await api("/api/anchor/" + id, { method: "DELETE" }); } catch (_) {}
    }
  }

  function clearSelections() {
    window.getSelection().removeAllRanges();
    const t = cfg.docTarget();
    if (t && t.win && t.win !== window) {
      try { t.win.getSelection().removeAllRanges(); } catch (_) {}
    }
  }

  /* ---------- undo ---------- */

  function offerUndo(anchor) {
    if (chip) chip.remove();
    chip = document.createElement("div");
    chip.className = "anchor-chip";
    chip.innerHTML = '<span></span><button type="button" class="btn small">Undo</button>';
    chip.querySelector("span").textContent = anchor.doc_loc.page
      ? "Linked to page " + anchor.doc_loc.page
      : "Linked to the document";
    chip.querySelector("button").addEventListener("click", async () => {
      chip.remove(); chip = null;
      await unlink(anchor.id, true);
    });
    cfg.wrap().appendChild(chip);
    const mine = chip;
    setTimeout(() => { if (chip === mine) { chip.remove(); chip = null; } }, 8000);
  }

  /** A sentence deleted from the note takes its link with it.
   *
   *  Called after a save, never on a keystroke: mid-edit a quote is often
   *  momentarily unfindable — selecting a sentence and retyping it passes
   *  through a state where it is gone — and a link must not die because
   *  someone was halfway through rewording. A link on someone else's page that
   *  we are not allowed to delete simply stays, and the footer keeps counting
   *  it as unresolved.
   */
  async function pruneLost() {
    if (S.pending || !S.mine.length || !cfg.noteId()) return;
    const text = cfg.editor().value;
    const gone = S.mine.filter(a => !resolveLocator(text, a.note_loc));
    if (!gone.length) return;
    let removed = 0;
    for (const a of gone) {
      try {
        await api("/api/anchor/" + a.id, { method: "DELETE" });
        removed++;
      } catch (_) { /* not ours to remove */ }
    }
    if (removed) await load();
  }

  async function unlink(aid, quiet) {
    try {
      await api("/api/anchor/" + aid, { method: "DELETE" });
      await load();
      if (!quiet) toast("Link removed", "ok");
    } catch (err) {
      toast("Could not remove the link: " + err.message, "err");
    }
  }

  /* ---------- following ---------- */

  function markAt(e) {
    const ta = cfg.editor();
    const box = ta.getBoundingClientRect();
    const x = e.clientX - box.left;
    const y = e.clientY - box.top + ta.scrollTop;
    for (const m of S.marks) {
      for (const b of m.boxes) {
        if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) return m;
      }
    }
    return null;
  }

  /** One highlight can sit under another. Rather than guess, ask. */
  function choose(hits, where, ev) {
    hideChooser();
    const ids = hits.map(h => h.id || (h.a && h.a.id)).filter(Boolean);
    const list = ids.map(id => S.all.find(a => a.id === id)).filter(Boolean);
    if (!list.length) return;
    if (list.length === 1) return go(list[0], where);

    chooser = document.createElement("div");
    chooser.className = "anchor-chooser";
    chooser.innerHTML = '<div class="anchor-chooser-head">' + list.length
      + " links here</div>";
    for (const a of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "anchor-chooser-item";
      const quote = where === "toNote"
        ? (a.note_loc.exact || "") : (a.doc_loc.exact || "");
      b.innerHTML = '<span class="dot" style="background:'
        + blameColor(cfg.colors(), a.created_by) + '"></span>'
        + '<span class="anchor-chooser-text"></span>';
      b.querySelector(".anchor-chooser-text").textContent =
        quote.replace(/\s+/g, " ").trim().slice(0, 90) || "(no text)";
      b.title = "by " + a.created_by;
      b.addEventListener("mouseenter", () => preflash(a, where));
      b.addEventListener("click", () => { hideChooser(); go(a, where); });
      chooser.appendChild(b);
    }
    document.body.appendChild(chooser);
    const [px, py] = toHostPoint(ev);
    const x = px || window.innerWidth / 2;
    const y = py || window.innerHeight / 2;
    chooser.style.left = Math.min(x + 6, window.innerWidth - 280) + "px";
    chooser.style.top = Math.min(y + 8, window.innerHeight - 40) + "px";
    setTimeout(() => document.addEventListener("mousedown", hideChooser,
                                               { once: true }), 0);
  }

  /** Hovering an entry in the chooser shows which one it means. */
  function preflash(a, where) {
    if (where === "toNote") {
      const m = S.marks.find(x => x.a.id === a.id);
      if (m) {
        m.els.forEach(el => el.classList.add("flash"));
        setTimeout(() => m.els.forEach(el => el.classList.remove("flash")), 700);
      }
      if (preview) preview.flash(a.id);
    } else if (isPdfDoc()) {
      PDFView.reveal(a.doc_loc, a.id);
    } else if (docSurface) {
      docSurface.reveal(a.id);
    }
  }

  function hideChooser() {
    if (chooser) { chooser.remove(); chooser = null; }
  }

  function go(a, where) {
    if (where === "toDoc") return followToDoc(a);
    return followToNote(a);
  }

  function followToDoc(a) {
    if (!revealInDoc(a)) {
      toast("That passage is in a document that is not open", "warn");
    }
  }

  /** A passage was clicked in the document: land on its sentence, wherever it
   *  lives — another note page, the editor, or the rendered note. */
  async function followToNote(a) {
    if (a.note_id !== cfg.noteId()) {
      const ok = await cfg.openNote(a.note_id);
      if (!ok) {
        return toast("That link belongs to a note page you cannot open", "warn");
      }
    }
    if (preview && cfg.inPreview && cfg.inPreview()) {
      if (preview.reveal(a.id)) return;
    }
    const m = S.marks.find(x => x.a.id === a.id);
    if (!m) {
      return toast("That link points at text that is no longer in the note",
                   "warn");
    }
    const ta = cfg.editor();
    const y = m.boxes[0].y;
    if (y < ta.scrollTop || y > ta.scrollTop + ta.clientHeight - 40) {
      ta.scrollTop = Math.max(0, y - ta.clientHeight / 3);
      syncScroll();
    }
    m.els.forEach(el => el.classList.add("flash"));
    setTimeout(() => m.els.forEach(el => el.classList.remove("flash")), 1600);
  }

  /* ---------- loading ---------- */

  async function load() {
    const nid = cfg.noteId(), sid = cfg.sourceId(), pid = cfg.projectId();
    bindDocSurface();
    if (!nid || !sid) {
      S.mine = []; S.all = [];
      draw();
      showDocAnchors();
      return;
    }
    try {
      const [mine, all] = await Promise.all([
        api("/api/notes/" + nid + "/anchors"),
        api("/api/projects/" + pid + "/sources/" + sid + "/anchors"),
      ]);
      S.mine = mine.anchors || [];
      S.all = all.anchors || [];
    } catch (_) {
      S.mine = []; S.all = [];
    }
    draw();
    showDocAnchors();
  }

  function clear() {
    S.mine = []; S.all = []; S.marks = []; S.lost = 0;
    leaveLinkMode();
    cfg.layer().innerHTML = "";
    if (preview) preview.clear();
    if (docSurface) { docSurface.clear(); docSurface.detach(); docSurface = null; }
    hideMenu(); hideDocMenu(); hideChooser();
    report();
  }

  /* ---------- wiring ---------- */

  const ta = cfg.editor();
  ta.addEventListener("mouseup", () => setTimeout(showMenu, 0));
  ta.addEventListener("keyup", e => {
    if (e.key === "Escape") return hideMenu();
    setTimeout(showMenu, 0);
  });
  ta.addEventListener("input", () => { hideMenu(); draw(); });
  ta.addEventListener("scroll", syncScroll);
  ta.addEventListener("blur", () => setTimeout(hideMenu, 150));

  ta.addEventListener("mousemove", e => {
    const hot = (e.ctrlKey || e.metaKey) && markAt(e);
    ta.classList.toggle("following", !!hot);
  });
  ta.addEventListener("click", e => {
    if (!(e.ctrlKey || e.metaKey)) return;
    const m = markAt(e);
    if (m) { e.preventDefault(); followToDoc(m.a); }
  });

  cfg.layer().addEventListener("click", e => {
    const dot = e.target.closest(".anchor-dot");
    if (!dot) return;
    const m = S.marks.find(x => x.a.id === dot.dataset.anchor);
    if (!m) return;
    if (e.altKey) return unlink(m.a.id);
    followToDoc(m.a);
  });

  // Selecting in the document offers the same link, the other way round.
  document.addEventListener("mouseup", onSelectInDoc, true);
  document.addEventListener("mousedown", e => {
    if (docMenu && !docMenu.contains(e.target)) hideDocMenu();
  }, true);

  window.addEventListener("resize", () => {
    draw();
    if (docSurface) docSurface.redraw();
  });

  PDFView.onAnchorClick = (hits, e) => choose(hits, "toNote", e);

  return {
    load, draw, clear, syncScroll, pruneLost,
    redrawPreview: drawPreview,
    get linking() { return !!S.pending; },
    get lost() { return S.lost; },
  };
}
