"use strict";
document.getElementById("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = document.getElementById("loginError");
  const btn = ev.target.querySelector("button");
  err.textContent = "";
  btn.disabled = true;
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: document.getElementById("username").value, password: document.getElementById("password").value }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    location.href = "/";
  } catch (e) {
    err.textContent = e.message;
    document.getElementById("password").select();
  } finally {
    btn.disabled = false;
  }
});
