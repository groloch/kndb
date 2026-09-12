"use strict";

/* The /learning page: replay quizzes over a selection of the caller's
 * workspace sources, full page instead of the per-source dialog the
 * workspace used to ship.
 *
 * The selection is strategy-based — v1 knows "by tag" (AND semantics, like
 * the library's @tag search; no tag selected matches every workspace
 * source) — with a per-source include/exclude list and an "only due
 * questions" switch on top. Selection state is persisted to localStorage
 * so a reload keeps the choice; sessions are not, the server holds every
 * answer as it arrives.
 *
 * The quiz store is untouched: everything reads through the existing
 * /api/quiz/* routes, which the backend scopes to the caller's personal
 * workspace. This page owns no tree and no quiz rows.
 *
 * $, api, toast, escapeHtml, confirmModal typePill and initIdentity live in
 * common.js, which must load first.
 */

const SEL_KEY = "kndb.learning.selection";

function nowIso() {
  /* The moment, in the ISO form the stats' next_review is stored in
  */
  return new Date().toISOString().slice(0, 19) + "Z";
}

function isDue(st) {
  /* Whether a question's stats say it is due now.
  * Never played counts as due — the same notion quiz_order applies
  */
  const nr = (st && st.next_review) || "";
  return !nr || nr <= nowIso();
}

async function chunked(items, fetchOne, size = 8) {
  /* fetchOne(item) over items, at most size in flight at once
  */
  const out = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(...await Promise.all(items.slice(i, i + size).map(fetchOne)));
  }
  return out;
}

function loadSelection() {
  /* The saved selection, reconciled to a sane shape.
  * Storage is not trusted: missing fields get their defaults here, and the
  * caller prunes tags and ids against the live workspace
  */
  let sel = null;
  try { sel = JSON.parse(localStorage.getItem(SEL_KEY) || "null"); } catch (_) {}
  if (!sel || typeof sel !== "object") sel = {};
  const strategy = (sel.strategy && sel.strategy.kind === "tag") ? sel.strategy : {};
  const tags = Array.isArray(strategy.tags) ? strategy.tags : [];
  return {
    strategy: { kind: "tag", tags },
    include: (sel.include && typeof sel.include === "object") ? sel.include : {},
    exclude: (sel.exclude && typeof sel.exclude === "object") ? sel.exclude : {},
    dueOnly: !!sel.dueOnly,
  };
}

function makeLearning() {
  /* The whole page. The builder (selection + sources) renders into
  * #learning-builder, and a session player owns #learning-player
  */
  const S = {
    pid: "",
    sources: [],        // the workspace's sources, public shape
    tags: [],           // {key, name, count} — the tag vocabulary
    sel: loadSelection(),
    stats: {},          // sid -> stats blob, for the builder numbers
    statsKey: "",       // matched sid set S.stats was built for
  };

  let P = null;         // the live session, or null in the builder

  /* --- selection helpers --- */

  function save() {
    localStorage.setItem(SEL_KEY, JSON.stringify(S.sel));
  }

  function tagKeys(s) {
    /* Lowercased tags of one source, the vocabulary the chips are keyed by
    */
    return new Set((s.tags || "").split(",").map(t => t.trim().toLowerCase())
      .filter(Boolean));
  }

  function matches(s) {
    /* Whether a source carries every selected tag (AND). Empty selection
    * matches everything — the whole library is the default review set
    */
    const keys = S.sel.strategy.tags;
    if (!keys.length) return true;
    const stags = tagKeys(s);
    return keys.every(k => stags.has(k));
  }

  function playable(s) {
    return (s.num_questions || 0) > 0;
  }

  function byId() {
    return new Map(S.sources.map(s => [s.id, s]));
  }

  function included() {
    /* The sources a session would play: the strategy's match, plus anything
    * explicitly included, minus the explicit exclusions, minus the sources
    * that have no quiz at all
    */
    const map = byId();
    const ids = new Set();
    for (const s of S.sources) if (matches(s) && playable(s)) ids.add(s.id);
    for (const sid of Object.keys(S.sel.include)) {
      const s = map.get(sid);
      if (s && playable(s)) ids.add(sid);
    }
    for (const sid of Object.keys(S.sel.exclude)) ids.delete(sid);
    return [...ids].map(id => map.get(id)).filter(Boolean);
  }

  function builderRows() {
    /* Every source the selection speaks of, in title order: the matched ones
    * plus anything explicitly included. A zero-quiz row shows dimmed
    */
    const map = byId();
    const ids = new Set(S.sources.filter(matches).map(s => s.id));
    for (const sid of Object.keys(S.sel.include)) ids.add(sid);
    return [...ids].map(id => map.get(id)).filter(Boolean)
      .sort((a, b) => a.title.localeCompare(b.title));
  }

  function statsOf(sid) {
    return S.stats[sid] || { questions: {} };
  }

  function countDue(sid) {
    let n = 0;
    for (const qid in statsOf(sid).questions || {}) {
      if (isDue(statsOf(sid).questions[qid])) n++;
    }
    return n;
  }

  function countNeverPlayed(sid) {
    let n = 0;
    for (const qid in statsOf(sid).questions || {}) {
      if (!(statsOf(sid).questions[qid].times_played || 0)) n++;
    }
    return n;
  }

  /* --- data loading --- */

  function buildVocabulary() {
    /* The tag vocabulary of the workspace, deduplicated case-insensitively,
    * each with how many sources carry it
    */
    const seen = new Map(), count = new Map();
    for (const s of S.sources) {
      for (const t of (s.tags || "").split(",")) {
        const name = t.trim();
        if (!name) continue;
        const key = name.toLowerCase();
        if (!seen.has(key)) seen.set(key, name);
        count.set(key, (count.get(key) || 0) + 1);
      }
    }
    S.tags = [...seen].map(([key, name]) => ({ key, name, count: count.get(key) }))
      .sort((a, b) => a.key.localeCompare(b.key));
  }

  function reconcileSelection() {
    /* Drops tags and source ids that the live workspace no longer knows
    */
    const keys = new Set(S.tags.map(t => t.key));
    S.sel.strategy.tags = S.sel.strategy.tags.filter(k => keys.has(k));
    const ids = new Set(S.sources.map(s => s.id));
    for (const side of ["include", "exclude"]) {
      S.sel[side] = Object.fromEntries(
        Object.keys(S.sel[side]).filter(id => ids.has(id)).map(id => [id, true]));
    }
    save();
  }

  async function refreshStats() {
    /* Fetches the per-source stats blobs (the builder's due/never-played
    * numbers) once per matched set. A source that fails to load (deleted
    * elsewhere, say) reads as an empty blob
    */
    const sids = S.sources.filter(matches).map(s => s.id).sort().join("|");
    if (sids === S.statsKey) return;
    S.statsKey = sids;
    S.stats = {};
    const list = sids ? sids.split("|") : [];
    if (!list.length) return;
    const fetched = await chunked(list, sid =>
      api(`/api/quiz/${sid}/stats`)
        .then(d => ({ sid, stats: d.stats || {} }))
        .catch(() => ({ sid, stats: {} })));
    for (const { sid, stats } of fetched) S.stats[sid] = stats;
  }

  /* --- rendering --- */

  function emptyNote(text) {
    return `<p class="muted lb-empty">${text}</p>`;
  }

  function renderTags() {
    const host = $("#lb-tags");
    if (!S.tags.length) {
      host.innerHTML = emptyNote("No tags in your library yet — tag sources in "
        + "the Library to filter by tag.");
      return;
    }
    const on = new Set(S.sel.strategy.tags);
    host.innerHTML = S.tags.map(t =>
      `<button type="button" class="lb-tag${on.has(t.key) ? " on" : ""}" data-key="${t.key}">`
      + `${escapeHtml(t.name)} <span class="n">${t.count}</span></button>`).join("");
    $$(".lb-tag", host).forEach(b => {
      b.addEventListener("click", () => toggleTag(b.dataset.key));
    });
  }

  function renderAggregate() {
    const inc = included();
    const questions = inc.reduce((n, s) => n + (s.num_questions || 0), 0);
    const due = inc.reduce((n, s) => n + countDue(s.id), 0);
    const never = inc.reduce((n, s) => n + countNeverPlayed(s.id), 0);
    $("#lb-aggregate").innerHTML =
      `<b>${inc.length}</b> sources · <b>${questions}</b> questions · `
      + `<b>${due}</b> due now · <b>${never}</b> never played`;
  }

  function renderSources() {
    const list = $("#lb-sources-list");
    const inc = new Set(included().map(s => s.id));
    const rows = builderRows();
    if (!rows.length) {
      list.innerHTML = S.sources.length
        ? emptyNote("No sources match this selection — pick another tag.")
        : emptyNote('Your library is empty — import sources from the Library '
          + "tab, then generate quizzes there.");
    } else {
      list.innerHTML = rows.map(s => {
        const dim = !playable(s);
        const checked = inc.has(s.id) && !dim;
        const meta = dim
          ? '<span class="chip" title="Generate a quiz in the Library first">no quiz yet</span>'
          : `${s.num_questions}Q · ${countDue(s.id)} due`;
        return `<label class="lb-src${dim ? " dim" : ""}">`
          + `<input type="checkbox" class="lb-src-check" data-sid="${s.id}"`
          + `${checked ? " checked" : ""}${dim ? " disabled" : ""}>`
          + `<span class="lb-src-title">${escapeHtml(s.title)} ${typePill(s.source_type)}</span>`
          + `<span class="lb-src-meta">${meta}</span></label>`;
      }).join("");
    }
    $$(".lb-src-check", list).forEach(cb => {
      cb.addEventListener("change", () => toggleSource(cb.dataset.sid, cb.checked));
    });
    renderFoot();
  }

  function renderFoot() {
    /* The "only due" switch state, the caught-up note, and the Start button
    */
    const inc = included();
    const due = inc.reduce((n, s) => n + countDue(s.id), 0);
    $("#lb-due-only").checked = S.sel.dueOnly;
    const caught = $("#lb-caughtup");
    caught.textContent = (S.sel.dueOnly && inc.length && !due)
      ? "All caught up — nothing due in this selection."
      : "";
    $("#lb-start").disabled = !inc.length || (S.sel.dueOnly && !due);
    $("#lb-start").title = (S.sel.dueOnly && inc.length && !due)
      ? "Nothing is due for review" : "";
  }

  function renderBuilder() {
    renderTags();
    renderAggregate();
    renderSources();
  }

  /* --- interactions --- */

  function toggleTag(key) {
    const tags = S.sel.strategy.tags;
    const i = tags.indexOf(key);
    if (i >= 0) tags.splice(i, 1); else tags.push(key);
    save();
    onSelection();
  }

  function toggleSource(sid, checked) {
    if (checked) delete S.sel.exclude[sid];
    else S.sel.exclude[sid] = true;
    save();
    renderAggregate();
    renderSources();
  }

  async function onSelection() {
    await refreshStats();
    renderBuilder();
  }

  /** --- the session --- */

  function makeSession(base) {
    /* Fresh player state. base is the {sid, title, q} queue built once; a
    * restart re-runs the same questions, exactly like the old per-source
    * player's "Play again"
    */
    return {
      base: [...base],
      queue: [...base],
      answered: 0,
      correct: 0,
      requeued: new Set(),
      per: {},          // sid -> {title, total, correct}
    };
  }

  async function startSession() {
    /* Builds the queue from every included source's review order and flips
    * the page to the player. The due switch fetches fresh stats and drops
    * the questions that are not due yet; the server's order is filtered,
    * never re-sorted
    */
    const inc = included();
    if (!inc.length || P) return;

    const orders = {};
    const fresh = {};
    $("#lb-start").disabled = true;
    await Promise.all([
      chunked(inc, async s => {
        try { orders[s.id] = (await api(`/api/quiz/${s.id}/order`)).questions; }
        catch (_) { orders[s.id] = []; }
      }),
      S.sel.dueOnly
        ? chunked(inc, async s => {
            try { fresh[s.id] = (await api(`/api/quiz/${s.id}/stats`)).stats || {}; }
            catch (_) { fresh[s.id] = {}; }
          })
        : Promise.resolve(),
    ]);

    const dueOf = sid => {
      const st = S.sel.dueOnly ? (fresh[sid] || {}) : (S.stats[sid] || {});
      let n = 0;
      for (const qid in (st.questions || {})) if (isDue(st.questions[qid])) n++;
      return n;
    };

    const sources = [...inc].sort((a, b) => {
      const d = dueOf(b.id) - dueOf(a.id);
      return d || a.title.localeCompare(b.title);
    });

    const queue = [];
    for (const s of sources) {
      let qs = orders[s.id] || [];
      if (S.sel.dueOnly) {
        const qst = (fresh[s.id] || { questions: {} }).questions || {};
        qs = qs.filter(q => isDue(qst[q.id]));
      }
      for (const q of qs) queue.push({ sid: s.id, title: s.title, q });
    }
    if (!queue.length) {
      toast("All caught up — nothing due in this selection", "warn");
      return;
    }
    P = makeSession(queue);
    swapView("player");
    renderPlayer();
  }

  function renderPlayer() {
    $("#lp-abort").classList.remove("hidden");
    $("#lp-source-title").textContent = "";
    $("#lp-score").textContent = "";
    renderQuestion();
  }

  function swapView(name) {
    $("#learning-builder").classList.toggle("hidden", name === "player");
    $("#learning-player").classList.toggle("hidden", name === "builder");
  }

  function answer(q, chosen, btn) {
    /* Marks the answer, requeues a missed question once, and reports the
    * result to the server under the question's own source
    */
    const ok = chosen.idx === q.q.answer_index;
    P.answered++;
    if (ok) P.correct++;
    else if (!P.requeued.has(q.q.id)) { P.requeued.add(q.q.id); P.queue.push(q); }
    const per = P.per[q.sid] || (P.per[q.sid] = { title: q.title, total: 0, correct: 0 });
    per.total++;
    if (ok) per.correct++;

    const body = $("#lp-body");
    $$(".ans", body).forEach(b => { b.disabled = true; });
    $$(".ans", body).forEach(b => {
      if (+b.dataset.idx === q.q.answer_index) b.classList.add("correct");
      else if (b === btn) b.classList.add("wrong");
    });
    const fb = $(".quiz-feedback", body);
    fb.textContent = ok ? "✓ Correct"
      : `✗ Incorrect — correct answer: ${q.q.answers[q.q.answer_index]}`;
    fb.className = "quiz-feedback " + (ok ? "ok" : "no");
    $(".lb-next", body).classList.remove("hidden");

    $("#lp-progress").textContent = `${Math.min(P.answered, P.base.length)} / ${P.base.length}`;
    $("#lp-score").textContent = `success: ${P.correct}`;
    api(`/api/quiz/${q.sid}/answer`, {
      method: "POST",
      body: { question_id: q.q.id, success: ok },
    }).catch(() => {});
  }

  function renderQuestion() {
    if (!P.queue.length) return renderSummary();
    const q = P.queue.shift();

    const opts = q.q.answers.map((text, idx) => ({ text, idx }));
    for (let i = opts.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [opts[i], opts[j]] = [opts[j], opts[i]];
    }

    $("#lp-source-title").textContent = q.title;
    $("#lp-progress").textContent =
      `${Math.min(P.answered + 1, P.base.length)} / ${P.base.length}`;
    $("#lp-score").textContent = `success: ${P.correct}`;

    const body = $("#lp-body");
    body.innerHTML = `
      <h3 class="quiz-question">${escapeHtml(q.q.question)}</h3>
      <div class="quiz-answers"></div>
      <div class="quiz-footer">
        <span class="quiz-feedback"></span>
        <button class="btn hidden lb-next" id="lp-next">Next</button>
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
    $(".lb-next", body).addEventListener("click", renderQuestion);
  }

  function renderSummary() {
    /* Final score, the per-source breakdown, and the way back
    */
    const pct = Math.round(100 * P.correct / Math.max(P.answered, 1));
    const rows = Object.values(P.per).map(p =>
      `<tr><td class="lb-src-name">${escapeHtml(p.title)}</td>`
      + `<td class="lb-src-score">${p.correct} / ${p.total}</td>`
      + `<td class="lb-src-pct">${Math.round(100 * p.correct / Math.max(p.total, 1))}%</td></tr>`
    ).join("");
    $("#lp-source-title").textContent = "";
    $("#lp-progress").textContent = `${P.answered} / ${P.base.length}`;
    $("#lp-score").textContent = `success: ${P.correct}`;
    $("#lp-abort").classList.add("hidden");
    $("#lp-body").innerHTML = `
      <div class="quiz-summary">
        <h3>Session finished</h3>
        <p class="big">${P.correct} / ${P.answered} correct (${pct}%)</p>
        <p class="muted">Answers are stored per source — questions you missed
          come up for review sooner.</p>
        ${rows ? `<table class="learning-breakdown">
          <thead><tr><th>Source</th><th>Correct</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table>` : ""}
        <button class="btn primary" id="lp-again">Play again</button>
        <button class="btn" id="lp-back">Back to builder</button>
      </div>`;
    $("#lp-again").addEventListener("click", () => {
      P = makeSession(P.base);
      $("#lp-abort").classList.remove("hidden");
      renderPlayer();
    });
    $("#lp-back").addEventListener("click", toBuilder);
  }

  function toBuilder() {
    P = null;
    swapView("builder");
  }

  async function abort() {
    /* Leaves the session. With answers recorded it asks first, since those
    * answers are already stored and the run cannot be taken back
    */
    if (!P) return toBuilder();
    if (P.answered && !await confirmModal({
      title: "Abandon the session?",
      body: "Stop here and go back to the selection?",
      hint: "The answers already given are stored.",
      confirm: "Abandon session",
      danger: true,
    })) return;
    toBuilder();
  }

  /* --- wiring --- */

  $("#lb-due-only").addEventListener("change", e => {
    S.sel.dueOnly = e.target.checked;
    save();
    renderFoot();
    renderAggregate();
  });
  $("#lb-start").addEventListener("click", startSession);
  $("#lp-abort").addEventListener("click", abort);

  /* --- boot --- */

  async function init() {
    /* Identity first: the workspace id comes with it. Then the library, the
    * vocabulary, and the saved selection reconciled against both
    */
    await initIdentity();
    S.pid = (KNDB.personal && KNDB.personal.id) || "";
    if (!S.pid) {
      toast("Could not reach the server — reload to try again", "err", 9000);
      return;
    }
    let data = null;
    try {
      data = await api(`/api/sources?project=${encodeURIComponent(S.pid)}`);
    } catch (e) {
      toast("Could not load your library: " + e.message, "err", 9000);
      return;
    }
    S.sources = data.sources || [];
    buildVocabulary();
    reconcileSelection();
    await refreshStats();
    renderBuilder();
  }

  init();
}

initIdentity().then(makeLearning);