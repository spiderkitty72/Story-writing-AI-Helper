"use strict";
// Shows who is logged in, with links to the account page and log out.
(async () => {
  const box = document.getElementById("userMenu");
  if (!box) return;
  const r = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (!r.ok) return;
  const me = await r.json();
  document.body.dataset.admin = String(me.is_admin);
  if (!me.is_admin) {  // writers cannot edit the shared AI configuration
    const cfg = document.getElementById("cfgEditor");
    if (cfg) cfg.closest(".panel").hidden = true;
  }
  const name = document.createElement("a");
  name.href = "/account";
  name.className = "button ghost small";
  name.title = me.is_admin ? "Your account and user management" : "Your account";
  name.textContent = "👤 " + me.username;
  const out = document.createElement("button");
  out.className = "ghost small";
  out.textContent = "Log out";
  out.addEventListener("click", async () => {
    const csrf = (document.cookie.match(/(?:^|;\s*)storyhelper_csrf=([^;]+)/) || [])[1] || "";
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf } });
    location.href = "/login";
  });
  box.append(name, out);
})();
