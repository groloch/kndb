"use strict";

/* A *surface* is anything a link can point into that is ordinary HTML: a
 * rendered markdown source, an HTML source inside its frame, or the note
 * preview. The PDF viewer is the fourth surface and implements the same three
 * verbs — capture, show, reveal — its own way, because it has pages and needs
 * geometry that survives zoom.
 *
 * Highlights are drawn as absolutely positioned rectangles in an overlay that
 * scrolls with the content, computed from a live Range. Nothing wraps the text
 * in extra elements: wrapping would change the very offsets the locators are
 * written in, and would fight with the source's own markup.
 *
 * The overlay never takes the pointer — clicks are hit-tested against the
 * stored rectangles — so selecting text over a highlight keeps working, which
 * is what makes a passage linkable twice.
 */

const INLINE_TAGS = new Set([
  "SPAN", "A", "EM", "STRONG", "B", "I", "CODE", "U", "S", "SUP", "SUB",
  "SMALL", "MARK", "ABBR", "CITE", "Q", "TIME", "VAR", "KBD", "SAMP", "DEL",
  "INS", "BDI", "BDO", "FONT", "LABEL",
]);

function makeTextSurface(cfg) {

  // cfg: {root, scroller, win, onClick}
  let items = [];       // [{id, loc, color, rects}]
  let overlay = null;
  let ro = null;
  let bound = null;     // the scroller we attached the listeners to
  let hovered = null;   // id of the link under the pointer

  const root = () => cfg.root();
  const scroller = () => (cfg.scroller && cfg.scroller()) || cfg.root();
  const win = () => (cfg.win && cfg.win()) || window;
  const doc = () => { const r = root(); return (r && r.ownerDocument) || document; };

  function blockOf(node, stop) {
    /* Block element a text node belongs to, walking out through inline tags
    */
    let el = node.parentElement;
    while (el && el !== stop && INLINE_TAGS.has(el.tagName)) el = el.parentElement;
    return el;
  }

  function flatten() {
    /* The surface's text as one string, plus where each text node starts in it.
    * Block boundaries become newlines, so a quote cannot silently run two
    * paragraphs together. Script, style and overlay text is left out
    */
    const r = root();
    const nodes = [];
    let text = "", prevBlock = null;
    if (!r) return { text, nodes };
    const walk = doc().createTreeWalker(r, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        const p = n.parentElement;
        if (!p || p.tagName === "SCRIPT" || p.tagName === "STYLE") {
          return NodeFilter.FILTER_REJECT;
        }
        if (p.closest(".anchor-overlay")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const block = blockOf(n, r);
      if (prevBlock && block !== prevBlock && text && !text.endsWith("\n")) {
        text += "\n";
      }
      prevBlock = block;
      nodes.push({ node: n, start: text.length });
      text += n.nodeValue;
    }
    return { text, nodes };
  }

  function offsetOf(map, node, offset) {
    /* Position in the flattened text of a DOM boundary, null when off the map
    */
    if (node.nodeType === 3) {
      for (const e of map.nodes) if (e.node === node) return e.start + offset;
      return null;
    }
    // A boundary that landed on an element: take the child it points at.
    const child = node.childNodes[offset] || node.lastChild;
    if (!child) return null;
    for (const e of map.nodes) {
      if (e.node === child || child.contains(e.node)) return e.start;
    }
    return null;
  }

  function nodeAt(map, offset) {
    /* Text node and local offset holding a flat offset, null when past the end
    */
    for (const e of map.nodes) {
      if (offset <= e.start + e.node.nodeValue.length) {
        return { node: e.node, offset: Math.max(0, offset - e.start) };
      }
    }
    return null;
  }

  function capture() {
    /* Locator of the current selection, null unless it sits inside the root
    */
    const sel = win().getSelection();
    const r = root();
    if (!sel || sel.isCollapsed || !sel.rangeCount || !r) return null;
    const range = sel.getRangeAt(0);
    if (!r.contains(range.startContainer) || !r.contains(range.endContainer)) {
      return null;
    }
    const map = flatten();
    const a = offsetOf(map, range.startContainer, range.startOffset);
    const b = offsetOf(map, range.endContainer, range.endOffset);
    if (a === null || b === null || b <= a) return null;
    if (!map.text.slice(a, b).trim()) return null;
    return Object.assign({ kind: "text", page: 0 }, quoteLocator(map.text, a, b));
  }

  function ensureStyles() {
    /* Puts the highlight CSS in the surface's own document.
    * A source's frame carries its own stylesheet, and ours is not in it
    */
    const d = doc();
    if (d === document || d.getElementById("kndb-anchor-style")) return;
    const el = d.createElement("style");
    el.id = "kndb-anchor-style";
    el.textContent = ".anchor-overlay{position:absolute;top:0;left:0;width:100%;"
      + "height:0;pointer-events:none;z-index:2147483000}"
      + ".anchor-hl{position:absolute;background:#4a6cf7;opacity:.2;"
      + "border-radius:2px;transition:opacity .12s}"
      + ".anchor-hl.hover{opacity:.42}"
      // !important because the source's own stylesheet is not ours to predict
      + ".anchor-hot,.anchor-hot *{cursor:pointer!important}"
      + ".anchor-hl.flash{animation:kndb-anchor-pulse 1.5s ease-out}"
      + "@keyframes kndb-anchor-pulse{0%{opacity:.2}12%{opacity:.75}"
      + "35%{opacity:.25}55%{opacity:.7}100%{opacity:.2}}";
    (d.head || d.documentElement).appendChild(el);
  }

  function ensureOverlay() {
    /* Builds the overlay once, and makes the root a positioning context for it
    */
    const r = root();
    ensureStyles();
    if (overlay && overlay.parentElement === r) return overlay;
    if (getComputedStyle(r).position === "static") r.style.position = "relative";
    overlay = doc().createElement("div");
    overlay.className = "anchor-overlay";
    r.appendChild(overlay);
    return overlay;
  }

  function show(list) {
    /* Draws a new set of links, dropping the entries without a locator
    */
    items = (list || []).filter(x => x && x.loc);
    redraw();
  }

  function redraw() {
    /* Remeasures every highlight from a live Range.
    * A locator that no longer resolves leaves its entry without rectangles,
    * so it stops being hit-testable
    */
    const r = root();
    if (!r || !r.isConnected) return;
    ensureOverlay();
    overlay.innerHTML = "";
    if (!items.length) return;

    const map = flatten();
    const rr = r.getBoundingClientRect();
    const sc = scroller();
    for (const it of items) {
      it.rects = [];
      const at = resolveLocator(map.text, it.loc);
      if (!at) continue;
      const a = nodeAt(map, at.start), b = nodeAt(map, at.end);
      if (!a || !b) continue;
      const range = doc().createRange();
      try {
        range.setStart(a.node, a.offset);
        range.setEnd(b.node, b.offset);
      } catch (_) { continue; }
      for (const cr of mergeRowRects([...range.getClientRects()])) {
        const box = {
          x: cr.left - rr.left + sc.scrollLeft,
          y: cr.top - rr.top + sc.scrollTop,
          w: cr.width, h: cr.height,
        };
        it.rects.push(box);
        const d = doc().createElement("div");
        d.className = "anchor-hl";
        d.dataset.anchor = it.id;
        d.style.left = box.x + "px";
        d.style.top = box.y + "px";
        d.style.width = box.w + "px";
        d.style.height = box.h + "px";
        if (it.color) d.style.background = it.color;
        overlay.appendChild(d);
      }
    }
  }

  function hitTest(clientX, clientY) {
    /* Links whose rectangles cover a viewport point, in draw order
    */
    const r = root();
    if (!r) return [];
    const rr = r.getBoundingClientRect();
    const sc = scroller();
    const x = clientX - rr.left + sc.scrollLeft;
    const y = clientY - rr.top + sc.scrollTop;
    return items.filter(it => (it.rects || []).some(
      b => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h));
  }

  function onClick(e) {
    /* Hands the host every link under the pointer
    */
    if (!cfg.onClick) return;
    const sel = win().getSelection();
    if (sel && !sel.isCollapsed) return;   // that was a selection, not a click
    const hits = hitTest(e.clientX, e.clientY);
    if (hits.length) cfg.onClick(hits, e);
  }

  function onMove(e) {
    /* Dresses the root and the rectangle under the cursor so a link looks
    * like one.
    * The overlay takes no pointer events, so hovering is hit-tested the same
    * way a click is
    */
    if (!items.length) return;
    const hit = hitTest(e.clientX, e.clientY)[0];
    const r = root();
    r.classList.toggle("anchor-hot", !!hit);
    if (hovered === (hit ? hit.id : null)) return;
    hovered = hit ? hit.id : null;
    if (!overlay) return;
    overlay.querySelectorAll(".anchor-hl.hover")
      .forEach(el => el.classList.remove("hover"));
    if (hovered) {
      overlay.querySelectorAll('[data-anchor="' + hovered + '"]')
        .forEach(el => el.classList.add("hover"));
    }
  }

  function onLeave() {
    /* Undoes the hover dressing
    */
    hovered = null;
    const r = root();
    if (r) r.classList.remove("anchor-hot");
    if (overlay) {
      overlay.querySelectorAll(".anchor-hl.hover")
        .forEach(el => el.classList.remove("hover"));
    }
  }

  function reveal(id) {
    /* Scrolls to a link and flashes it, false when it is not drawn
    */
    const it = items.find(x => x.id === id);
    if (!it || !(it.rects || []).length) return false;
    const sc = scroller();
    const y = it.rects[0].y;
    if (y < sc.scrollTop || y > sc.scrollTop + sc.clientHeight - 30) {
      sc.scrollTo({ top: Math.max(0, y - sc.clientHeight / 3), behavior: "smooth" });
    }
    flash(id);
    return true;
  }

  function flash(id) {
    if (!overlay) return;
    const marks = overlay.querySelectorAll('[data-anchor="' + id + '"]');
    marks.forEach(m => m.classList.add("flash"));
    setTimeout(() => marks.forEach(m => m.classList.remove("flash")), 1600);
  }

  function attach() {
    /* Binds the pointer listeners to the scroller.
    * Moves them when the scroller changed, and watches the root for resizes
    */
    const sc = scroller();
    if (bound === sc) return;
    detach();
    if (!sc) return;
    sc.addEventListener("click", onClick);
    sc.addEventListener("mousemove", onMove);
    sc.addEventListener("mouseleave", onLeave);
    bound = sc;
    if (window.ResizeObserver && root()) {
      ro = new ResizeObserver(() => redraw());
      ro.observe(root());
    }
  }

  function detach() {
    /* Drops the pointer listeners and the resize observer
    */
    if (bound) {
      bound.removeEventListener("click", onClick);
      bound.removeEventListener("mousemove", onMove);
      bound.removeEventListener("mouseleave", onLeave);
    }
    onLeave();
    bound = null;
    if (ro) { ro.disconnect(); ro = null; }
  }

  function clear() {
    /* Forgets every link and empties the overlay, which stays attached
    */
    items = [];
    if (overlay) overlay.innerHTML = "";
  }

  return {
    capture, show, redraw, reveal, flash, hitTest, attach, detach, clear,
    get items() { return items; },
  };
}
