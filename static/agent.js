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

        const starting = A.lastRole !== role || !A.lastMsgEl || !A.lastTextEl;
        // The whitespace a model leaves around a tool call would otherwise
        // open a bubble of its own, empty but for its timestamp
        if (starting && !text.trim()) return;

        clearSeed(thread);

        if (starting) {
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

            const p = document.createElement("p");
            p.textContent = "";
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

        A.lastTextEl.textContent += text;
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
        thread.scrollTop = thread.scrollHeight;
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

    async function sendMessage() {
        const message = $("#agent-draft").value.trim();
        if (!message) return;

        A.streaming = true;
        updateSendBtn();

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
                }
            );
        } finally {
            A.streaming = false;
            updateSendBtn();
        }
    }

    if ($("#agent-draft")) {
        $("#agent-draft").addEventListener("input", updateSendBtn);
    }
    if ($("#btn-send-message")) {
        $("#btn-send-message").addEventListener("click", sendMessage);
    }
    if ($(".agent-thread")) {
        $(".agent-thread").addEventListener("click", onThreadClick);
    }

    updateSendBtn();
}
