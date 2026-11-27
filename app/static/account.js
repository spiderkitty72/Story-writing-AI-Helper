"use strict";
const $ = (id) => document.getElementById(id);
const csrf = () => (document.cookie.match(/(?:^|;\s*)storyhelper_csrf=([^;]+)/) || [])[1] || "";

async function call(path, method = "GET", body) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET") init.headers["X-CSRF-Token"] = csrf();
  if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const r = await fetch(path, init);
  if (r.status === 401) { location.href = "/login"; throw new Error("Please log in"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : (data.detail ? JSON.stringify(data.detail) : `HTTP ${r.status}`));
  return data;
}

function say(el, text, ok) { el.textContent = text; el.className = ok ? "auth-ok" : "auth-error"; }

let me = null;

$("pwForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if ($("pwNew").value !== $("pwRepeat").value) return say($("pwMsg"), "The new passwords do not match");
  try {
    await call("/api/auth/password", "POST", { current_password: $("pwCurrent").value, new_password: $("pwNew").value });
    ev.target.reset();
    say($("pwMsg"), "Password changed.", true);
  } catch (e) { say($("pwMsg"), e.message); }
});

function cell(text) { const td = document.createElement("td"); td.textContent = text; return td; }
function btn(label, fn, danger) {
  const b = document.createElement("button");
  b.className = "ghost small" + (danger ? " danger" : "");
  b.textContent = label;
  b.addEventListener("click", async () => {
    try { await fn(); await loadUsers(); } catch (e) { say($("nuMsg"), e.message); }
  });
  return b;
}

async function loadUsers() {
  const users = await call("/api/admin/users");
  const tbody = $("userTable");
  tbody.replaceChildren();
  for (const u of users) {
    const tr = document.createElement("tr");
    tr.append(cell(u.username + (u.id === me.id ? " (you)" : "")), cell(u.is_admin ? "Admin" : "Writer"),
      cell(u.disabled ? "Disabled" : "Active"));
    const td = document.createElement("td");
    td.className = "actions";
    td.append(btn("Reset password", async () => {
      const pw = prompt(`New password for ${u.username} (at least 10 characters):`);
      if (pw) { await call(`/api/admin/users/${u.id}`, "PATCH", { password: pw }); say($("nuMsg"), "Password reset.", true); }
    }));
    if (u.id !== me.id) {
      td.append(
        btn(u.is_admin ? "Make writer" : "Make admin", () => call(`/api/admin/users/${u.id}`, "PATCH", { is_admin: !u.is_admin })),
        btn(u.disabled ? "Enable" : "Disable", () => call(`/api/admin/users/${u.id}`, "PATCH", { disabled: !u.disabled })),
        btn("Delete", async () => {
          if (!confirm(`Delete ${u.username}? Their books stay on disk.`)) return;
          const r = await call(`/api/admin/users/${u.id}`, "DELETE");
          say($("nuMsg"), r.note || "Deleted.", true);
        }, true),
      );
    }
    tr.append(td);
    tbody.append(tr);
  }
}

$("newUserForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const u = await call("/api/admin/users", "POST", { username: $("nuName").value, password: $("nuPass").value, is_admin: $("nuAdmin").checked });
    ev.target.reset();
    say($("nuMsg"), `Created ${u.username}.`, true);
    await loadUsers();
  } catch (e) { say($("nuMsg"), e.message); }
});

(async () => {
  me = await call("/api/auth/me");
  $("pwUser").value = me.username;
  if (me.is_admin) { $("adminPanel").hidden = false; await loadUsers(); }
})();
