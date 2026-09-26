"use strict";

const loginForm = document.querySelector("#login-form");
const registerForm = document.querySelector("#register-form");
const next = new URLSearchParams(location.search).get("next") || "/";
const safeNext = next.startsWith("/") && !next.startsWith("//") ? next : "/";
const requestedMode = new URLSearchParams(location.search).get("mode");

function errorMessage(element, error) {
  element.textContent = error.message || "Something went wrong. Please try again.";
}

function setMode(mode, registrationEnabled) {
  const register = mode === "register";
  document.body.classList.toggle("auth-mode-landing", !register && mode === "landing");
  document.body.classList.toggle("auth-mode-login", mode === "login");
  document.querySelector("#login-panel").hidden = register;
  document.querySelector("#register-panel").hidden = !register;
  document.querySelector("#auth-card").hidden = mode === "landing";
  document.querySelector("#nav-signin").hidden = mode === "login";
  document.querySelector("#nav-register").hidden = !registrationEnabled || register;
  document.querySelector("#login-signup-link").hidden = !registrationEnabled;
}

async function start() {
  let info;
  try {
    info = await api("/api/auth/config");
    KNDB.authMode = info.mode || "dev";
  } catch (_) {
    document.querySelector("#login-error").textContent = "Could not connect to KNDB.";
    return;
  }
  if (KNDB.authMode !== "password") {
    setMode("landing", false);
    document.querySelector("#nav-signin").hidden = true;
    document.querySelector("#nav-register").hidden = true;
    return;
  }
  let me;
  try { me = await api("/api/auth/me"); } catch (_) { /* signed out */ }
  if (me && me.user) {
    location.replace(safeNext);
    return;
  }
  const registrationEnabled = Boolean(info.registration_enabled);
  const mode = requestedMode === "register" && registrationEnabled
    ? "register"
    : location.pathname === "/" ? "landing" : "login";
  setMode(mode, registrationEnabled);
}

loginForm.addEventListener("submit", async event => {
  event.preventDefault();
  const error = document.querySelector("#login-error");
  error.textContent = "";
  const button = document.querySelector("#login-submit");
  button.disabled = true;
  try {
    await api("/api/auth/login", { method: "POST", body: {
      name: document.querySelector("#login-name").value,
      password: document.querySelector("#login-password").value,
    }});
    location.replace(safeNext);
  } catch (e) {
    errorMessage(error, e);
  } finally {
    button.disabled = false;
  }
});

registerForm.addEventListener("submit", async event => {
  event.preventDefault();
  const error = document.querySelector("#register-error");
  error.textContent = "";
  const password = document.querySelector("#register-password").value;
  const confirmation = document.querySelector("#register-confirm").value;
  if (password !== confirmation) {
    error.textContent = "Passwords do not match.";
    return;
  }
  const button = document.querySelector("#register-submit");
  button.disabled = true;
  try {
    await api("/api/auth/register", { method: "POST", body: {
      name: document.querySelector("#register-name").value,
      password,
    }});
    location.replace(safeNext);
  } catch (e) {
    errorMessage(error, e);
  } finally {
    button.disabled = false;
  }
});

start();
