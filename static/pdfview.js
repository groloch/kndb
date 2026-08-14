"use strict";

/* PDF renderer, replacing the browser's built-in reader.
 *
 * The built-in one is a black box: its page cannot be scripted from here, so a
 * selection cannot be read out of it. Owning the render is what lets a note
 * sentence point at a passage.
 *
 * Pages are laid out at their real size immediately, so the scrollbar is honest
 * from the first frame, but painted only while they are near the viewport.
 *
 * Exposes a single global, PDFView. The library is imported lazily, so a
 * workspace of markdown sources never pays for it
 */

const PDFView = (() => {

  /* How far outside the viewport a page still gets painted, in viewport
  * heights. One screen of slack in each direction keeps scrolling smooth
  * without holding a whole book's worth of canvases
  */
  const MARGIN = 1;

  /* Above this page count, the document is no longer asked for every page's
  * dimensions up front, and page 1's size stands in until a page is really
  * rendered. Papers never hit it. Thousand-page scans would stall on open
  */
  const MEASURE_ALL_UNDER = 300;

  const ZOOMS = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 3, 4];

  let lib = null;

  async function loadLib() {
    /* Imports pdf.js once, and points it at its worker
    */
    if (!lib) {
      lib = await import("/static/vendor/pdf.min.mjs");
      lib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.min.mjs";
    }
    return lib;
  }

  const st = {
    doc: null,
    loadingTask: null,  // the document is torn down through this, not through doc
    host: null,         // the element we were mounted into
    scroll: null,       // the scrolling column
    bar: null,
    pages: [],          // {n, div, canvasWrap, textDiv, w1, h1, page, task, state}
    zoom: null,         // null = fit width, otherwise a factor
    scale: 1,           // resolved css scale actually in use
    token: 0,           // bumped on every open(); stale async work checks it
    io: null,
    ro: null,
    current: 1,
  };

  async function open(url, host) {
    /* Renders url into host.
    * Safe to call again to switch documents
    */
    // Tear the previous document down *first*: destroy() invalidates the
    // in-flight token, so ours has to be taken after it, not before.
    destroy();
    const token = ++st.token;
    st.host = host;
    host.classList.remove("hidden");
    host.innerHTML = "";
    host.appendChild(buildBar());
    st.scroll = document.createElement("div");
    st.scroll.className = "pdf-scroll";
    host.appendChild(st.scroll);
    setStatus("Loading…");

    let pdfjs;
    try {
      pdfjs = await loadLib();
    } catch (e) {
      if (token !== st.token) return;
      return fail("The PDF renderer could not be loaded — run "
        + "python dev_tools/fetch_vendor.py to install it. (" + e.message + ")");
    }
    if (token !== st.token) return;

    try {
      st.loadingTask = pdfjs.getDocument({ url, isEvalSupported: false });
      st.doc = await st.loadingTask.promise;
    } catch (e) {
      if (token !== st.token) return;
      return fail("This PDF could not be opened: " + (e && e.message || e));
    }
    if (token !== st.token) { st.doc = null; return; }

    await buildPages(token);
    if (token !== st.token) return;

    setStatus("");
    relayout();
    drawAnchors();
    observe();
    st.scroll.addEventListener("scroll", onScroll, { passive: true });
    st.scroll.addEventListener("click", onClick);
    st.scroll.addEventListener("mousemove", onMove);
  }

  function destroy() {
    /* Tears the open document down and empties the host.
    * Bumps the token, so work already in flight drops its result
    */
    st.token++;
    if (st.io) { st.io.disconnect(); st.io = null; }
    if (st.ro) { st.ro.disconnect(); st.ro = null; }
    for (const p of st.pages) cancel(p);
    st.pages = [];
    // Destroying the loading task also aborts a load still in flight, which is
    // what happens when the user clicks through sources faster than they open.
    if (st.loadingTask) {
      try { st.loadingTask.destroy(); } catch (_) {}
      st.loadingTask = null;
    }
    st.doc = null;
    if (st.scroll) {
      st.scroll.removeEventListener("scroll", onScroll);
      st.scroll.removeEventListener("click", onClick);
      st.scroll.removeEventListener("mousemove", onMove);
    }
    hovered = null;
    st.scroll = null;
    st.bar = null;
    st.zoom = null;
    anchors = [];   // another document's links must never draw on this one
    st.current = 1;
    if (st.host) { st.host.innerHTML = ""; st.host.classList.add("hidden"); }
    st.host = null;
  }

  function fail(msg) {
    /* Replaces the viewer with a message, when the document cannot be shown
    */
    if (!st.host) return;
    st.host.innerHTML = '<div class="empty"></div>';
    st.host.firstChild.textContent = msg;
  }

  async function buildPages(token) {
    /* One sized, unpainted placeholder per page.
    * Gives up silently when token is no longer the current one, meaning
    * another document was opened meanwhile
    */
    const n = st.doc.numPages;
    // Page one is always needed: it sets the fit-width scale and, for long
    // documents, stands in for the size of every page we do not measure.
    const first = await st.doc.getPage(1);
    if (token !== st.token) return;
    const v1 = first.getViewport({ scale: 1 });

    const measured = [];
    if (n <= MEASURE_ALL_UNDER) {
      const rest = await Promise.all(
        Array.from({ length: n - 1 }, (_, i) => st.doc.getPage(i + 2)));
      if (token !== st.token) return;
      measured.push(first, ...rest);
    } else {
      measured.push(first);
    }

    for (let i = 1; i <= n; i++) {
      const page = measured[i - 1] || null;
      const vp = page ? page.getViewport({ scale: 1 }) : v1;
      const div = document.createElement("div");
      div.className = "pdf-page";
      div.dataset.page = String(i);
      const canvasWrap = document.createElement("div");
      canvasWrap.className = "pdf-canvas-wrap";
      // Anchored passages sit *under* the invisible text layer so they never
      // stand between the user and a selection; clicks on them are resolved by
      // hit-testing instead. The layer belongs to the page, not to the canvas,
      // so highlights survive a page being released and repainted.
      const hl = document.createElement("div");
      hl.className = "pdf-hl";
      const textDiv = document.createElement("div");
      textDiv.className = "textLayer";
      div.append(canvasWrap, hl, textDiv);
      st.scroll.appendChild(div);
      st.pages.push({
        n: i, div, canvasWrap, hl, textDiv, page,
        w1: vp.width, h1: vp.height, task: null, state: "blank",
      });
    }
    $bar("pages").textContent = String(n);
    $bar("page").value = "1";
  }

  function relayout() {
    /* Resolves the scale and sizes every placeholder.
    * Called on open, on zoom, and when the pane is resized
    */
    if (!st.pages.length) return;
    const avail = st.scroll.clientWidth - 28;         // page margins + scrollbar
    const fit = Math.max(0.1, avail / st.pages[0].w1);
    st.scale = st.zoom == null ? fit : st.zoom;
    for (const p of st.pages) {
      const w = Math.round(p.w1 * st.scale);
      const h = Math.round(p.h1 * st.scale);
      p.div.style.width = w + "px";
      p.div.style.height = h + "px";
      // The text layer positions its spans in terms of these; pdf.js writes
      // sizes as calc(… * var(--total-scale-factor)).
      p.div.style.setProperty("--scale-factor", st.scale);
      p.div.style.setProperty("--total-scale-factor", st.scale);
      if (p.state === "done") { release(p); }         // repaint at the new size
    }
    $bar("zoom").textContent = Math.round(st.scale * 100) + "%";
    paintVisible();
  }

  function observe() {
    /* Paints pages as they near the viewport, releases them as they leave.
    * Also refits the width on a resize, unless a zoom was chosen
    */
    st.io = new IntersectionObserver(entries => {
      for (const e of entries) {
        const p = st.pages[Number(e.target.dataset.page) - 1];
        if (!p) continue;
        if (e.isIntersecting) paint(p);
        else release(p);
      }
    }, { root: st.scroll, rootMargin: (MARGIN * 100) + "% 0px" });
    for (const p of st.pages) st.io.observe(p.div);

    st.ro = new ResizeObserver(() => { if (st.zoom == null) relayout(); });
    st.ro.observe(st.scroll);
  }

  function paintVisible() {
    /* Paints the pages on screen right now
    */
    // IntersectionObserver only fires on change, so after a zoom we prime the
    // pages that are on screen right now ourselves.
    const top = st.scroll.scrollTop, h = st.scroll.clientHeight;
    for (const p of st.pages) {
      const a = p.div.offsetTop, b = a + p.div.offsetHeight;
      if (b >= top - h * MARGIN && a <= top + h * (1 + MARGIN)) paint(p);
    }
  }

  function cancel(p) {
    /* Drops a page's render still in flight
    */
    if (p.task) { try { p.task.cancel(); } catch (_) {} p.task = null; }
  }

  function release(p) {
    /* Frees a page's canvas and text layer, back to a sized placeholder
    */
    cancel(p);
    if (p.state === "blank") return;
    p.canvasWrap.innerHTML = "";
    p.textDiv.innerHTML = "";
    p.state = "blank";
  }

  async function paint(p) {
    /* Paints a blank page, text layer first, then the canvas.
    * Every await is followed by a staleness check, as a zoom, a release or
    * another document can land mid-render
    */
    if (p.state !== "blank") return;
    p.state = "painting";
    const token = st.token, scale = st.scale;
    try {
      if (!p.page) {
        p.page = await st.doc.getPage(p.n);
        if (token !== st.token) return;
        const v1 = p.page.getViewport({ scale: 1 });
        if (Math.abs(v1.width - p.w1) > 0.5 || Math.abs(v1.height - p.h1) > 0.5) {
          p.w1 = v1.width; p.h1 = v1.height;
          p.div.style.width = Math.round(p.w1 * scale) + "px";
          p.div.style.height = Math.round(p.h1 * scale) + "px";
        }
      }
      const vp = p.page.getViewport({ scale });

      // The text layer goes first. It is invisible and exists only so the
      // passage can be selected — which is the entire point of rendering the
      // PDF ourselves — and it is pure DOM, so it lands immediately. Canvas
      // painting is driven by requestAnimationFrame and therefore stalls
      // completely in a background tab; selection must not wait on it.
      p.textDiv.innerHTML = "";
      const tl = new lib.TextLayer({
        textContentSource: p.page.streamTextContent(),
        container: p.textDiv,
        viewport: vp,
      });
      await tl.render();
      if (token !== st.token || scale !== st.scale || p.state !== "painting") return;
      p.state = "done";

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(vp.width * dpr);
      canvas.height = Math.floor(vp.height * dpr);
      canvas.style.width = "100%";
      canvas.style.height = "100%";
      const ctx = canvas.getContext("2d", { alpha: false });
      p.task = p.page.render({
        canvasContext: ctx,
        viewport: vp,
        transform: dpr === 1 ? null : [dpr, 0, 0, dpr, 0, 0],
      });
      await p.task.promise;
      p.task = null;
      // The page may have been released or rescaled while the canvas painted.
      if (token !== st.token || scale !== st.scale || p.state !== "done") return;
      p.canvasWrap.replaceChildren(canvas);
    } catch (e) {
      p.task = null;
      // A cancelled render is the normal outcome of scrolling fast; anything
      // else is worth leaving a trace of.
      if (!(e && (e.name === "RenderingCancelledException" || e.name === "AbortException"))) {
        console.warn("pdf page " + p.n + ": " + (e && e.message || e));
      }
      if (p.state === "painting") p.state = "blank";
    }
  }

  /* The document end of a link. Rects are stored as fractions of the page, so
  * a highlight is drawn correctly at any zoom, and even on a page that has not
  * been painted yet
  */

  let anchors = [];
  let onAnchorClick = null;
  let hovered = null;   // id of the passage under the pointer

  function pageText(p) {
    /* One page's text as a single string, plus where each text node starts in
    * it. The coordinate system every document locator is written in, so
    * capturing and resolving cannot disagree about offsets
    */
    const nodes = [];
    let text = "", prevTop = null;
    const walk = document.createTreeWalker(p.textDiv, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      // pdf.js emits one span per run of text; a run that starts further down
      // the page starts a new line, which nothing in the text itself records.
      const top = n.parentElement ? n.parentElement.offsetTop : 0;
      if (prevTop !== null && top !== prevTop && text && !text.endsWith("\n")) {
        text += "\n";
      }
      prevTop = top;
      nodes.push({ node: n, start: text.length });
      text += n.nodeValue;
    }
    return { text, nodes };
  }

  function offsetOf(map, node, offset) {
    /* Where a point of the text layer falls in the page text, null when the
    * node is not one of the mapped ones
    */
    for (const e of map.nodes) {
      if (e.node === node) return e.start + offset;
    }
    return null;
  }

  function pageOf(node) {
    /* Page element a node sits in, null outside the pages
    */
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return el ? el.closest(".pdf-page") : null;
  }

  function normRects(rects, box) {
    /* Client rects merged line by line, as fractions of the page box
    */
    return mergeRowRects([...rects])
      .map(r => [(r.left - box.left) / box.width, (r.top - box.top) / box.height,
                 r.width / box.width, r.height / box.height]
        .map(v => Number(v.toFixed(5))));
  }

  function captureSelection() {
    /* Locator of the current document selection, null if none.
    * Also null when the selection straddles two pages, or falls outside the
    * text layer
    */
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const pageDiv = pageOf(range.startContainer);
    if (!pageDiv || !pageDiv.contains(range.endContainer)) return null;
    const p = st.pages[Number(pageDiv.dataset.page) - 1];
    if (!p) return null;

    const map = pageText(p);
    const a = offsetOf(map, range.startContainer, range.startOffset);
    const b = offsetOf(map, range.endContainer, range.endOffset);
    if (a === null || b === null || b <= a) return null;

    return Object.assign({ kind: "text", page: p.n },
                         quoteLocator(map.text, a, b),
                         { rects: normRects(range.getClientRects(),
                                            pageDiv.getBoundingClientRect()) });
  }

  function showAnchors(list) {
    /* Shows every anchor on this document, replacing the ones drawn before.
    * Items are {id, doc_loc, color}, those without a doc_loc are dropped
    */
    anchors = (list || []).filter(a => a && a.doc_loc);
    drawAnchors();
  }

  function drawAnchors() {
    /* Repaints the highlight layer of every page from the anchor list
    */
    if (!st.pages.length) return;
    for (const p of st.pages) p.hl.innerHTML = "";
    for (const a of anchors) {
      const p = st.pages[(a.doc_loc.page || 1) - 1];
      if (!p) continue;
      for (const r of a.doc_loc.rects || []) {
        const d = document.createElement("div");
        d.className = "pdf-hl-rect";
        d.dataset.anchor = a.id;
        d.style.left = (r[0] * 100) + "%";
        d.style.top = (r[1] * 100) + "%";
        d.style.width = (r[2] * 100) + "%";
        d.style.height = (r[3] * 100) + "%";
        if (a.color) d.style.background = a.color;
        p.hl.appendChild(d);
      }
    }
  }

  function hitsAt(e) {
    /* Every anchor under the pointer, tightest first.
    * Several links can cover the same words, so the caller gets them all and
    * asks the user which
    */
    const pageDiv = pageOf(e.target);
    if (!pageDiv) return [];
    const box = pageDiv.getBoundingClientRect();
    const x = (e.clientX - box.left) / box.width;
    const y = (e.clientY - box.top) / box.height;
    const page = Number(pageDiv.dataset.page);
    const hits = [];
    for (const a of anchors) {
      if ((a.doc_loc.page || 1) !== page) continue;
      let area = Infinity;
      for (const r of a.doc_loc.rects || []) {
        if (x < r[0] || x > r[0] + r[2] || y < r[1] || y > r[1] + r[3]) continue;
        area = Math.min(area, r[2] * r[3]);
      }
      if (area < Infinity) hits.push({ a, area });
    }
    return hits.sort((p, q) => p.area - q.area).map(h => h.a);
  }

  function onClick(e) {
    /* Hands the anchors under the pointer to the host, which follows them
    */
    if (!onAnchorClick || !anchors.length) return;
    // The click that ends a drag-selection is not a click on a link.
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;
    const hits = hitsAt(e);
    if (hits.length) onAnchorClick(hits, e);
  }

  function onMove(e) {
    /* Lights up the passage under the pointer, and the cursor with it.
    * Highlights are painted under the text layer and never take the pointer,
    * so the hit-testing is done here
    */
    if (!anchors.length) return;
    const hit = hitsAt(e)[0];
    st.scroll.classList.toggle("anchor-hot", !!hit);
    const id = hit ? hit.id : null;
    if (id === hovered) return;
    hovered = id;
    st.scroll.querySelectorAll(".pdf-hl-rect.hover")
      .forEach(el => el.classList.remove("hover"));
    if (id) {
      st.scroll.querySelectorAll('[data-anchor="' + id + '"]')
        .forEach(el => el.classList.add("hover"));
    }
  }

  function reveal(doc_loc, anchorId) {
    /* Scrolls a passage into view and flashes it.
    * False when no document is open, or the passage is on a page this one
    * does not have
    */
    if (!doc_loc || !st.pages.length) return false;
    const p = st.pages[(doc_loc.page || 1) - 1];
    if (!p) return false;
    const r = (doc_loc.rects || [])[0];
    const y = p.div.offsetTop + (r ? r[1] * p.div.offsetHeight : 0);
    st.scroll.scrollTo({ top: Math.max(0, y - st.scroll.clientHeight / 3),
                         behavior: "smooth" });
    st.current = p.n;
    if (st.bar) $bar("page").value = String(p.n);
    if (anchorId) flash(anchorId);
    return true;
  }

  function flash(anchorId) {
    /* Blinks every rect of one link
    */
    const marks = st.scroll.querySelectorAll('[data-anchor="' + anchorId + '"]');
    marks.forEach(m => m.classList.add("flash"));
    setTimeout(() => marks.forEach(m => m.classList.remove("flash")), 1500);
  }

  function $bar(name) { return st.bar.querySelector('[data-pdf="' + name + '"]'); }

  function buildBar() {
    /* Builds the toolbar: paging, status, zoom
    */
    const bar = document.createElement("div");
    bar.className = "pdf-bar";
    bar.innerHTML =
      '<button class="btn small" data-pdf="prev" title="Previous page">‹</button>'
      + '<input class="pdf-pageno" data-pdf="page" value="1" size="3" inputmode="numeric">'
      + '<span class="muted">/ <span data-pdf="pages">?</span></span>'
      + '<button class="btn small" data-pdf="next" title="Next page">›</button>'
      + '<span class="pdf-gap"></span>'
      + '<span class="muted pdf-status" data-pdf="status"></span>'
      + '<span class="pdf-gap"></span>'
      + '<button class="btn small" data-pdf="out" title="Zoom out">−</button>'
      + '<span class="muted" data-pdf="zoom">100%</span>'
      + '<button class="btn small" data-pdf="in" title="Zoom in">+</button>'
      + '<button class="btn small" data-pdf="fit" title="Fit the page width">Fit</button>';
    st.bar = bar;
    bar.addEventListener("click", e => {
      const b = e.target.closest("[data-pdf]");
      if (!b) return;
      if (b.dataset.pdf === "prev") goToPage(st.current - 1);
      if (b.dataset.pdf === "next") goToPage(st.current + 1);
      if (b.dataset.pdf === "in") stepZoom(1);
      if (b.dataset.pdf === "out") stepZoom(-1);
      if (b.dataset.pdf === "fit") { st.zoom = null; relayout(); }
    });
    bar.addEventListener("keydown", e => {
      if (e.key !== "Enter" || !e.target.matches('[data-pdf="page"]')) return;
      goToPage(parseInt(e.target.value, 10));
      e.target.blur();
    });
    return bar;
  }

  function setStatus(t) { if (st.bar) $bar("status").textContent = t; }

  function stepZoom(dir) {
    /* One step up or down the zoom ladder, from the scale in use
    */
    const cur = st.scale;
    const next = dir > 0
      ? ZOOMS.find(z => z > cur + 0.01)
      : [...ZOOMS].reverse().find(z => z < cur - 0.01);
    if (next) { st.zoom = next; relayout(); }
  }

  function onScroll() {
    /* Keeps the toolbar on the page holding the upper third of the viewport.
    * Leaves the box alone while it is being typed in
    */
    const mid = st.scroll.scrollTop + st.scroll.clientHeight / 3;
    for (const p of st.pages) {
      if (p.div.offsetTop + p.div.offsetHeight > mid) {
        if (p.n !== st.current) {
          st.current = p.n;
          const box = $bar("page");
          if (document.activeElement !== box) box.value = String(p.n);
        }
        return;
      }
    }
  }

  function goToPage(n) {
    /* Scrolls page n into view, clamped to the document
    */
    if (!st.pages.length || !Number.isFinite(n)) return;
    n = Math.min(Math.max(1, n), st.pages.length);
    st.scroll.scrollTo({ top: st.pages[n - 1].div.offsetTop - 8, behavior: "smooth" });
    st.current = n;
    $bar("page").value = String(n);
  }

  return {
    open, destroy, goToPage,
    captureSelection, showAnchors, reveal,
    set onAnchorClick(fn) { onAnchorClick = fn; },
    get onAnchorClick() { return onAnchorClick; },
    get anchorCount() { return anchors.length; },
    get pageCount() { return st.pages.length; },
    get currentPage() { return st.current; },
    get isOpen() { return !!st.doc; },
  };
})();
