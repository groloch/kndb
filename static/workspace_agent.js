"use strict";

/* The /agent page: the caller's own workspace agent at the top level.
 * The pane is the same templates/agent.html a project's Agent tab includes;
 * only the project it talks to changes — the caller's personal workspace,
 * which auto-import keeps as a mirror of everything they ever imported
 * anywhere. The backend is unchanged: one stateless run over that project
 */

initIdentity().then(() => makeAgent({
    projectId: () => (KNDB.personal && KNDB.personal.id) || "",
}));