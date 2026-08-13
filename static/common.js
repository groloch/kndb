"use strict";

/* Shared frontend plumbing: identity, fetch, markdown, toasts.
 *
 * Identity is dev-mode: the page states who it is acting as via a header on
 * every API call. Rather than thread that through every call site, we patch
 * fetch once here — so app.js and project.js stay unaware of it, and swapping
 * in real auth means deleting this block. */

const KNDB = (window.KNDB = {
  USER_KEY: "kndb.user",
  // empty until /api/me answers: the server resolves an unnamed caller to
  // permissions.default_user, so the default lives in kndb.yaml, not here
  user: localStorage.getItem("kndb.user") || "",
  me: null,          // filled by /api/me
  users: [],
  roles: [],         // role vocabulary, from permissions: in kndb.yaml
  grants: {},        // action -> the roles allowed to do it
});

/** Does `role` cover `action`? The server enforces the same table; this only
 *  decides what to draw, since a button that always 403s is worse than none. */
KNDB.may = (role, action) => (KNDB.grants[action] || []).includes(role);

(function patchFetch() {
  const original = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if (url.startsWith("/api/")) {
      init = init || {};
      const headers = new Headers(init.headers || {});
      headers.set("X-KNDB-User", KNDB.user);
      init = { ...init, headers };
    }
    return original.call(this, input, init);
  };
})();

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts = {}) {
  const init = { headers: {}, ...opts };
  if (init.body && !(init.body instanceof FormData) && typeof init.body !== "string") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok || (data && data.ok === false)) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

let _toastTimer;
function toast(msg, kind = "info", ms = 4200) {
  const t = $("#toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = "toast"; }, ms);
}

// Accepts "modal-x" or "#modal-x" — both spellings exist across the two pages.
function modalEl(id) { return $(id.startsWith("#") ? id : `#${id}`); }
function openModal(id) { modalEl(id).classList.remove("hidden"); }
function closeModal(id) { modalEl(id).classList.add("hidden"); }

/** A modal that asks for one value — the app's own `window.prompt`. Builds its
 *  own markup so pages do not each have to carry a copy, and resolves to the
 *  trimmed string, or to `null` if the user backed out. */
function askModal(opts = {}) {
  return new Promise(resolve => {
    const modal = document.createElement("div");
    modal.className = "modal";
    modal.innerHTML =
      '<div class="modal-box narrow">'
      + '<div class="modal-head"><h3></h3><button class="x" type="button">×</button></div>'
      + '<div class="modal-body">'
      + '<label class="lbl"></label>'
      + '<input class="input" type="text" spellcheck="false">'
      + '<div class="modal-actions"><p class="muted hint"></p>'
      + '<button class="btn" data-act="cancel" type="button">Cancel</button>'
      + '<button class="btn primary" data-act="ok" type="button"></button>'
      + "</div></div></div>";

    const input = $("input", modal), ok = $('[data-act="ok"]', modal);
    $("h3", modal).textContent = opts.title || "";
    $(".lbl", modal).textContent = opts.label || "Value";
    // Left in place when empty: `.modal-actions .hint` is `flex: 1`, so it is
    // also the spacer that keeps the buttons on the right.
    $(".hint", modal).textContent = opts.hint || "";
    input.value = opts.value || "";
    input.placeholder = opts.placeholder || "";
    ok.textContent = opts.submit || "OK";
    ok.disabled = !input.value.trim();

    function done(value) {
      document.removeEventListener("keydown", onKey, true);
      modal.remove();
      resolve(value);
    }
    function onKey(e) {
      if (e.key === "Escape") { e.stopPropagation(); done(null); }
    }

    input.addEventListener("input", () => { ok.disabled = !input.value.trim(); });
    input.addEventListener("keydown", e => {
      if (e.key === "Enter" && input.value.trim()) done(input.value.trim());
    });
    ok.addEventListener("click", () => { if (input.value.trim()) done(input.value.trim()); });
    modal.addEventListener("click", e => {
      if (e.target.closest('[data-act="cancel"]') || e.target.closest(".x")) done(null);
    });
    modal.addEventListener("mousedown", e => { if (e.target === modal) done(null); });
    // Capture, so a page-level shortcut handler does not see the keystroke.
    document.addEventListener("keydown", onKey, true);

    document.body.appendChild(modal);
    input.focus();
    // A prefilled name is a suggestion to replace; a prefilled path is a prefix
    // to type onto, so leave the caret after it.
    if (opts.selectAll === false) input.setSelectionRange(input.value.length, input.value.length);
    else input.select();
  });
}

/** A modal that asks a yes/no question — the app's own `window.confirm`.
 *  Resolves to `true` only if the user picks the confirming button; backing out
 *  any other way (Cancel, ×, Escape, a click on the backdrop) resolves `false`.
 *
 *  `body` is the question, `hint` the consequence spelled out underneath it.
 *  Set `danger` for anything that destroys text, which paints the button red.
 *
 *  opts = {title, body, hint, confirm, cancel, danger}
 */
function confirmModal(opts = {}) {
  return new Promise(resolve => {
    const modal = document.createElement("div");
    modal.className = "modal";
    modal.innerHTML =
      '<div class="modal-box narrow">'
      + '<div class="modal-head"><h3></h3><button class="x" type="button">×</button></div>'
      + '<div class="modal-body">'
      + '<p class="ask-body"></p>'
      + '<p class="muted ask-hint"></p>'
      + '<div class="modal-actions"><span class="hint"></span>'
      + '<button class="btn" data-act="cancel" type="button"></button>'
      + '<button class="btn primary" data-act="ok" type="button"></button>'
      + "</div></div></div>";

    const ok = $('[data-act="ok"]', modal);
    $("h3", modal).textContent = opts.title || "Are you sure?";
    $(".ask-body", modal).textContent = opts.body || "";
    const hint = $(".ask-hint", modal);
    hint.textContent = opts.hint || "";
    if (!opts.hint) hint.remove();
    $('[data-act="cancel"]', modal).textContent = opts.cancel || "Cancel";
    ok.textContent = opts.confirm || "OK";
    if (opts.danger) ok.classList.replace("primary", "danger");

    function done(value) {
      document.removeEventListener("keydown", onKey, true);
      modal.remove();
      resolve(value);
    }
    function onKey(e) {
      if (e.key === "Escape") { e.stopPropagation(); done(false); }
    }

    ok.addEventListener("click", () => done(true));
    modal.addEventListener("click", e => {
      if (e.target.closest('[data-act="cancel"]') || e.target.closest(".x")) done(false);
    });
    modal.addEventListener("mousedown", e => { if (e.target === modal) done(false); });
    // Capture, so a page-level shortcut handler does not see the keystroke.
    document.addEventListener("keydown", onKey, true);

    document.body.appendChild(modal);
    // Like the native dialog, the confirming button holds focus, so Enter
    // answers yes and Escape answers no without reaching for the mouse.
    ok.focus();
  });
}

/* ---------- Markdown ---------- */

function renderMarkdown(md) {
  if (!md) return "";
  // Both libraries are vendored (dev_tools/fetch_vendor.py). Without the
  // sanitizer, marked's HTML cannot be trusted at all, so fall all the way back
  // to miniMarkdown — it escapes the source first and only ever emits its own
  // small set of tags. Degrade the rendering, never the safety.
  if (!window.DOMPurify) return miniMarkdown(md);
  const html = window.marked
    ? window.marked.parse(md, { breaks: true, gfm: true })
    : miniMarkdown(md);
  return DOMPurify.sanitize(html);
}

function miniMarkdown(md) {
  const esc = md.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = esc.split(/\r?\n/);
  const out = []; let inCode = false; let para = [];
  const flushP = () => { if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; } };
  const inline = t => t
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
  for (const raw of lines) {
    if (raw.startsWith("```")) {
      flushP();
      if (!inCode) { out.push("<pre><code>"); inCode = true; }
      else { out.push("</code></pre>"); inCode = false; }
      continue;
    }
    if (inCode) { out.push(raw); continue; }
    const h = raw.match(/^(#{1,6})\s+(.*)/);
    if (h) { flushP(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    if (/^&gt;\s?/.test(raw)) { flushP(); out.push(`<blockquote>${inline(raw.replace(/^&gt;\s?/, ""))}</blockquote>`); continue; }
    if (/^[-*]\s+/.test(raw)) { flushP(); out.push(`<li>${inline(raw.replace(/^[-*]\s+/, ""))}</li>`); continue; }
    if (/^\d+\.\s+/.test(raw)) { flushP(); out.push(`<li>${inline(raw.replace(/^\d+\.\s+/, ""))}</li>`); continue; }
    if (/^---+\s*$/.test(raw)) { flushP(); out.push("<hr>"); continue; }
    if (raw.trim() === "") { flushP(); continue; }
    para.push(inline(raw));
  }
  flushP();
  if (inCode) out.push("</code></pre>");
  return out.join("\n");
}

function typePill(stype) {
  return '<span class="pill type-' + String(stype).replace("+", "\\+") + '">'
    + escapeHtml(stype) + "</span>";
}

/** PDFs go to our own renderer rather than the browser's, because a passage
 *  has to be selectable for a note to point at it (see plan-anchors.md). */
function isPdf(s) { return !!s && s.source_type === "pdf"; }


/* ---------- Quote locators ---------- */

/* Both ends of an anchor are quotes, not positions: a note sentence keeps its
 * link through edits above it, and a passage keeps its link through a re-fetch.
 * The stored offset is only a hint that makes the common case a single string
 * comparison. Pure functions, no DOM — the one piece of anchoring that could be
 * unit-tested if a JS runner is ever added. */

const LOC_CONTEXT = 40;

function quoteLocator(text, start, end) {
  return {
    exact: text.slice(start, end),
    prefix: text.slice(Math.max(0, start - LOC_CONTEXT), start),
    suffix: text.slice(end, end + LOC_CONTEXT),
    char_start: start,
  };
}

/** Find `loc` in `text`, or null if the quoted text is gone. */
function resolveLocator(text, loc) {
  const exact = (loc && loc.exact) || "";
  if (!exact || !text) return null;

  // The hint, verified. Almost always the answer, and costs one comparison.
  const hint = Number.isFinite(loc.char_start) ? loc.char_start : -1;
  if (hint >= 0 && text.substr(hint, exact.length) === exact) {
    return { start: hint, end: hint + exact.length };
  }

  // Otherwise the quote moved: take the occurrence whose surroundings look
  // most like the ones we recorded, and break ties by staying near the hint.
  let best = null, bestScore = -1;
  for (let i = text.indexOf(exact); i !== -1; i = text.indexOf(exact, i + 1)) {
    const score = _tailMatch(text.slice(0, i), loc.prefix || "")
      + _headMatch(text.slice(i + exact.length), loc.suffix || "");
    const nearer = best && hint >= 0
      && Math.abs(i - hint) < Math.abs(best.start - hint);
    if (score > bestScore || (score === bestScore && nearer)) {
      best = { start: i, end: i + exact.length };
      bestScore = score;
    }
  }
  return best;
}

/** Collapse a Range's client rectangles to one per line.
 *
 *  A selection crossing several spans reports a rectangle per span, which
 *  draws as a row of boxes with gaps at every word the markup happens to
 *  split, and — for a PDF, where the rectangles are stored — can push a
 *  locator past the size the server accepts. One box per line is both what the
 *  reader expects to see and an order of magnitude less to store. */
function mergeRowRects(rects) {
  const rows = [];
  for (const r of rects) {
    if (r.width < 0.5 || r.height < 0.5) continue;
    const tol = Math.max(2, r.height * 0.6);
    // Compared against the line the row *started* on, never against its grown
    // bounds: otherwise each merge widens the row enough to swallow the next
    // line, and a whole paragraph collapses into one box.
    const row = rows.find(x =>
      Math.abs(x.keyTop - r.top) <= tol && Math.abs(x.keyBottom - r.bottom) <= tol
      // …and only across a gap a line of text could plausibly contain, so two
      // columns of a paper are never bridged over the gutter between them.
      && r.left - x.right <= Math.max(20, r.height * 3)
      && x.left - r.right <= Math.max(20, r.height * 3));
    if (row) {
      row.left = Math.min(row.left, r.left);
      row.right = Math.max(row.right, r.right);
      row.top = Math.min(row.top, r.top);
      row.bottom = Math.max(row.bottom, r.bottom);
    } else {
      rows.push({ left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                  keyTop: r.top, keyBottom: r.bottom });
    }
  }
  return rows.map(r => ({ left: r.left, top: r.top,
                          width: r.right - r.left, height: r.bottom - r.top }));
}

/** How many characters `a` and `b` share at their ends. */
function _tailMatch(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n++;
  return n;
}

/** How many characters `a` and `b` share at their starts. */
function _headMatch(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n++;
  return n;
}

/* ---------- Tags ---------- */

/* Tags live on the source row itself, so they are shared by every project and
 * every user holding that source — editing them anywhere edits them
 * everywhere. `sources.tags` is one comma-separated string. */

function splitTags(s) {
  return String(s || "").split(",").map(t => t.trim()).filter(Boolean);
}

function joinTags(tags) { return tags.join(", "); }

/** Save a tag list onto a source. Sends only `tags`, which the meta route
 *  treats as a partial update. */
async function saveSourceTags(sid, tags) {
  const d = await api(`/api/source/${sid}/meta`,
    { method: "POST", body: { tags: joinTags(tags) } });
  return d.tags;
}

/** The tag manager, shared by both pages so tagging works the same way
 *  everywhere. Each add and each removal is saved on the spot — there is no
 *  Save button to forget — and `onSaved` receives the stored tag string.
 *
 *  opts = {source, readOnly, onSaved}
 */
function openTagsModal(opts = {}) {
  const src = opts.source;
  if (!src) return;
  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML =
    '<div class="modal-box narrow">'
    + '<div class="modal-head"><h3>Tags</h3><button class="x" type="button">×</button></div>'
    + '<div class="modal-body">'
    + '<p class="muted tag-owner"></p>'
    + '<div class="tags-list"></div>'
    + '<div class="tag-add"><input class="input" placeholder="new tag…" spellcheck="false">'
    + '<button class="btn primary" type="button">+ Add</button></div>'
    + '<div class="modal-actions"><span class="hint">Searchable in the library with '
    + "<b>@tag</b>. Tags live on the source itself, so they follow it into every "
    + "project that holds it.</span>"
    + '<button class="btn" data-act="done" type="button">Done</button>'
    + "</div></div></div>";

  const list = $(".tags-list", modal);
  const input = $(".tag-add .input", modal);
  const addBtn = $(".tag-add .btn", modal);
  $(".tag-owner", modal).textContent = src.title || "";
  if (opts.readOnly) $(".tag-add", modal).classList.add("hidden");

  function draw() {
    const tags = splitTags(src.tags);
    if (!tags.length) {
      list.innerHTML = '<p class="muted">'
        + (opts.readOnly ? "No tags." : "No tags yet — add one below.") + "</p>";
      return;
    }
    list.innerHTML = tags.map(t =>
      '<span class="tag-chip">'
      + (opts.readOnly ? "" : '<button class="tag-x" type="button" title="Remove tag">×</button>')
      + "<span>@" + escapeHtml(t) + "</span></span>").join("");
    // The tag text rides on the chip's dataset rather than in an attribute in
    // the markup, so a tag containing quotes cannot break out of it.
    $$(".tag-chip", list).forEach((chip, i) => { chip.dataset.tag = tags[i]; });
  }

  async function write(tags) {
    try {
      src.tags = await saveSourceTags(src.id, tags);
      draw();
      if (opts.onSaved) opts.onSaved(src.tags);
    } catch (e) {
      toast("Could not save tags: " + e.message, "err");
    }
  }

  function add() {
    const v = input.value.trim().replace(/^@/, "");
    if (!v) return;
    const tags = splitTags(src.tags);
    if (tags.some(t => t.toLowerCase() === v.toLowerCase())) {
      toast("Already tagged @" + v, "warn");
      return;
    }
    input.value = "";
    write([...tags, v]);
  }

  function close() {
    document.removeEventListener("keydown", onKey, true);
    modal.remove();
  }
  function onKey(e) {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  }

  list.addEventListener("click", e => {
    const x = e.target.closest(".tag-x");
    if (!x) return;
    const tag = x.closest(".tag-chip").dataset.tag;
    write(splitTags(src.tags).filter(t => t !== tag));
  });
  addBtn.addEventListener("click", add);
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); add(); }
  });
  modal.addEventListener("click", e => {
    if (e.target.closest('[data-act="done"]') || e.target.closest(".x")) close();
  });
  modal.addEventListener("mousedown", e => { if (e.target === modal) close(); });
  document.addEventListener("keydown", onKey, true);

  draw();
  document.body.appendChild(modal);
  if (!opts.readOnly) input.focus();
}

function fmtDate(s) {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/* ---------- Blame ---------- */

/** Run-length blame -> one entry per line. */
function expandBlame(rle, nLines) {
  const out = [];
  for (const run of rle || []) {
    for (let i = 0; i < (run.n || 0); i++) out.push(run);
  }
  if (nLines == null) return out;
  while (out.length < nLines) out.push({ author: "", ts: "" });
  return out.slice(0, nLines);
}

const EXTERNAL_COLOR = "#94a3b8";
function blameColor(colors, author) {
  if (!author) return "transparent";
  return (colors && colors[author]) || EXTERNAL_COLOR;
}

/** May the current user delete this note page? Mirrors
 *  `backend/data/notes.py:deletable_by`, which is the authority — this only
 *  decides whether to draw the button, since one that always 403s is worse
 *  than none. `page.authors` is every author still holding a line. */
function noteDeletable(page, role) {
  if (!page || !KNDB.may(role, "edit_others")) return false;
  return (page.authors || []).every(a => !a || a === KNDB.user);
}

/* ---------- Identity switcher ---------- */

async function initIdentity() {
  let me;
  try {
    me = await api("/api/me");
  } catch (e) {
    return null;
  }
  KNDB.me = me.user;
  KNDB.user = KNDB.user || (me.user && me.user.name) || "";
  KNDB.users = me.users || [];
  KNDB.personal = me.personal_project;
  KNDB.roles = (me.permissions && me.permissions.roles) || [];
  KNDB.grants = (me.permissions && me.permissions.grants) || {};
  KNDB.defaultRole = (me.permissions && me.permissions.default_role) || "";
  KNDB.ownerRole = (me.permissions && me.permissions.owner_role) || "";
  renderUserSwitcher();
  return me;
}

function renderUserSwitcher() {
  const host = $("#user-switch");
  if (!host) return;
  const opts = KNDB.users.map(u =>
    `<option value="${escapeHtml(u.name)}"${u.name === KNDB.user ? " selected" : ""}>`
    + escapeHtml(u.display_name || u.name) + "</option>").join("");
  host.innerHTML =
    `<select id="user-select" class="input small" title="Acting as (dev mode)">${opts}</select>`
    + '<button id="user-new" class="btn small" title="Add a user">+</button>';

  $("#user-select").addEventListener("change", e => {
    KNDB.user = e.target.value;
    localStorage.setItem(KNDB.USER_KEY, KNDB.user);
    location.reload();
  });
  $("#user-new").addEventListener("click", async () => {
    const name = await askModal({
      title: "Add a user",
      label: "User name",
      placeholder: "alice",
      submit: "Add user",
      hint: "Dev mode — no password. They get their own personal workspace, "
          + "and you can act as them from this switcher.",
    });
    if (!name) return;
    try {
      await api("/api/users", { method: "POST", body: { name } });
      KNDB.user = name;
      localStorage.setItem(KNDB.USER_KEY, name);
      location.reload();
    } catch (e) {
      toast(e.message, "err");
    }
  });
}
