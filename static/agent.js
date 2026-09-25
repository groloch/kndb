"use strict";

/*
* cfg = {
*   projectId: () => pid
* 
* 
* 
* }
* 
* 
*
*/ 

function makeAgent(cfg){
    const pid = () => (cfg.projectId ? cfg.projectId() : "");

    const A = {
        streaming: false,
        lastRole: null,
        lastMsgEl: null,
        lastTextEl: null,
        lastRaw: "",
        clearedSeed: false
    }

    function busy() {
        if(!A.streaming){
            return false;
        }
        return true;
    }

    function updateSendBtn() {
        const btn = $("#btn-send-message");
        const entry = $("#agent-draft");
        if (busy()) {
            btn.disabled = true;
        } else {
            if (entry.value.trim() === "") {
                btn.disabled = true;
            }else{
                btn.disabled = false;
            }
        }
    }

    function clearSeed(thread) {
        /* Drops the example conversation the page ships with, once, when the
        * first real event arrives
        */
        if (!A.clearedSeed) {
            thread.innerHTML = "";
            A.clearedSeed = true;
        }
    }

    function updateSession(token) {
        const thread = $(".agent-thread");
        if (!thread || !token) return;

        const role = token.role;
        const text = token.text || "";
        if (!text) return;

        // The user's message is drawn locally the moment it is sent; what the
        // server echoes back is only a confirmation, never drawn
        if (role === "user") return;

        const starting = A.lastRole !== role || !A.lastMsgEl || !A.lastTextEl;
        // The whitespace a model leaves around a tool call would otherwise
        // open a bubble of its own, empty but for its timestamp
        if (starting && !text.trim()) return;

        clearSeed(thread);

        if (starting) {
            removePending();

            const msg = document.createElement("article");
            switch (role) {
                case "user":
                    msg.className = "agent-msg agent-msg-user";
                    break;
                case "assistant":
                    msg.className = "agent-msg agent-msg-ai";
                    break;
                case "assistant-trace":
                    msg.className = "agent-msg agent-msg-ai-trace collapsed";
                    break;
                default:
                    msg.className = "agent-msg";
            }

            /* The model answers in markdown, so assistant bubbles render it;
             * user messages stay plain text
             */
            const p = role === "assistant"
                ? Object.assign(document.createElement("div"), { className: "agent-md" })
                : document.createElement("p");
            msg.appendChild(p);

            if (role === "user" || role === "assistant"){
                const time = document.createElement("time");
                const now = new Date();
                time.dateTime = now.toISOString();
                time.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
                msg.appendChild(time);
            }

            thread.appendChild(msg);

            A.lastMsgEl = msg;
            A.lastTextEl = p;
            A.lastRole = role;
        }

        A.lastRaw += text;
        if (role === "assistant") {
            /* Re-render the whole bubble: markdown is structural, so a token
             * can close a fence or list opened earlier in the message
             */
            A.lastTextEl.innerHTML = renderMarkdown(A.lastRaw);
        } else {
            A.lastTextEl.textContent = A.lastRaw;
        }
        thread.scrollTop = thread.scrollHeight;
    }

    function addUserMessage(text) {
        /* The user's own bubble, shown at once — before the server has even
         * acknowledged the message
         */
        const thread = $(".agent-thread");
        if (!thread) return;

        clearSeed(thread);

        const msg = document.createElement("article");
        msg.className = "agent-msg agent-msg-user";
        const p = document.createElement("p");
        p.textContent = text;
        msg.appendChild(p);

        const time = document.createElement("time");
        const now = new Date();
        time.dateTime = now.toISOString();
        time.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        msg.appendChild(time);

        thread.appendChild(msg);

        A.lastRole = "user";
        A.lastMsgEl = msg;
        A.lastTextEl = p;
        A.lastRaw = text;
        thread.scrollTop = thread.scrollHeight;
    }

    function showPending() {
        /* The three moving dots on the model's side, shown while the model
         * prefills — between the send and its first token
         */
        const thread = $(".agent-thread");
        if (!thread) return;

        clearSeed(thread);

        const msg = document.createElement("article");
        msg.className = "agent-msg agent-msg-ai agent-pending";
        for (let i = 0; i < 3; i++) {
            const dot = document.createElement("span");
            dot.className = "agent-typing-dot";
            msg.appendChild(dot);
        }
        thread.appendChild(msg);
        thread.scrollTop = thread.scrollHeight;
    }

    function removePending() {
        document.querySelectorAll(".agent-pending").forEach(el => el.remove());
    }

    function addErrorMessage(text) {
        /* A failed run — server unreachable, model error — shown where the
         * answer would have been, so the exchange still reads as one
         */
        const thread = $(".agent-thread");
        if (!thread) return;

        removePending();

        const msg = document.createElement("article");
        msg.className = "agent-msg agent-msg-error";
        msg.textContent = text;
        thread.appendChild(msg);

        A.lastRole = null;
        A.lastMsgEl = null;
        A.lastTextEl = null;
        A.lastRaw = "";
        thread.scrollTop = thread.scrollHeight;
    }

    function callSignature(ev) {
        /* "tool(arg=value, ...)", the call as the model wrote it
        */
        const args = ev.args && typeof ev.args === "object" ? ev.args : {};
        const parts = Object.keys(args).map(k => `${k}=${JSON.stringify(args[k])}`);
        return `${ev.tool || "tool"}(${parts.join(", ")})`;
    }

    function callResult(result) {
        /* The returned value, re-indented when the tool answered with JSON
        */
        if (result === null || result === undefined) return "";
        if (typeof result !== "string") return JSON.stringify(result, null, 2);
        try {
            return JSON.stringify(JSON.parse(result), null, 2);
        } catch (_) {
            return result;
        }
    }

    function addToolCall(ev) {
        /* One folded "used <tool>" row, unfolding onto the arguments and the
        * value the tool returned
        */
        const thread = $(".agent-thread");
        if (!thread || !ev) return;

        clearSeed(thread);

        const box = document.createElement("div");
        box.className = "agent-toolcall collapsed";

        const head = document.createElement("button");
        head.type = "button";
        head.className = "agent-toolcall-head";

        const caret = document.createElement("span");
        caret.className = "agent-toolcall-caret";
        caret.textContent = "▸";
        const name = document.createElement("span");
        name.className = "agent-toolcall-name";
        name.textContent = `used ${ev.tool || "tool"}`;
        head.append(caret, name);

        const body = document.createElement("div");
        body.className = "agent-toolcall-body";
        const args = document.createElement("code");
        args.className = "agent-toolcall-args";
        args.textContent = callSignature(ev);
        const result = document.createElement("pre");
        result.className = "agent-toolcall-result";
        result.textContent = callResult(ev.result);
        body.append(args, result);

        box.append(head, body);
        thread.appendChild(box);

        A.lastRole = null;
        A.lastMsgEl = null;
        A.lastTextEl = null;
        A.lastRaw = "";
        thread.scrollTop = thread.scrollHeight;
    }

    function addApproval(ev) {
        /* One pending "may I?" card: what the agent wants to run and with
        * which arguments, with Allow and Deny. The answer goes to the
        * approval side channel, not the stream — the run is paused on it —
        * and the approval_result event stamps the outcome onto the card
        */
        const thread = $(".agent-thread");
        if (!thread || !ev) return;

        clearSeed(thread);
        removePending();

        const box = document.createElement("div");
        box.className = "agent-approval";
        box.dataset.requestId = ev.request_id || "";

        const head = document.createElement("div");
        head.className = "agent-approval-head";
        const name = document.createElement("span");
        name.className = "agent-approval-name";
        name.textContent = `wants to use ${ev.tool || "a tool"}`;
        head.appendChild(name);

        const args = document.createElement("code");
        args.className = "agent-toolcall-args";
        args.textContent = callSignature(ev);

        const actions = document.createElement("div");
        actions.className = "agent-approval-actions";
        const allow = document.createElement("button");
        allow.type = "button";
        allow.className = "agent-approval-btn allow";
        allow.textContent = "Allow";
        const deny = document.createElement("button");
        deny.type = "button";
        deny.className = "agent-approval-btn deny";
        deny.textContent = "Deny";
        actions.append(allow, deny);

        const note = document.createElement("span");
        note.className = "agent-approval-note muted";

        box.append(head, args, actions, note);
        thread.appendChild(box);

        async function answer(decision) {
            allow.disabled = true;
            deny.disabled = true;
            try {
                await api(`/api/agent/approval/${pid()}`, {
                    method: "POST",
                    body: { request_id: box.dataset.requestId, decision }
                });
            } catch (e) {
                toast(e.message, "error");
                allow.disabled = false;
                deny.disabled = false;
                return;
            }
            settleApproval({ request_id: box.dataset.requestId, decision: decision });
        }
        allow.addEventListener("click", () => answer("allow"));
        deny.addEventListener("click", () => answer("deny"));

        A.lastRole = null;
        A.lastMsgEl = null;
        A.lastTextEl = null;
        A.lastRaw = "";
        thread.scrollTop = thread.scrollHeight;
    }

    function settleApproval(ev) {
        /* Stamps the outcome onto the matching card and freezes its buttons.
        * The toolcall event that follows shows what ran — or the refusal the
        * model reads, for a denied call
        */
        document.querySelectorAll(".agent-approval").forEach(box => {
            if (!ev || !ev.request_id || box.dataset.requestId !== ev.request_id) return;
            box.classList.add("answered");
            const allow = box.querySelector(".agent-approval-btn.allow");
            const deny = box.querySelector(".agent-approval-btn.deny");
            if (allow) allow.disabled = true;
            if (deny) deny.disabled = true;
            const note = box.querySelector(".agent-approval-note");
            if (note) {
                note.textContent = ev.decision === "allow" ? "allowed"
                    : ev.decision === "timeout" ? "timed out — denied"
                    : "denied";
            }
        });
    }

    function onThreadClick(e) {
        /* Folds and unfolds traces and tool calls. Clicks inside an unfolded
        * tool call are left alone, so its result stays selectable
        */
        const tool = e.target.closest(".agent-toolcall");
        if (tool) {
            if (!e.target.closest(".agent-toolcall-body")) {
                tool.classList.toggle("collapsed");
            }
            return;
        }
        const trace = e.target.closest(".agent-msg-ai-trace");
        if (trace) trace.classList.toggle("collapsed");
    }

    function autoGrow(entry) {
        /* The draft grows with its lines, up to the CSS max-height, where the
         * textarea's own scrollbar takes over
         */
        entry.style.height = "auto";
        entry.style.height = `${entry.scrollHeight}px`;
    }

    function clearDraft(entry) {
        entry.value = "";
        autoGrow(entry);
    }

    async function sendMessage() {
        const entry = $("#agent-draft");
        const message = entry.value.trim();
        if (!message || busy()) return;

        A.streaming = true;
        updateSendBtn();
        clearDraft(entry);

        addUserMessage(message);
        showPending();

        const body = {
            "message": message
        }

        try {
            await streamEvents(
                `/api/agent/message/${pid()}`,
                body,
                (ev) => {
                    if (ev.type === "token") updateSession(ev);
                    else if (ev.type === "toolcall") addToolCall(ev);
                    else if (ev.type === "approval_request") addApproval(ev);
                    else if (ev.type === "approval_result") settleApproval(ev);
                }
            );
        } catch (e) {
            addErrorMessage("The agent could not answer: " + e.message);
        } finally {
            removePending();
            A.streaming = false;
            updateSendBtn();
        }
    }

    async function newConversation() {
        /* Forgets the conversation server-side and empties the thread —
         * the next message starts a fresh context
         */
        if (A.streaming) return;
        try {
            await fetch(`/api/agent/session/${pid()}`, { method: "DELETE" });
        } catch (_) {
            return;
        }
        const thread = $(".agent-thread");
        if (thread) thread.innerHTML = "";
        A.clearedSeed = true;
        A.lastRole = null;
        A.lastMsgEl = null;
        A.lastTextEl = null;
        A.lastRaw = "";
    }

    async function replaySession() {
        /* Redraws the thread from the session's display log — what earlier
         * runs in this (project, user) session streamed. The page comes up
         * with the conversation intact instead of blank
         */
        const thread = $(".agent-thread");
        if (!thread) return;
        let events = [];
        try {
            const r = await fetch(`/api/agent/session/${pid()}`);
            if (r.ok) events = await r.json();
        } catch (_) {
            return;  // no session or server down — an empty thread is fine
        }
        events = (events && events.events) || [];
        if (!events.length) return;

        A.clearedSeed = true;
        for (const ev of events) {
            if (ev.type === "token") {
                if (ev.role === "user") addUserMessage(ev.text || "");
                else updateSession(ev);
            } else if (ev.type === "toolcall") {
                addToolCall(ev);
            }
        }
        thread.scrollTop = thread.scrollHeight;
    }

    if ($("#agent-draft")) {
        const entry = $("#agent-draft");
        entry.addEventListener("input", () => {
            updateSendBtn();
            autoGrow(entry);
        });
        /* Enter sends, Shift+Enter (and the other modifiers) make a newline */
        entry.addEventListener("keydown", (e) => {
            if (e.key !== "Enter" || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
            e.preventDefault();
            if (!busy()) sendMessage();
        });
    }
    if ($("#btn-send-message")) {
        $("#btn-send-message").addEventListener("click", sendMessage);
    }
    if ($(".agent-thread")) {
        $(".agent-thread").addEventListener("click", onThreadClick);
    }
    if ($("#btn-agent-new")) {
        $("#btn-agent-new").addEventListener("click", newConversation);
    }

    updateSendBtn();
    replaySession();
}
