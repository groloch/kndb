"use strict";

/* The kanban board: one project's columns and cards, drawn on its Board tab.
 *
 * The page owns where the board lives and its permissions; this module owns
 * the board itself — rendering, drag & drop, the card editor modal, archiving.
 * Drag & drop is native HTML5, so nothing here needs a vendored library.
 * The server is the authority: failed writes refetch the board, and a local
 * reorder only ever happens optimistically, right before its own fetch.
 *
 * cfg = {
 *   projectId: () => pid,
 *   readOnly:  () => bool,           // hide the composer and the drag handles
 *   members:   () => [{name, role, color}],
 *   sources:   () => [source rows],  // for the card's source select
 *   onOpenSource: sid => …,          // the card pill's click-through
 * }
 */

const COLUMN_COLORS = ["", "#ef4444", "#f59e0b", "#10b981", "#3b82f6",
                       "#8b5cf6", "#ec4899", "#14b8a6"];

function makeBoard(cfg) {
  const pid = () => (cfg.projectId ? cfg.projectId() : "");
  const ro = () => (cfg.readOnly ? cfg.readOnly() : false);
  const members = () => (cfg.members ? cfg.members() : []);
  const sources = () => (cfg.sources ? cfg.sources() : []);
  const colorOf = name => blameColor(Object.fromEntries(
    members().map(m => [m.name, m.color || ""])), name);

  const B = {
    columns: [], cards: [], archived: [],
    filter: "", mineOnly: false, showArchived: false,
  };

  // Editor and drag state — transient, not part of the board.
  const U = {
    col: null,         // the column preset for a new card
    editId: null,      // the card being edited, null = create
    pendingLabels: [],
    readOnly: false,   // fixed when the editor modal opens
    dragging: null,    // {kind:"col"|"card", id} while a pointer is down
    dropTarget: null,  // {colId, position} or {at}, computed during dragover
    menuCol: null,     // the column whose ⋯ menu is open
  };

  function todayStr() {
    return new Date().toISOString().slice(0, 10);
  }

  function fmtDue(d) {
    if (!d) return "";
    const dt = new Date(d + "T00:00:00");
    return isNaN(dt) ? d : dt.toLocaleDateString(undefined,
      { month: "short", day: "numeric" });
  }

  function isOverdue(card) {
    return !!card.due_date && card.due_date < todayStr();
  }

  function cardsIn(colId) {
    return B.cards.filter(c => c.column_id === colId);
  }

  function cardById(id) {
    return B.cards.find(c => c.id === id) || B.archived.find(c => c.id === id);
  }

  function matches(card) {
    if (B.mineOnly && !card.assignees.includes(KNDB.user)) return false;
    const q = B.filter.trim().toLowerCase();
    if (!q) return true;
    return [card.title, ...(card.labels || []), ...(card.assignees || [])]
      .join(" ").toLowerCase().includes(q);
  }

  /* ---- fetching ---------------------------------------------------------- */

  async function refresh() {
    let d;
    try {
      d = await api(`/api/projects/${pid()}/board`);
    } catch (e) {
      toast("Failed to load the board: " + e.message, "err");
      return false;
    }
    B.columns = d.board.columns;
    B.cards = d.board.cards;
    B.archived = d.board.archived;
    render();
    return true;
  }

  function open() {
    refresh();
  }

  /* ---- rendering --------------------------------------------------------- */

  function render() {
    const host = $("#board");
    if (!host) return;
    const readonly = ro();
    const addBtn = $("#btn-add-column");
    if (addBtn) addBtn.disabled = readonly;
    host.innerHTML = "";
    if (!B.columns.length) {
      const el = document.createElement("div");
      el.className = "board-empty";
      el.innerHTML = readonly
        ? '<p class="muted">No columns yet.</p>'
        : '<p class="muted">No columns yet — start your workflow with <b>+ Column</b> above.</p>';
      host.appendChild(el);
    } else {
      for (const col of B.columns) host.appendChild(renderColumn(col, readonly));
    }
    renderArchivedStrip();
  }

  function renderColumn(col, readonly) {
    const el = document.createElement("div");
    el.className = "board-col";
    el.dataset.id = col.id;
    if (col.color) {
      el.classList.add("colored");
      el.style.setProperty("--col-accent", col.color);
    }

    const head = document.createElement("div");
    head.className = "col-head";
    let grip = null;
    if (!readonly) {
      grip = document.createElement("span");
      grip.className = "col-grip";
      grip.title = "Drag to reorder";
      grip.textContent = "⠿";
      grip.addEventListener("dragstart", e => colDragStart(e, col));
      head.appendChild(grip);
    }
    const name = document.createElement("span");
    name.className = "col-name";
    name.textContent = col.name;
    name.title = col.name;
    head.appendChild(name);

    const cnt = document.createElement("span");
    cnt.className = "col-count";
    if (col.wip_limit > 0) {
      if (col.card_count >= col.wip_limit) cnt.classList.add("wip-over");
      cnt.textContent = col.card_count + " / " + col.wip_limit;
      cnt.title = "WIP limit: " + col.wip_limit;
    } else {
      cnt.textContent = String(col.card_count);
    }
    head.appendChild(cnt);

    if (!readonly) {
      const menu = document.createElement("button");
      menu.className = "col-menu";
      menu.type = "button";
      menu.textContent = "⋯";
      menu.title = "Column options";
      menu.addEventListener("click", e => {
        e.stopPropagation();
        U.menuCol = U.menuCol === col.id ? null : col.id;
        render();
      });
      head.appendChild(menu);
    }
    if (U.menuCol === col.id) head.appendChild(columnMenu(col));

    const body = document.createElement("div");
    body.className = "col-cards";
    body.dataset.col = col.id;
    const visible = cardsIn(col.id).filter(matches);
    for (const card of visible) body.appendChild(renderCard(card, readonly));
    if (!visible.length) body.appendChild(columnHint(col));
    el.addEventListener("dragover", e => {
      if (!U.dragging || U.dragging.kind !== "card") return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      computeCardDrop(e, body, col.id);
    });
    el.addEventListener("drop", e => {
      if (!U.dragging || U.dragging.kind !== "card") return;
      e.preventDefault();
      const drop = U.dropTarget;
      const cardId = U.dragging.id;
      cardDragEnd();
      if (drop) cardDrop(cardId, drop.colId, drop.position);
    });

    el.appendChild(head);
    el.appendChild(body);
    if (!readonly) {
      const add = document.createElement("div");
      add.className = "col-add";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "+ Add card";
      btn.addEventListener("click", () => openCardModal(col.id, null));
      add.appendChild(btn);
      el.appendChild(add);
    }
    return el;
  }

  function columnHint(col) {
    const hint = document.createElement("div");
    hint.className = "col-empty";
    hint.textContent = !cardsIn(col.id).length ? "Drop cards here"
      : "No matching cards";
    return hint;
  }

  function columnMenu(col) {
    const pop = document.createElement("div");
    pop.className = "col-menu-pop";
    for (const [action, label, danger] of [
      ["rename", "Rename…", false],
      ["wip", "WIP limit…", false],
      ["color", "Colour…", false],
      ["delete", "Delete column", true],
    ]) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "col-menu-item" + (danger ? " danger" : "");
      b.dataset.action = action;
      b.textContent = label;
      pop.appendChild(b);
    }
    const swatches = document.createElement("div");
    swatches.className = "col-swatches";
    for (const c of COLUMN_COLORS) {
      const s = document.createElement("button");
      s.type = "button";
      s.className = "swatch" + (c ? "" : " none");
      s.dataset.color = c;
      s.title = c ? c : "No accent";
      if (c) s.style.background = c;
      swatches.appendChild(s);
    }
    pop.appendChild(swatches);
    pop.addEventListener("click", e => {
      const sw = e.target.closest("[data-color]");
      if (sw) return void setColumnColor(col, sw.dataset.color);
      const item = e.target.closest(".col-menu-item");
      if (!item) return;
      const act = item.dataset.action;
      if (act === "color") return;
      U.menuCol = null;
      render();
      if (act === "rename") renameColumn(col);
      else if (act === "wip") wipColumn(col);
      else if (act === "delete") deleteColumn(col);
    });
    return pop;
  }

  function renderCard(card, readonly) {
    const el = document.createElement("div");
    el.className = "board-card";
    el.dataset.id = card.id;
    if (isOverdue(card)) el.classList.add("overdue");
    if (readonly) {
      el.addEventListener("click", () => openCardModal(card.column_id, card));
    } else {
      el.draggable = true;
      el.addEventListener("click", () => openCardModal(card.column_id, card));
      el.addEventListener("dragstart", e => cardDragStart(e, card));
      el.addEventListener("dragend", cardDragEnd);
    }

    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = card.title;
    title.title = card.title;
    el.appendChild(title);

    if (card.labels.length) {
      const row = document.createElement("div");
      row.className = "card-tags";
      for (const l of card.labels) {
        const t = document.createElement("span");
        t.className = "card-tag";
        t.textContent = "@" + l;
        row.appendChild(t);
      }
      el.appendChild(row);
    }

    const meta = document.createElement("div");
    meta.className = "card-meta-row";
    if (card.assignees.length) {
      const dots = document.createElement("span");
      dots.className = "card-assignees";
      dots.title = card.assignees.join(", ");
      for (const a of card.assignees) {
        const d = document.createElement("span");
        d.className = "assignee-dot";
        d.style.background = colorOf(a);
        dots.appendChild(d);
      }
      meta.appendChild(dots);
    }
    if (card.due_date) {
      const due = document.createElement("span");
      due.className = "card-due" + (isOverdue(card) ? " due-over" : "");
      due.textContent = fmtDue(card.due_date);
      due.title = "Due " + card.due_date + (isOverdue(card) ? " — overdue" : "");
      meta.appendChild(due);
    }
    if (card.source_id) {
      const src = sources().find(s => s.id === card.source_id);
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "card-pill";
      pill.textContent = "📄 " + (src ? src.title : card.source_id);
      pill.title = src ? "Open in the Library" : "Source no longer linked";
      pill.addEventListener("click", e => {
        e.stopPropagation();
        if (src && cfg.onOpenSource) cfg.onOpenSource(card.source_id);
        else openCardModal(card.column_id, card);
      });
      meta.appendChild(pill);
    }
    if (meta.children.length) el.appendChild(meta);

    if (card.created_by || card.updated_at) {
      const foot = document.createElement("div");
      foot.className = "card-foot";
      foot.textContent = (card.created_by ? "by " + card.created_by : "")
        + (card.updated_at ? " · " + fmtDate(card.updated_at) : "");
      el.appendChild(foot);
    }
    return el;
  }

  function renderArchivedStrip() {
    const strip = $("#board-archived");
    if (!strip) return;
    if (!B.showArchived || !B.archived.length) {
      strip.classList.add("hidden");
      return;
    }
    strip.classList.remove("hidden");
    strip.innerHTML = '<div class="arch-head">Archived — ' + B.archived.length
      + (B.archived.length === 1 ? " card" : " cards") + "</div>";
    for (const card of B.archived) strip.appendChild(renderArchivedCard(card));
  }

  function renderArchivedCard(card) {
    const el = document.createElement("div");
    el.className = "board-card archived";
    el.dataset.id = card.id;
    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = card.title;
    el.appendChild(title);
    if (card.labels.length) {
      const row = document.createElement("div");
      row.className = "card-tags";
      for (const l of card.labels) {
        const t = document.createElement("span");
        t.className = "card-tag";
        t.textContent = "@" + l;
        row.appendChild(t);
      }
      el.appendChild(row);
    }
    if (card.assignees.length) {
      const meta = document.createElement("div");
      meta.className = "card-meta-row";
      const dots = document.createElement("span");
      dots.className = "card-assignees";
      for (const a of card.assignees) {
        const d = document.createElement("span");
        d.className = "assignee-dot";
        d.style.background = colorOf(a);
        dots.appendChild(d);
      }
      meta.appendChild(dots);
      el.appendChild(meta);
    }
    if (ro()) return el;
    const act = document.createElement("div");
    act.className = "arch-actions";
    const rest = document.createElement("button");
    rest.type = "button";
    rest.className = "btn small";
    rest.textContent = "Restore";
    rest.addEventListener("click", () => setArchived(card.id, false));
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn small danger";
    del.textContent = "Delete";
    del.addEventListener("click", () => deleteCard(card));
    act.appendChild(rest);
    act.appendChild(del);
    el.appendChild(act);
    return el;
  }

  /* ---- card drag & drop -------------------------------------------------- */

  function cardDragStart(e, card) {
    U.dragging = { kind: "card", id: card.id };
    U.dropTarget = null;
    e.dataTransfer.setData("text/plain", card.id);
    e.dataTransfer.effectAllowed = "move";
    e.target.classList.add("drag-src");
  }

  function cardDragEnd() {
    U.dragging = null;
    U.dropTarget = null;
    $$(".board-card").forEach(el => el.classList.remove(
      "drop-before", "drop-after", "drag-src"));
    $$(".col-cards").forEach(el => el.classList.remove("drag-over"));
  }

  function computeCardDrop(e, body, colId) {
    // The visible cards are a subsequence of the column's server order, so the
    // drop position is the index of the first card *below* the pointer in that
    // full order, never the visible index — hidden cards count too.
    const order = cardsIn(colId).filter(c => c.id !== U.dragging.id).map(c => c.id);
    let pos = order.length;
    $$(".col-cards").forEach(el => el.classList.remove("drag-over"));
    for (const cardEl of $$(".board-card", body)) {
      if (cardEl.dataset.id === U.dragging.id) continue;
      const r = cardEl.getBoundingClientRect();
      if (!r.height) continue;
      if (e.clientY < r.top + r.height / 2) {
        cardEl.classList.add("drop-before");
        const i = order.indexOf(cardEl.dataset.id);
        if (i >= 0) pos = Math.min(pos, i);
      } else {
        cardEl.classList.add("drop-after");
      }
    }
    body.classList.add("drag-over");
    U.dropTarget = { colId, position: pos };
  }

  async function cardDrop(cardId, colId, position) {
    applyLocalMove(cardId, colId, position);
    render();
    try {
      await api(`/api/projects/${pid()}/board/move`, {
        method: "POST",
        body: { card_id: cardId, column_id: colId, position },
      });
    } catch (e) {
      toast("Move failed: " + e.message, "err", 6000);
      await refresh();   // the server's truth wins over the optimistic move
    }
  }

  function applyLocalMove(cardId, colId, position) {
    const card = B.cards.find(c => c.id === cardId);
    if (!card) return;
    const rest = B.cards.filter(c => c.id !== cardId);
    const out = [];
    let seen = 0, inserted = false;
    for (const c of rest) {
      if (c.column_id === colId) {
        if (seen === position && !inserted) {
          out.push(card);
          inserted = true;
        }
        seen++;
      }
      out.push(c);
    }
    if (!inserted) out.push(card);
    card.column_id = colId;
    const posOf = {};
    for (const cid of new Set(out.map(c => c.column_id))) {
      let i = 0;
      for (const c of out) if (c.column_id === cid) posOf[c.id] = i++;
    }
    for (const c of out) c.position = posOf[c.id];
    B.cards = out;
    // The server's per-column counts follow the optimistic move too.
    for (const col of B.columns) {
      col.card_count = B.cards.filter(c => c.column_id === col.id).length;
    }
  }

  /* ---- column drag & drop ------------------------------------------------ */

  function colDragStart(e, col) {
    U.dragging = { kind: "col", id: col.id };
    U.dropTarget = null;
    e.dataTransfer.setData("text/plain", col.id);
    e.dataTransfer.effectAllowed = "move";
  }

  function colDragOver(e) {
    if (!U.dragging || U.dragging.kind !== "col") return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const cols = $$(".board-col");
    clearColHints();
    let at = cols.length;
    for (let i = 0; i < cols.length; i++) {
      const r = cols[i].getBoundingClientRect();
      if (e.clientX < r.left + r.width / 2) {
        at = i;
        cols[i].classList.add("col-drop-l");
        break;
      }
      cols[i].classList.add("col-drop-r");
    }
    U.dropTarget = { at };
  }

  function colDropHandler(e) {
    if (!U.dragging || U.dragging.kind !== "col") return;
    e.preventDefault();
    const at = U.dropTarget ? U.dropTarget.at : null;
    const dragId = U.dragging.id;
    colDragEnd();
    if (at !== null) colDrop(dragId, at);
  }

  function clearColHints() {
    $$(".board-col").forEach(el => el.classList.remove("col-drop-l", "col-drop-r"));
  }

  function colDragEnd() {
    U.dragging = null;
    U.dropTarget = null;
    clearColHints();
  }

  async function colDrop(colId, at) {
    const order = B.columns.map(c => c.id);
    const from = order.indexOf(colId);
    order.splice(from, 1);
    // Dragging rightwards needs one fewer after the removal shifts things.
    order.splice(Math.max(0, from < at ? at - 1 : at), 0, colId);
    const moved = B.columns.find(c => c.id === colId);
    if (!moved) return;
    B.columns.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
    for (let i = 0; i < B.columns.length; i++) B.columns[i].position = i;
    render();
    try {
      await api(`/api/projects/${pid()}/board/columns/order`, {
        method: "POST",
        body: { column_ids: order },
      });
    } catch (e) {
      toast("Column move failed: " + e.message, "err");
      await refresh();
    }
  }

  /* ---- column actions ---------------------------------------------------- */

  async function addColumn() {
    const name = await askModal({
      title: "New column",
      label: "Column name",
      submit: "Add column",
      hint: "e.g. Backlog, Doing, Done.",
    });
    if (!name) return;
    try {
      await api(`/api/projects/${pid()}/board/columns`, {
        method: "POST", body: { name },
      });
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  async function renameColumn(col) {
    const name = await askModal({
      title: "Rename column",
      label: "Column name",
      value: col.name,
      submit: "Rename",
    });
    if (!name || name === col.name) return;
    try {
      await api(`/api/projects/${pid()}/board/columns/${col.id}`, {
        method: "PATCH", body: { name },
      });
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  async function wipColumn(col) {
    const v = await askModal({
      title: "WIP limit",
      label: "Max cards before warning",
      value: col.wip_limit ? String(col.wip_limit) : "0",
      submit: "Set limit",
      hint: "0 means no limit. The board warns when a column is at or over it.",
    });
    if (v === null) return;
    const wip = parseInt(v, 10);
    if (isNaN(wip) || wip < 0) {
      toast("A WIP limit is a number from 0 up", "err");
      return;
    }
    try {
      await api(`/api/projects/${pid()}/board/columns/${col.id}`, {
        method: "PATCH", body: { wip_limit: wip },
      });
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  async function setColumnColor(col, color) {
    U.menuCol = null;
    render();
    try {
      await api(`/api/projects/${pid()}/board/columns/${col.id}`, {
        method: "PATCH", body: { color },
      });
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  async function deleteColumn(col) {
    if (!await confirmModal({
      title: "Delete column",
      body: 'Delete the column "' + col.name + '"?',
      hint: "The cards in it are deleted with it. The last column cannot be deleted.",
      confirm: "Delete column",
      danger: true,
    })) return;
    try {
      await api(`/api/projects/${pid()}/board/columns/${col.id}`, {
        method: "DELETE",
      });
      await refresh();
    } catch (e) { toast(e.message, "err", 6000); }
  }

  /* ---- the card editor modal --------------------------------------------- */

  function openCardModal(colId, card) {
    U.col = colId;
    U.editId = card ? card.id : null;
    U.pendingLabels = card ? (card.labels || []).slice() : [];
    U.readOnly = ro();
    $("#card-modal-title").textContent = card
      ? (U.readOnly ? "Card" : "Edit card") : "New card";
    $("#card-title").value = card ? card.title : "";
    $("#card-title").disabled = U.readOnly;
    $("#card-desc").value = card ? card.description : "";
    $("#card-desc").disabled = U.readOnly;
    $("#card-due").value = card ? card.due_date : "";
    $("#card-due").disabled = U.readOnly;
    const sel = $("#card-source");
    sel.innerHTML = '<option value="">— none —</option>'
      + sources().map(s => '<option value="' + escapeHtml(s.id) + '"'
        + (card && card.source_id === s.id ? " selected" : "") + ">"
        + escapeHtml(s.title) + "</option>").join("");
    sel.disabled = U.readOnly;
    $("#card-label-input").disabled = U.readOnly;
    $("#btn-card-label").disabled = U.readOnly;
    $("#btn-card-arch").classList.toggle("hidden", U.readOnly || !card);
    $("#btn-card-arch").textContent = card && card.archived ? "Restore" : "Archive";
    $("#btn-card-delete").classList.toggle("hidden", U.readOnly || !card);
    drawLabels();
    drawAssignees(card ? card.assignees : []);
    openModal("modal-card");
  }

  function drawLabels() {
    const list = $("#card-labels");
    if (!list) return;
    if (!U.pendingLabels.length) {
      list.innerHTML = '<p class="muted">No labels yet.</p>';
      return;
    }
    list.innerHTML = U.pendingLabels.map(l =>
      '<span class="tag-chip">'
      + (U.readOnly ? "" : '<button class="tag-x" type="button" title="Remove label">×</button>')
      + "<span>@" + escapeHtml(l) + "</span></span>").join("");
    $$(".tag-chip", list).forEach((chip, i) => { chip.dataset.tag = U.pendingLabels[i]; });
  }

  function drawAssignees(selected) {
    const list = $("#card-assignees");
    if (!list) return;
    const rows = members();
    if (!rows.length) {
      list.innerHTML = '<p class="muted">No members yet — add them in Settings.</p>';
      return;
    }
    list.innerHTML = rows.map(m =>
      '<label class="check-row">'
      + '<input type="checkbox" value="' + escapeHtml(m.name) + '"'
      + (selected.includes(m.name) ? " checked" : "")
      + (U.readOnly ? " disabled" : "") + ">"
      + '<span class="dot" style="background:' + escapeHtml(m.color || "") + '"></span>'
      + '<span class="row-name">' + escapeHtml(m.name) + "</span></label>").join("");
  }

  function addLabel() {
    const input = $("#card-label-input");
    if (!input) return;
    const v = input.value.trim().replace(/^@/, "");
    if (!v) return;
    if (U.pendingLabels.some(l => l.toLowerCase() === v.toLowerCase())) {
      toast("Already labelled @" + v, "warn");
      return;
    }
    input.value = "";
    U.pendingLabels.push(v);
    drawLabels();
  }

  async function saveCard() {
    const title = $("#card-title").value.trim();
    if (!title) { toast("A card needs a title", "warn"); return; }
    const body = {
      title,
      description: $("#card-desc").value.trim(),
      labels: U.pendingLabels,
      assignees: $$("#card-assignees input:checked").map(i => i.value),
      due_date: $("#card-due").value,
      source_id: $("#card-source").value,
    };
    try {
      if (U.editId) {
        await api(`/api/projects/${pid()}/board/cards/${U.editId}`,
          { method: "PATCH", body });
      } else {
        await api(`/api/projects/${pid()}/board/cards`,
          { method: "POST", body: { ...body, column_id: U.col } });
      }
      closeModal("modal-card");
      await refresh();
    } catch (e) {
      toast("Could not save the card: " + e.message, "err", 6000);
    }
  }

  async function toggleArchive() {
    const card = cardById(U.editId);
    if (!card) return;
    try {
      await api(`/api/projects/${pid()}/board/cards/${U.editId}`,
        { method: "PATCH", body: { archived: !card.archived } });
      closeModal("modal-card");
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  async function setArchived(cardId, archived) {
    try {
      await api(`/api/projects/${pid()}/board/cards/${cardId}`,
        { method: "PATCH", body: { archived } });
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  async function deleteCard(card) {
    if (!card) return;
    if (!await confirmModal({
      title: "Delete card",
      body: 'Delete "' + card.title + '" forever?',
      hint: "Archive moves it out of the flow instead; this is the permanent removal.",
      confirm: "Delete",
      danger: true,
    })) return;
    try {
      await api(`/api/projects/${pid()}/board/cards/${card.id}`,
        { method: "DELETE" });
      closeModal("modal-card");
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  /* ---- wiring ------------------------------------------------------------- */

  function wireToolbar() {
    const search = $("#board-search");
    if (search) search.addEventListener("input", e => {
      B.filter = e.target.value;
      render();
    });
    const mine = $("#board-mine");
    if (mine) mine.addEventListener("change", e => {
      B.mineOnly = e.target.checked;
      render();
    });
    const arch = $("#board-arch");
    if (arch) arch.addEventListener("change", e => {
      B.showArchived = e.target.checked;
      render();
    });
    const addBtn = $("#btn-add-column");
    if (addBtn) addBtn.addEventListener("click", addColumn);
  }

  function wireCardModal() {
    $("#card-save").addEventListener("click", saveCard);
    $("#btn-card-arch").addEventListener("click", toggleArchive);
    $("#btn-card-delete").addEventListener("click", () =>
      deleteCard(cardById(U.editId)));
    $("#card-title").addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); saveCard(); }
    });
    const labelInput = $("#card-label-input");
    labelInput.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); addLabel(); }
    });
    $("#btn-card-label").addEventListener("click", addLabel);
    $("#card-labels").addEventListener("click", e => {
      const x = e.target.closest(".tag-x");
      if (!x) return;
      U.pendingLabels = U.pendingLabels.filter(l => l !== x.closest(".tag-chip").dataset.tag);
      drawLabels();
    });
  }

  document.addEventListener("click", e => {
    if (e.target.closest(".col-menu, .col-menu-pop")) return;
    if (U.menuCol) {
      U.menuCol = null;
      render();
    }
  });

  const host = $("#board");
  if (host) {
    host.addEventListener("dragover", colDragOver);
    host.addEventListener("drop", colDropHandler);
  }
  wireToolbar();
  wireCardModal();

  return { open, refresh };
}