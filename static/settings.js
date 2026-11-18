"use strict";

initIdentity().then(me => {
  if (!me) return;
  const form = document.querySelector("#settings-password-form");
  const feedback = document.querySelector("#settings-password-feedback");
  form.hidden = KNDB.authMode !== "password";
  if (form.hidden) return;

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const current = document.querySelector("#settings-current-password").value;
    const next = document.querySelector("#settings-new-password").value;
    const confirm = document.querySelector("#settings-confirm-password").value;
    feedback.className = "settings-feedback";
    feedback.textContent = "";
    if (next.length < 14) {
      feedback.classList.add("error");
      feedback.textContent = "The new password must be at least 14 characters.";
      return;
    }
    if (next !== confirm) {
      feedback.classList.add("error");
      feedback.textContent = "The new passwords do not match.";
      return;
    }
    const submit = document.querySelector("#settings-password-submit");
    submit.disabled = true;
    try {
      await api("/api/auth/password", { method: "POST", body: {
        current_password: current,
        new_password: next,
      }});
      feedback.classList.add("success");
      feedback.textContent = "Password updated. Please sign in again.";
      form.reset();
      setTimeout(() => { location.href = "/login"; }, 1200);
    } catch (error) {
      feedback.classList.add("error");
      feedback.textContent = error.message === "current password is incorrect"
        ? "The current password is incorrect."
        : "Password could not be updated: " + error.message;
      submit.disabled = false;
    }
  });
});
