"use strict";

// ---------- tiny helpers ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const csrfToken = () => (document.cookie.match(/(?:^|;\s*)storyhelper_csrf=([^;]+)/) || [])[1] || "";

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (init.method !== "GET") init.headers["X-CSRF-Token"] = csrfToken();
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) { location.href = "/login"; throw new Error("Please log in"); }
  const type = r.headers.get("content-type") || "";
  const data = type.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `HTTP ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), isError ? 6000 : 2500);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

// ---------- state ----------
const S = {
  config: null,
  book: null,        // id
  bookData: null,
  chapter: null,     // id
  savedText: "",
  review: null,
  decisions: {},     // chunkIndex -> {changeId: bool}
  graph: { entities: [], relations: [] },
  editingEntity: null,
  chat: [],
  watching: new Set(),
};

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "review") loadReviews();
  if (name === "knowledge") loadKnowledge();
  if (name === "settings") loadSettings();
  if (name === "plan") guard(loadPlan)();
  document.body.classList.remove("menu-open");
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------- config ----------
async function loadConfig() {
  S.config = await api("/api/config");
  const ts = $("taskSelect");
  ts.innerHTML =
    `<optgroup label="Workflows">${S.config.workflows.map((w) => `<option value="wf:${esc(w.name)}">${esc(w.name)} (${w.steps.map(esc).join(" → ")})</option>`).join("")}</optgroup>` +
    `<optgroup label="Single agents">${S.config.agents.map((a) => `<option value="ag:${esc(a.name)}">${esc(a.name)}${a.mode === "report" ? " (notes only)" : ""}</option>`).join("")}</optgroup>`;
  const prefer = [...ts.options].find((o) => o.value === "ag:custom");
  if (prefer) prefer.selected = true;
  updateTaskDesc();
  const provOpts = S.config.providers.map((p) => `<option value="${esc(p.name)}">${esc(p.name)} (${esc(p.type)}${p.model ? ", " + esc(p.model) : ""})</option>`).join("");
  $("providerSelect").innerHTML = `<option value="">(agent default: ${esc(S.config.default_provider)})</option>` + provOpts;
  $("kgProvider").innerHTML = `<option value="">(default: ${esc(S.config.default_provider)})</option>` + provOpts;
  $("chunkInput").placeholder = "agent default";
  $("overlapInput").placeholder = S.config.chunking.overlap_tokens;
}
function updateTaskDesc() {
  const v = $("taskSelect").value || "";
  const [kind, name] = [v.slice(0, 2), v.slice(3)];
  const item = kind === "wf" ? S.config.workflows.find((w) => w.name === name) : S.config.agents.find((a) => a.name === name);
  $("taskDesc").textContent = item ? item.description : "";
  const writes = !!item && item.mode === "write";
  $("scopeLabel").classList.toggle("hidden", writes);
  $("writeOpts").classList.toggle("hidden", !writes);
  const agent = kind === "ag" ? item : item && S.config.agents.find((a) => a.name === item.steps[0]);
  $("wordsInput").placeholder = (agent && agent.words) || 250;
}
$("taskSelect").addEventListener("change", updateTaskDesc);

// ---------- books ----------
async function loadBooks(selectId) {
  const books = await api("/api/books");
  if (!books.length) {
    const r = await api("/api/books", { method: "POST", body: { title: "My Book" } });
    return loadBooks(r.id);
  }
  $("bookSelect").innerHTML = books.map((b) => `<option value="${esc(b.id)}">${esc(b.title)}</option>`).join("");
  let id = selectId || localStorage.getItem("book");
  if (!books.some((b) => b.id === id)) id = books[0].id;
  $("bookSelect").value = id;
  await openBook(id);
}
async function openBook(id) {
  if (!(await confirmLeave())) { $("bookSelect").value = S.book; return; }
  S.book = id;
  localStorage.setItem("book", id);
  $("exportBtn").href = `/api/books/${encodeURIComponent(id)}/export`;
  await refreshBook();
  const remembered = localStorage.getItem("chapter:" + id);
  const chs = S.bookData.chapters;
  const ch = chs.find((c) => c.id === remembered) || chs[0];
  if (ch) await openChapter(ch.id, true); else clearEditor();
  refreshReviewBadge();
  loadChat();
}
async function refreshBook() {
  S.bookData = await api(`/api/books/${encodeURIComponent(S.book)}`);
  renderChapterList();
}
$("bookSelect").addEventListener("change", guard((e) => openBook(e.target.value)));
$("newBookBtn").addEventListener("click", guard(async () => {
  const title = prompt("Title of the new book:");
  if (!title) return;
  const r = await api("/api/books", { method: "POST", body: { title } });
  await loadBooks(r.id);
}));

// ---------- chapters ----------
function renderChapterList() {
  $("chapterList").innerHTML = S.bookData.chapters.map((c) =>
    `<li data-id="${esc(c.id)}" class="${c.id === S.chapter ? "active" : ""}" draggable="true">
       <span>${esc(c.title)}</span><span class="muted small-text">${c.words}w</span></li>`).join("");
  $("kgScope").innerHTML = `<option value="">Whole book</option>` +
    S.bookData.chapters.map((c) => `<option value="${esc(c.id)}">${esc(c.title)}</option>`).join("");
}
$("chapterList").addEventListener("click", guard(async (e) => {
  const li = e.target.closest("li");
  if (li && li.dataset.id !== S.chapter) await openChapter(li.dataset.id);
  document.body.classList.remove("menu-open");
  showTab("write");
}));
// drag to reorder
let dragId = null;
$("chapterList").addEventListener("dragstart", (e) => { dragId = e.target.closest("li")?.dataset.id; });
$("chapterList").addEventListener("dragover", (e) => e.preventDefault());
$("chapterList").addEventListener("drop", guard(async (e) => {
  e.preventDefault();
  const target = e.target.closest("li")?.dataset.id;
  if (!dragId || !target || dragId === target) return;
  const order = S.bookData.chapters.map((c) => c.id).filter((id) => id !== dragId);
  order.splice(order.indexOf(target), 0, dragId);
  await api(`/api/books/${encodeURIComponent(S.book)}/order`, { method: "PUT", body: { order } });
  await refreshBook();
}));

function clearEditor() {
  S.chapter = null; S.savedText = "";
  $("editor").value = ""; $("chapterTitle").textContent = "No chapter";
  updateCounts();
}
async function confirmLeave() {
  if (S.chapter && $("editor").value !== S.savedText) {
    if (confirm("Save changes to the current chapter first?")) await saveChapter();
  }
  return true;
}
async function openChapter(id, skipConfirm = false) {
  if (!skipConfirm) await confirmLeave();
  const r = await api(`/api/books/${encodeURIComponent(S.book)}/chapters/${encodeURIComponent(id)}`);
  S.chapter = id; S.savedText = r.text;
  localStorage.setItem("chapter:" + S.book, id);
  $("editor").value = r.text;
  const info = S.bookData.chapters.find((c) => c.id === id);
  $("chapterTitle").textContent = info ? info.title : id;
  renderChapterList();
  updateCounts();
  updateAiBar();
  if (!$("preview").classList.contains("hidden")) renderPreview();
}
async function saveChapter() {
  if (!S.chapter) return;
  const text = $("editor").value;
  await api(`/api/books/${encodeURIComponent(S.book)}/chapters/${encodeURIComponent(S.chapter)}`, { method: "PUT", body: { text } });
  S.savedText = text;
  updateCounts();
  await refreshBook();
  $("saveState").textContent = "Saved " + new Date().toLocaleTimeString();
}
$("saveBtn").addEventListener("click", guard(saveChapter));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); guard(saveChapter)(); }
});
let autosaveTimer;
$("editor").addEventListener("input", () => {
  updateCounts();
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(guard(saveChapter), 4000);
  if (!$("preview").classList.contains("hidden")) schedulePreview();
});
function updateCounts() {
  const t = $("editor").value;
  const words = (t.match(/\S+/g) || []).length;
  $("wordCount").textContent = `${words} words · ~${Math.ceil(t.length / (S.config?.chunking.chars_per_token || 4))} tokens`;
  $("saveState").textContent = S.chapter && t !== S.savedText ? "Unsaved changes" : $("saveState").textContent.startsWith("Saved") ? $("saveState").textContent : "";
}
window.addEventListener("beforeunload", (e) => { if (S.chapter && $("editor").value !== S.savedText) e.preventDefault(); });

$("newChapterBtn").addEventListener("click", guard(async () => {
  const title = prompt("Chapter title:", `Chapter ${S.bookData.chapters.length + 1}`);
  if (!title) return;
  await confirmLeave();
  const r = await api(`/api/books/${encodeURIComponent(S.book)}/chapters`, { method: "POST", body: { title } });
  await refreshBook();
  await openChapter(r.id, true);
}));
$("renameChapterBtn").addEventListener("click", guard(async () => {
  if (!S.chapter) return;
  const text = $("editor").value;
  const m = text.match(/^#\s+(.*)$/m);
  const title = prompt("New chapter title (changes the first # heading):", m ? m[1] : "");
  if (!title) return;
  $("editor").value = m ? text.replace(/^#\s+.*$/m, "# " + title) : `# ${title}\n\n` + text;
  await saveChapter();
  $("chapterTitle").textContent = title;
}));
$("deleteChapterBtn").addEventListener("click", guard(async () => {
  if (!S.chapter || !confirm("Delete this chapter? This cannot be undone.")) return;
  await api(`/api/books/${encodeURIComponent(S.book)}/chapters/${encodeURIComponent(S.chapter)}`, { method: "DELETE" });
  S.savedText = $("editor").value; // avoid the save prompt
  await refreshBook();
  const first = S.bookData.chapters[0];
  if (first) await openChapter(first.id, true); else clearEditor();
}));

// preview
let previewTimer;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(guard(renderPreview), 400); }
async function renderPreview() {
  const r = await api("/api/render", { method: "POST", body: { text: $("editor").value } });
  $("preview").innerHTML = r.html; // rendered server-side by python-markdown from the user's own text
}
$("previewToggle").addEventListener("change", (e) => {
  $("preview").classList.toggle("hidden", !e.target.checked);
  if (e.target.checked) guard(renderPreview)();
});

// ---------- run AI ----------
function taskOf(name) {
  if (name.startsWith("wf:")) return S.config.workflows.find((w) => w.name === name.slice(3));
  return S.config.agents.find((a) => a.name === name.slice(3));
}
// opts: {task: "ag:name"|"wf:name", instructions, scope: "chapter"|"selection", words}
async function runTask(opts) {
  if (!S.chapter) return toast("Open a chapter first", true);
  const ed = $("editor");
  const sel = { start: ed.selectionStart, end: ed.selectionEnd };
  await saveChapter();
  const item = taskOf(opts.task);
  if (!item) return toast("Unknown task", true);
  const body = {
    chapter: S.chapter,
    instructions: opts.instructions || "",
    provider: $("providerSelect").value || null,
    model: $("modelInput").value.trim() || null,
    use_knowledge: $("useKnowledge").checked,
  };
  if (opts.task.startsWith("wf:")) body.workflow = item.name; else body.agent = item.name;
  if ($("chunkInput").value) body.chunk_tokens = +$("chunkInput").value;
  if ($("overlapInput").value !== "") body.overlap_tokens = +$("overlapInput").value;
  if (item.mode === "write") {
    body.start = sel.end; body.end = sel.end; // write at the cursor / after the selection
    if (opts.words) body.words = opts.words;
    if (item.name === "write" || (item.steps && item.steps[0] === "write")) {
      if (!body.instructions.trim()) return toast("Type what you want the AI to write", true);
    }
  } else if (opts.scope === "selection") {
    if (sel.start === sel.end) return toast("Select some text in the editor first", true);
    body.start = sel.start; body.end = sel.end;
  }
  if (body.agent === "custom" && !body.instructions.trim()) return toast("Type some instructions for the custom task", true);
  const job = await api(`/api/books/${encodeURIComponent(S.book)}/run`, { method: "POST", body });
  $("runStatus").textContent = "Started. Progress is shown in the sidebar.";
  watchJob(job.id, S.book);
}
$("runBtn").addEventListener("click", guard(() => runTask({
  task: $("taskSelect").value,
  instructions: $("instructions").value,
  scope: $("scopeSelect").value,
  words: +$("wordsInput").value || null,
})));

// Quick AI bar above the editor: acts on the selection, or writes at the cursor.
function hasSelection() { const ed = $("editor"); return ed.selectionStart !== ed.selectionEnd; }
function updateAiBar() {
  const ed = $("editor");
  const sel = hasSelection();
  $("aiBarSel").classList.toggle("hidden", !sel);
  $("aiBarCursor").classList.toggle("hidden", sel);
  if (sel) {
    const words = (ed.value.slice(ed.selectionStart, ed.selectionEnd).match(/\S+/g) || []).length;
    $("aiBarLabel").textContent = `${words} word${words === 1 ? "" : "s"} selected:`;
    $("aiBarInput").placeholder = "Ask for a change, e.g. make her sound angrier";
  } else {
    $("aiBarLabel").textContent = "At the cursor:";
    $("aiBarInput").placeholder = "Or ask it to write something, e.g. a short fight in the rain";
  }
}
["select", "keyup", "mouseup", "input", "focus"].forEach((ev) => $("editor").addEventListener(ev, updateAiBar));
$("aiBar").addEventListener("mousedown", (e) => { if (e.target.tagName === "BUTTON") e.preventDefault(); });
$("aiBar").addEventListener("click", guard(async (e) => {
  const q = e.target.dataset.quick;
  if (!q) return;
  if (!S.config.agents.some((a) => a.name === q)) return toast(`There is no '${q}' agent in agents.yaml`, true);
  await runTask({ task: "ag:" + q, instructions: $("aiBarInput").value.trim(), scope: "selection" });
}));
async function aiBarGo() {
  const text = $("aiBarInput").value.trim();
  if (!text) return toast("Type what you want first", true);
  if (hasSelection()) await runTask({ task: "ag:custom", instructions: text, scope: "selection" });
  else await runTask({ task: "ag:write", instructions: text });
  $("aiBarInput").value = "";
}
$("aiBarGo").addEventListener("click", guard(aiBarGo));
$("aiBarInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); guard(aiBarGo)(); } });

// ---------- chat ----------
document.querySelectorAll(".subtabs button[data-sub]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".subtabs button[data-sub]").forEach((x) => x.classList.toggle("active", x === b));
  ["tasks", "chat", "style"].forEach((n) => $("sub-" + n).classList.toggle("hidden", b.dataset.sub !== n));
}));
const chatKey = () => "chat:" + S.book;
function loadChat() {
  try { S.chat = JSON.parse(localStorage.getItem(chatKey()) || "[]"); } catch { S.chat = []; }
  renderChat();
}
function storeChat() { try { localStorage.setItem(chatKey(), JSON.stringify(S.chat.slice(-40))); } catch { /* private mode */ } }
function renderChat(pending = false) {
  $("chatLog").innerHTML = S.chat.map((m, i) => `<div class="msg ${m.role}"><div class="msg-text">${esc(m.content)}</div>` +
    (m.role === "assistant" ? `<div class="msg-actions"><button class="ghost small" data-insert="${i}" title="Adds this text at the cursor, through Review">Insert at cursor</button></div>` : "") +
    `</div>`).join("") + (pending ? `<div class="msg assistant muted">Thinking…</div>` : "") ||
    `<div class="muted small-text">No messages yet.</div>`;
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
}
async function sendChat() {
  const message = $("chatInput").value.trim();
  if (!message || $("chatSendBtn").disabled) return;
  const ed = $("editor");
  const history = S.chat.slice(-8);
  S.chat.push({ role: "user", content: message });
  $("chatInput").value = "";
  $("chatSendBtn").disabled = true;
  renderChat(true);
  try {
    const r = await api(`/api/books/${encodeURIComponent(S.book)}/chat`, {
      method: "POST",
      body: { message, history, chapter: S.chapter, position: S.chapter ? ed.selectionEnd : null,
        provider: $("providerSelect").value || null, model: $("modelInput").value.trim() || null,
        use_knowledge: $("useKnowledge").checked },
    });
    S.chat.push({ role: "assistant", content: r.reply });
  } catch (e) {
    S.chat.pop();
    $("chatInput").value = message;
    throw e;
  } finally {
    $("chatSendBtn").disabled = false;
    storeChat();
    renderChat();
  }
}
$("chatSendBtn").addEventListener("click", guard(sendChat));
$("chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); guard(sendChat)(); } });
$("chatClearBtn").addEventListener("click", () => { S.chat = []; storeChat(); renderChat(); });
$("chatLog").addEventListener("click", guard(async (e) => {
  const i = e.target.dataset.insert;
  if (i === undefined) return;
  if (!S.chapter) return toast("Open a chapter first", true);
  const position = $("editor").selectionEnd;
  await saveChapter();
  const r = await api(`/api/books/${encodeURIComponent(S.book)}/reviews/insert`, {
    method: "POST", body: { chapter: S.chapter, text: S.chat[+i].content, position },
  });
  refreshReviewBadge();
  showTab("review");
  await openReview(r.review_id);
}));

async function watchJob(id, book) {
  S.watching.add(id);
  renderJobs();
  while (S.watching.has(id)) {
    await new Promise((r) => setTimeout(r, 1200));
    let job;
    try { job = await api(`/api/jobs/${id}`); } catch { S.watching.delete(id); break; }
    renderJobs();
    if (job.status !== "running") {
      S.watching.delete(id);
      if (job.status === "error") toast("AI job failed: " + job.error, true);
      if (job.status === "done" && job.kind === "edit" && job.result.review_id && book === S.book) {
        toast("Done: " + job.message + ". Opening review…");
        $("runStatus").textContent = "";
        refreshReviewBadge();
        showTab("review");
        await openReview(job.result.review_id);
      }
      if (job.status === "done" && job.kind === "knowledge") {
        toast("Knowledge graph updated: " + job.message);
        if (book === S.book) loadKnowledge();
      }
    }
  }
}
async function renderJobs() {
  const jobs = await api("/api/jobs").catch(() => []);
  $("jobList").innerHTML = jobs.slice(0, 8).map((j) => {
    const pct = j.total ? Math.round(100 * j.progress / j.total) : 0;
    const cancel = j.status === "running" ? ` <button class="ghost small" data-cancel="${j.id}">Cancel</button>` : "";
    return `<li><div><strong>${esc(j.label)}</strong>${cancel}</div>
      <div class="muted">${esc(j.status)}${j.message ? " · " + esc(j.message) : ""}${j.error ? " · " + esc(j.error) : ""}</div>
      ${j.status === "running" ? `<div class="progress"><div style="width:${pct}%"></div></div>` : ""}</li>`;
  }).join("") || `<li class="muted">No jobs yet</li>`;
}
$("jobList").addEventListener("click", guard(async (e) => {
  const id = e.target.dataset.cancel;
  if (id) { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); renderJobs(); }
}));

// ---------- review ----------
async function refreshReviewBadge() {
  if (!S.book) return;
  const list = await api(`/api/books/${encodeURIComponent(S.book)}/reviews`).catch(() => []);
  const n = list.filter((r) => r.status === "pending").length;
  $("reviewBadge").textContent = n;
  $("reviewBadge").classList.toggle("hidden", !n);
  return list;
}
async function loadReviews() {
  const list = await refreshReviewBadge();
  const chTitle = (id) => (S.bookData.chapters.find((c) => c.id === id) || {}).title || id;
  $("reviewList").innerHTML = (list || []).map((r) =>
    `<li data-id="${esc(r.id)}" class="${S.review && S.review.id === r.id ? "active" : ""}">
      <div><div>${esc(r.label)}</div><div class="muted small-text">${esc(chTitle(r.chapter))} · ${esc(r.created)}<br>
      ${r.changes} changes, ${r.notes_count} notes · <em>${esc(r.status)}</em></div></div></li>`).join("") ||
    `<li class="muted">Nothing to review yet</li>`;
}
$("reviewList").addEventListener("click", guard(async (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) await openReview(li.dataset.id);
}));

async function openReview(id) {
  S.review = await api(`/api/books/${encodeURIComponent(S.book)}/reviews/${encodeURIComponent(id)}`);
  S.decisions = {};
  for (const c of S.review.chunks) {
    S.decisions[c.index] = {};
    for (const s of c.segments) if (s.type === "change") S.decisions[c.index][s.id] = true;
  }
  focusIdx = -1;
  renderReview();
  loadReviews();
}
function renderReview() {
  const r = S.review;
  $("reviewEmpty").classList.toggle("hidden", !!r);
  $("reviewView").classList.toggle("hidden", !r);
  if (!r) return;
  const chTitle = (S.bookData.chapters.find((c) => c.id === r.chapter) || {}).title || r.chapter;
  $("reviewTitle").textContent = r.label;
  const accepted = Object.values(S.decisions).reduce((n, d) => n + Object.values(d).filter(Boolean).length, 0);
  $("reviewMeta").textContent = `${chTitle} · ${r.created} · ${r.changes} changes (${accepted} accepted) · ${r.notes_count} notes · ${r.status}`;
  const pending = r.status === "pending";
  ["applyBtn", "acceptAllBtn", "rejectAllBtn"].forEach((b) => ($(b).disabled = !pending));
  $("reviewBody").innerHTML = r.chunks.map((c) => {
    const body = c.segments.map((s) => {
      if (s.type === "equal") return esc(s.text);
      const ok = S.decisions[c.index]?.[s.id] ?? true;
      return `<span class="chg ${ok ? "accepted" : "rejected"}" data-c="${c.index}" data-h="${s.id}" title="Click to ${ok ? "reject" : "accept"}">` +
        (s.old ? `<del>${esc(s.old)}</del>` : "") + (s.new ? `<ins>${esc(s.new)}</ins>` : "") + `</span>`;
    }).join("");
    const notes = c.notes.length ? `<div class="notes"><strong>Notes</strong><ul>${c.notes.map((n) => `<li><em>${esc(n.agent)}:</em> ${esc(n.text)}</li>`).join("")}</ul></div>` : "";
    const errors = c.errors.length ? `<div class="errors">⚠ ${c.errors.map(esc).join("<br>")}</div>` : "";
    if (r.kind === "insert") {
      const pre = r.anchor ? `<span class="ctx">…${esc(r.anchor.slice(-60))}</span>` : "";
      const post = r.anchor_after ? `<span class="ctx">${esc(r.anchor_after.slice(0, 60))}…</span>` : "";
      return `<div class="chunk">${errors}${notes}${pre}${body}${post}</div>`;
    }
    return `<div class="chunk">${errors}${notes}${body}</div>`;
  }).join("");
}
$("reviewBody").addEventListener("click", (e) => {
  const el = e.target.closest(".chg");
  if (!el || S.review.status !== "pending") return;
  const d = S.decisions[el.dataset.c];
  d[el.dataset.h] = !d[el.dataset.h];
  const ok = d[el.dataset.h];
  el.classList.toggle("accepted", ok); el.classList.toggle("rejected", !ok);
  el.title = `Click to ${ok ? "reject" : "accept"}`;
  const accepted = Object.values(S.decisions).reduce((n, x) => n + Object.values(x).filter(Boolean).length, 0);
  $("reviewMeta").textContent = $("reviewMeta").textContent.replace(/\(\d+ accepted\)/, `(${accepted} accepted)`);
});
function setAll(v) { for (const d of Object.values(S.decisions)) for (const k in d) d[k] = v; renderReview(); }
$("acceptAllBtn").addEventListener("click", () => setAll(true));
$("rejectAllBtn").addEventListener("click", () => setAll(false));
let focusIdx = -1;
function moveFocus(delta) {
  const all = [...document.querySelectorAll("#reviewBody .chg")];
  if (!all.length) return;
  all.forEach((x) => x.classList.remove("focus"));
  focusIdx = (focusIdx + delta + all.length) % all.length;
  all[focusIdx].classList.add("focus");
  all[focusIdx].scrollIntoView({ block: "center", behavior: "smooth" });
}
$("nextChangeBtn").addEventListener("click", () => moveFocus(1));
$("prevChangeBtn").addEventListener("click", () => moveFocus(-1));
$("applyBtn").addEventListener("click", guard(async () => {
  const r = S.review;
  if (S.chapter === r.chapter && $("editor").value !== S.savedText) await saveChapter();
  const res = await api(`/api/books/${encodeURIComponent(S.book)}/reviews/${encodeURIComponent(r.id)}/apply`, { method: "POST", body: { decisions: S.decisions } });
  toast(`Applied ${res.accepted} of ${res.total} changes. A backup of the old chapter was saved.`);
  await refreshBook();
  if (S.chapter === r.chapter) await openChapter(r.chapter, true);
  await openReview(r.id);
}));
$("discardBtn").addEventListener("click", guard(async () => {
  if (!confirm("Delete this change set?")) return;
  await api(`/api/books/${encodeURIComponent(S.book)}/reviews/${encodeURIComponent(S.review.id)}`, { method: "DELETE" });
  S.review = null; renderReview(); loadReviews();
}));

// ---------- knowledge ----------
const TYPES = ["character", "place", "object", "event", "group", "concept"];
const TYPE_COLORS = { character: "#d9735b", place: "#4f9a6a", object: "#c2a03a", event: "#8a6bc4", group: "#3b8fb5", concept: "#8a8a8a" };
$("entType").innerHTML = TYPES.map((t) => `<option>${t}</option>`).join("");

async function loadKnowledge() {
  if (!S.book) return;
  S.graph = await api(`/api/books/${encodeURIComponent(S.book)}/knowledge`);
  $("bookNotes").value = S.bookData.notes || "";
  renderSummaries();
  renderEntityList();
  graphView.setData(S.graph);
}
function renderEntityList() {
  const f = $("kgFilter").value.toLowerCase();
  const ents = S.graph.entities.filter((e) => !f || e.name.toLowerCase().includes(f) || e.type.includes(f))
    .sort((a, b) => a.type.localeCompare(b.type) || a.name.localeCompare(b.name));
  $("entityList").innerHTML = ents.map((e) =>
    `<li data-id="${esc(e.id)}"><span>${e.pinned ? "📌 " : ""}${esc(e.name)}</span>
     <span class="type" style="color:${TYPE_COLORS[e.type]}">${esc(e.type)}</span></li>`).join("") ||
    `<li class="muted">No entities yet. Extract them with AI or add them by hand.</li>`;
}
$("kgFilter").addEventListener("input", renderEntityList);
$("entityList").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) editEntity(li.dataset.id);
});
async function saveGraph() {
  S.graph = await api(`/api/books/${encodeURIComponent(S.book)}/knowledge`, { method: "PUT", body: S.graph });
  renderEntityList();
  graphView.setData(S.graph);
}
function editEntity(id) {
  const e = S.graph.entities.find((x) => x.id === id);
  if (!e) return;
  S.editingEntity = id;
  $("entityEditor").classList.remove("hidden");
  $("entName").value = e.name; $("entType").value = e.type;
  $("entAliases").value = (e.aliases || []).join(", ");
  $("entDesc").value = e.description || ""; $("entFacts").value = (e.facts || []).join("\n");
  $("entPinned").checked = !!e.pinned;
  renderEntityFields(e.type, e.fields || {});
  renderRelations();
  graphView.highlight(id);
}
function renderRelations() {
  const id = S.editingEntity;
  const name = (x) => (S.graph.entities.find((e) => e.id === x) || {}).name || "?";
  $("relList").innerHTML = S.graph.relations.map((r, i) => (r.source === id || r.target === id)
    ? `<li><span>${esc(name(r.source))} <em>${esc(r.label)}</em> ${esc(name(r.target))}</span><button class="ghost small danger" data-rel="${i}">×</button></li>` : "").join("");
  $("relTarget").innerHTML = S.graph.entities.filter((e) => e.id !== id)
    .map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("");
}
$("relList").addEventListener("click", guard(async (e) => {
  if (e.target.dataset.rel === undefined) return;
  S.graph.relations.splice(+e.target.dataset.rel, 1);
  await saveGraph(); renderRelations();
}));
$("addRelBtn").addEventListener("click", guard(async () => {
  const label = $("relLabel").value.trim(), target = $("relTarget").value;
  if (!label || !target) return;
  S.graph.relations.push({ source: S.editingEntity, target, label });
  $("relLabel").value = "";
  await saveGraph(); renderRelations();
}));
$("saveEntityBtn").addEventListener("click", guard(async () => {
  const e = S.graph.entities.find((x) => x.id === S.editingEntity);
  Object.assign(e, {
    name: $("entName").value.trim(), type: $("entType").value,
    aliases: $("entAliases").value.split(",").map((s) => s.trim()).filter(Boolean),
    description: $("entDesc").value.trim(),
    facts: $("entFacts").value.split("\n").map((s) => s.trim()).filter(Boolean),
    pinned: $("entPinned").checked,
    fields: readEntityFields(),
  });
  await saveGraph(); toast("Saved");
}));
$("deleteEntityBtn").addEventListener("click", guard(async () => {
  if (!confirm("Delete this entity?")) return;
  S.graph.entities = S.graph.entities.filter((x) => x.id !== S.editingEntity);
  S.graph.relations = S.graph.relations.filter((r) => r.source !== S.editingEntity && r.target !== S.editingEntity);
  $("entityEditor").classList.add("hidden");
  await saveGraph();
}));
$("closeEntityBtn").addEventListener("click", () => { $("entityEditor").classList.add("hidden"); graphView.highlight(null); });
$("addEntityBtn").addEventListener("click", guard(async () => {
  const name = prompt("Name of the character, place, object…");
  if (!name) return;
  const id = Math.random().toString(16).slice(2, 10);
  S.graph.entities.push({ id, name, type: "character", aliases: [], description: "", facts: [], pinned: false, fields: {} });
  await saveGraph(); editEntity(id);
}));
$("buildKgBtn").addEventListener("click", guard(async () => {
  const job = await api(`/api/books/${encodeURIComponent(S.book)}/knowledge/build`, {
    method: "POST", body: { chapter: $("kgScope").value || null, provider: $("kgProvider").value || null },
  });
  toast("Extracting knowledge… progress is in the sidebar.");
  watchJob(job.id, S.book);
}));
function renderSummaries() {
  const sums = S.graph.summaries || {};
  $("summaryList").innerHTML = S.bookData.chapters.map((c) =>
    `<label>${esc(c.title)}<textarea rows="3" data-sum="${esc(c.id)}" placeholder="Not summarised yet">${esc(sums[c.id] || "")}</textarea></label>`).join("");
}
$("saveSummariesBtn").addEventListener("click", guard(async () => {
  const sums = { ...(S.graph.summaries || {}) };
  document.querySelectorAll("#summaryList textarea").forEach((t) => { sums[t.dataset.sum] = t.value.trim(); });
  S.graph.summaries = sums;
  await saveGraph();
  renderSummaries();
  toast("Summaries saved");
}));
$("saveNotesBtn").addEventListener("click", guard(async () => {
  await api(`/api/books/${encodeURIComponent(S.book)}`, { method: "PATCH", body: { notes: $("bookNotes").value } });
  S.bookData.notes = $("bookNotes").value;
  toast("Book notes saved");
}));

// Small force-directed mind map on a canvas (no external libraries).
const graphView = (() => {
  const cv = $("graphCanvas");
  const ctx = cv.getContext("2d");
  let nodes = [], links = [], selected = null, drag = null, pan = { x: 0, y: 0 }, panStart = null, ticks = 0;
  function size() {
    const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    cv.width = r.width * dpr; cv.height = r.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return r;
  }
  function setData(g) {
    const old = Object.fromEntries(nodes.map((n) => [n.id, n]));
    const r = cv.getBoundingClientRect();
    nodes = g.entities.map((e, i) => {
      const o = old[e.id];
      const a = (i / Math.max(1, g.entities.length)) * Math.PI * 2;
      return { id: e.id, name: e.name, type: e.type, pinned: e.pinned,
        x: o ? o.x : r.width / 2 + Math.cos(a) * 160, y: o ? o.y : r.height / 2 + Math.sin(a) * 160, vx: 0, vy: 0 };
    });
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
    links = g.relations.filter((l) => byId[l.source] && byId[l.target]).map((l) => ({ s: byId[l.source], t: byId[l.target], label: l.label }));
    ticks = 0;
    requestAnimationFrame(loop);
  }
  function step() {
    const r = cv.getBoundingClientRect(), cx = r.width / 2, cy = r.height / 2;
    for (const a of nodes) {
      for (const b of nodes) {
        if (a === b) continue;
        let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01;
        const f = 2200 / d2;
        a.vx += dx * f / Math.sqrt(d2); a.vy += dy * f / Math.sqrt(d2);
      }
      a.vx += (cx - a.x) * 0.002; a.vy += (cy - a.y) * 0.002;
    }
    for (const l of links) {
      const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - 130) * 0.01;
      l.s.vx += dx / d * f; l.s.vy += dy / d * f; l.t.vx -= dx / d * f; l.t.vy -= dy / d * f;
    }
    for (const n of nodes) {
      if (n === drag) continue;
      n.vx *= 0.82; n.vy *= 0.82; n.x += n.vx; n.y += n.vy;
    }
  }
  function draw() {
    const r = size();
    const style = getComputedStyle(document.body);
    ctx.clearRect(0, 0, r.width, r.height);
    ctx.save(); ctx.translate(pan.x, pan.y);
    ctx.font = "12px system-ui"; ctx.textAlign = "center";
    for (const l of links) {
      const hot = selected && (l.s.id === selected || l.t.id === selected);
      ctx.strokeStyle = hot ? style.getPropertyValue("--accent") : style.getPropertyValue("--border");
      ctx.lineWidth = hot ? 2 : 1;
      ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
      if (hot || nodes.length < 25) {
        ctx.fillStyle = style.getPropertyValue("--muted");
        ctx.fillText(l.label, (l.s.x + l.t.x) / 2, (l.s.y + l.t.y) / 2 - 3);
      }
    }
    for (const n of nodes) {
      ctx.fillStyle = TYPE_COLORS[n.type] || "#888";
      ctx.beginPath(); ctx.arc(n.x, n.y, n.id === selected ? 11 : 8, 0, Math.PI * 2); ctx.fill();
      if (n.pinned) { ctx.strokeStyle = style.getPropertyValue("--text"); ctx.lineWidth = 2; ctx.stroke(); }
      ctx.fillStyle = style.getPropertyValue("--text");
      ctx.fillText(n.name, n.x, n.y + 22);
    }
    ctx.restore();
    if (!nodes.length) {
      ctx.fillStyle = style.getPropertyValue("--muted"); ctx.textAlign = "center";
      ctx.fillText("The mind map appears here once the book has entities.", r.width / 2, r.height / 2);
    }
  }
  function loop() {
    if (ticks < 400 || drag) { step(); ticks++; draw(); requestAnimationFrame(loop); } else draw();
  }
  const pos = (e) => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left - pan.x, y: e.clientY - r.top - pan.y }; };
  const hit = (p) => nodes.find((n) => (n.x - p.x) ** 2 + (n.y - p.y) ** 2 < 144);
  let moved = false;
  cv.addEventListener("mousedown", (e) => {
    const p = pos(e); drag = hit(p); moved = false;
    if (!drag) panStart = { x: e.clientX - pan.x, y: e.clientY - pan.y };
    requestAnimationFrame(loop);
  });
  window.addEventListener("mousemove", (e) => {
    if (drag) { const p = pos(e); drag.x = p.x; drag.y = p.y; moved = true; }
    else if (panStart) { pan = { x: e.clientX - panStart.x, y: e.clientY - panStart.y }; draw(); }
  });
  window.addEventListener("mouseup", () => {
    if (drag && !moved) editEntity(drag.id);
    if (drag) { ticks = 300; requestAnimationFrame(loop); }
    drag = null; panStart = null;
  });
  window.addEventListener("resize", draw);
  return { setData, highlight(id) { selected = id; draw(); } };
})();

// ---------- shared UI helpers ----------
const bookUrl = (path = "") => `/api/books/${encodeURIComponent(S.book)}${path}`;
function openModal(title, html) {
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = html;
  $("modal").classList.remove("hidden");
}
function closeModal() { $("modal").classList.add("hidden"); $("modalBody").innerHTML = ""; }
$("modalClose").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("modal").classList.contains("hidden")) closeModal(); });
function selectInEditor(start, end) {
  showTab("write");
  const ed = $("editor");
  ed.focus();
  ed.setSelectionRange(start, end);
  // scroll the selection into view: measure with a hidden copy of the text
  const before = ed.value.slice(0, start);
  const lines = before.split("\n").length;
  const lh = parseFloat(getComputedStyle(ed).lineHeight) || 28;
  ed.scrollTop = Math.max(0, (lines - 3) * lh * (ed.clientWidth < 500 ? 1.6 : 1.1));
  updateAiBar();
}
function segmentsHtml(segments) {
  return segments.map((s) => s.type === "equal" ? esc(s.text)
    : `<span class="chg accepted">${s.old ? `<del>${esc(s.old)}</del>` : ""}${s.new ? `<ins>${esc(s.new)}</ins>` : ""}</span>`).join("");
}

// ---------- mobile menu ----------
$("menuBtn").addEventListener("click", () => document.body.classList.toggle("menu-open"));
$("scrim").addEventListener("click", () => document.body.classList.remove("menu-open"));

// ---------- snapshots (history) ----------
async function showHistory(selectId) {
  if (!S.chapter) return toast("Open a chapter first", true);
  if ($("editor").value !== S.savedText) await saveChapter();
  const url = bookUrl(`/chapters/${encodeURIComponent(S.chapter)}/snapshots`);
  const list = await api(url);
  openModal("History: " + $("chapterTitle").textContent, `
    <div class="row wrap"><input id="snapLabel" placeholder="Name for a new snapshot (optional)">
      <button id="snapSave" class="small">Save snapshot now</button></div>
    <div class="history-grid">
      <ul id="snapList" class="list">${list.map((x) => `<li data-id="${esc(x.id)}"><div>${esc(x.label)}
        <div class="muted small-text">${esc(x.created)} · ${x.words} words</div></div></li>`).join("") ||
        `<li class="muted">No snapshots yet. One is saved automatically before AI changes, find &amp; replace and restores.</li>`}</ul>
      <div id="snapView" class="muted small-text">Pick a snapshot to compare it with the current chapter.</div>
    </div>`);
  $("snapSave").onclick = guard(async () => {
    await api(url, { method: "POST", body: { label: $("snapLabel").value } });
    toast("Snapshot saved"); await showHistory();
  });
  $("snapList").onclick = guard(async (e) => {
    const li = e.target.closest("li[data-id]");
    if (!li) return;
    document.querySelectorAll("#snapList li").forEach((x) => x.classList.toggle("active", x === li));
    const snap = await api(`${url}/${encodeURIComponent(li.dataset.id)}`);
    const n = snap.segments.filter((x) => x.type === "change").length;
    $("snapView").className = "";
    $("snapView").innerHTML = `<div class="toolbar"><span class="muted small-text">${n ? `${n} differences. <del class="dsample">Red</del> is only in the snapshot, <ins class="dsample">green</ins> was added since.` : "Same as the current chapter."}</span>
      <span class="spacer"></span><button id="snapRestore" class="small">Restore this version</button></div>
      <div class="review-body prose-plain snap-diff">${segmentsHtml(snap.segments)}</div>`;
    $("snapRestore").onclick = guard(async () => {
      if (!confirm("Replace the chapter with this version? The current text is saved as a snapshot first.")) return;
      await api(`${url}/${encodeURIComponent(li.dataset.id)}/restore`, { method: "POST" });
      await openChapter(S.chapter, true);
      await refreshBook();
      closeModal(); toast("Restored. The previous text is in History.");
    });
  });
  if (selectId) document.querySelector(`#snapList li[data-id="${selectId}"]`)?.click();
}
$("historyBtn").addEventListener("click", guard(() => showHistory()));

// ---------- find and replace ----------
function findOpts() {
  return { query: $("findQ").value, case: $("findCase").checked, whole_word: $("findWord").checked, regex: $("findRe").checked };
}
function showFind(initial = "") {
  openModal("Find and replace in the whole book", `
    <div class="row wrap"><input id="findQ" placeholder="Find…"><button id="findGo" class="small">Find</button></div>
    <div class="row wrap find-opts">
      <label class="toggle"><input type="checkbox" id="findCase"> Match case</label>
      <label class="toggle"><input type="checkbox" id="findWord" checked> Whole words</label>
      <label class="toggle"><input type="checkbox" id="findRe"> Regular expression</label></div>
    <div class="row wrap"><input id="findR" placeholder="Replace with…"><button id="replaceGo" class="ghost small">Replace all</button></div>
    <div id="findOut" class="find-out"></div>`);
  $("findQ").value = initial;
  const run = guard(async () => {
    const res = await api(bookUrl("/search"), { method: "POST", body: findOpts() });
    const total = res.reduce((n, c) => n + c.count, 0);
    $("findOut").innerHTML = `<p class="muted small-text">${total} matches in ${res.length} chapters</p>` + res.map((c) =>
      `<h4>${esc(c.title)} <span class="muted small-text">(${c.count})</span></h4><ul class="list hits">${c.hits.map((h) =>
        `<li data-ch="${esc(c.chapter)}" data-s="${h.start}" data-e="${h.end}"><span>…${esc(h.before)}<mark>${esc(h.match)}</mark>${esc(h.after)}…</span></li>`).join("")}</ul>`).join("");
  });
  $("findGo").onclick = run;
  $("findQ").onkeydown = (e) => { if (e.key === "Enter") run(); };
  $("replaceGo").onclick = guard(async () => {
    const o = findOpts();
    if (!o.query) return toast("Type something to find", true);
    if (!confirm(`Replace every "${o.query}" with "${$("findR").value}" in the whole book? A snapshot of each changed chapter is saved first.`)) return;
    if (S.chapter && $("editor").value !== S.savedText) await saveChapter();
    const r = await api(bookUrl("/replace"), { method: "POST", body: { ...o, replacement: $("findR").value } });
    toast(`Replaced ${r.replaced} in ${Object.keys(r.chapters).length} chapters. Undo from History if needed.`);
    if (S.chapter) await openChapter(S.chapter, true);
    await refreshBook();
    run();
  });
  $("findOut").onclick = guard(async (e) => {
    const li = e.target.closest("li[data-ch]");
    if (!li) return;
    if (li.dataset.ch !== S.chapter) await openChapter(li.dataset.ch);
    closeModal();
    selectInEditor(+li.dataset.s, +li.dataset.e);
  });
  if (initial) run();
  setTimeout(() => $("findQ")?.focus(), 50);
}
$("findBtn").addEventListener("click", () => {
  const ed = $("editor");
  showFind(ed.value.slice(ed.selectionStart, ed.selectionEnd).slice(0, 80));
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "f") { e.preventDefault(); $("findBtn").click(); }
});

// ---------- style check ----------
const STYLE_LABELS = {
  long_sentence: "Long sentences", adverb: "Adverbs", passive: "Passive voice", filter: "Filter words",
  weak: "Weak words", echo: "Echoes (same word close together)", repeated_phrase: "Repeated phrases",
};
$("styleBtn").addEventListener("click", guard(async () => {
  const ed = $("editor");
  const sel = ed.selectionStart !== ed.selectionEnd;
  const base = sel ? ed.selectionStart : 0;
  const text = sel ? ed.value.slice(ed.selectionStart, ed.selectionEnd) : ed.value;
  if (!text.trim()) return toast("There is no text to check", true);
  const r = await api("/api/style", { method: "POST", body: { text } });
  const st = r.stats;
  const ease = st.reading_ease >= 70 ? "easy" : st.reading_ease >= 50 ? "fairly easy" : st.reading_ease >= 30 ? "hard" : "very hard";
  const groups = Object.keys(STYLE_LABELS).filter((t) => r.counts[t]).map((t) => {
    const items = r.issues.filter((i) => i.type === t);
    return `<details><summary>${esc(STYLE_LABELS[t])} <span class="badge soft">${items.length}</span></summary><ul class="list hits">${
      items.slice(0, 200).map((i) => `<li data-s="${i.start + base}" data-e="${i.end + base}" title="${esc(i.message)}"><span><mark>${esc(i.text.slice(0, 80))}</mark> <span class="muted small-text">${esc(i.message)}</span></span></li>`).join("")}</ul></details>`;
  }).join("");
  $("styleOut").innerHTML = `
    <div class="stats"><div><strong>${st.words}</strong><span>words</span></div><div><strong>${st.avg_sentence_words}</strong><span>words per sentence</span></div>
      <div><strong>${Math.round(st.reading_ease)}</strong><span>reading ease (${ease})</span></div><div><strong>${st.dialogue_percent}%</strong><span>dialogue</span></div></div>
    ${groups || `<p class="muted">No issues found.</p>`}
    ${r.overused.length ? `<details><summary>Most used words</summary><p class="small-text">${r.overused.map((w) => `${esc(w.word)} <span class="muted">${w.count}</span>`).join(" · ")}</p></details>` : ""}`;
}));
$("styleOut").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-s]");
  if (li) selectInEditor(+li.dataset.s, +li.dataset.e);
});

// ---------- brainstorm ----------
const BRAINSTORM = {
  next: () => "Give me 5 different ideas for what could happen next in the story, from where my cursor is. One or two sentences each, as a numbered list.",
  twists: () => "Suggest 5 plot twists that would fit this story and surprise the reader without breaking any established facts. Numbered list, one or two sentences each.",
  whatif: () => "Ask me 7 'What if…' questions that could take the story in interesting directions. Numbered list.",
  names: () => { const k = prompt("Names for what? (e.g. a tavern, a northern village, a villain)"); return k && `Suggest 10 names for ${k} that fit the world and tone of this story, each with a few words on why it fits. Numbered list.`; },
  describe: () => { const k = prompt("Describe what? (e.g. the market at dawn)"); return k && `Describe ${k} using all five senses, in the style of this story. Give a short bullet list of vivid details I can use.`; },
};
document.querySelector(".brainstorm").addEventListener("click", guard(async (e) => {
  const fn = BRAINSTORM[e.target.dataset.brainstorm];
  if (!fn) return;
  const text = fn();
  if (!text) return;
  $("chatInput").value = text;
  await sendChat();
}));

// ---------- entity templates & rename everywhere ----------
function renderEntityFields(type, values) {
  const fields = (S.config.entity_fields || {})[type] || [];
  $("entTemplate").classList.toggle("hidden", !fields.length);
  $("entFields").innerHTML = fields.map(([k, label]) =>
    `<label>${esc(label)} <textarea rows="1" data-field="${esc(k)}">${esc(values[k] || "")}</textarea></label>`).join("");
}
function readEntityFields() {
  const out = {};
  document.querySelectorAll("#entFields [data-field]").forEach((t) => { if (t.value.trim()) out[t.dataset.field] = t.value.trim(); });
  return out;
}
$("entType").addEventListener("change", () => renderEntityFields($("entType").value, readEntityFields()));
$("renameEverywhereBtn").addEventListener("click", guard(async () => {
  const e = S.graph.entities.find((x) => x.id === S.editingEntity);
  if (!e) return;
  const name = prompt(`Rename "${e.name}" in the knowledge map and in every chapter to:`, e.name);
  if (!name || name.trim() === e.name) return;
  if (S.chapter && $("editor").value !== S.savedText) await saveChapter();
  const r = await api(bookUrl("/replace"), { method: "POST",
    body: { query: e.name, replacement: name.trim(), case: true, whole_word: true, regex: false } });
  for (const x of S.graph.entities) {
    x.facts = (x.facts || []).map((f) => f.split(e.name).join(name.trim()));
    if (x.description) x.description = x.description.split(e.name).join(name.trim());
  }
  for (const k in S.graph.summaries || {}) S.graph.summaries[k] = S.graph.summaries[k].split(e.name).join(name.trim());
  e.name = name.trim(); $("entName").value = e.name;
  await saveGraph();
  if (S.chapter) await openChapter(S.chapter, true);
  await refreshBook();
  toast(`Renamed. ${r.replaced} replacements in ${Object.keys(r.chapters).length} chapters (snapshots saved in History).`);
}));

// ---------- plan: outline / corkboard ----------
let planView = "outline";
document.querySelectorAll(".plan-tabs button").forEach((b) => b.addEventListener("click", guard(async () => {
  planView = b.dataset.plan;
  document.querySelectorAll(".plan-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $("plan-outline").classList.toggle("hidden", planView !== "outline");
  $("plan-timeline").classList.toggle("hidden", planView !== "timeline");
  await loadPlan();
})));
async function loadPlan() {
  if (!S.book) return;
  if (planView === "outline") return loadOutline();
  return loadTimeline();
}
async function loadOutline() {
  const o = await api(bookUrl("/outline"));
  S.outline = o;
  $("planHint").textContent = "Drag cards (or use ◀ ▶) to reorder chapters. Synopses help the AI when a chapter has no summary yet.";
  $("corkboard").innerHTML = o.chapters.map((c, i) => `
    <div class="card status-${esc(c.status)}" draggable="true" data-id="${esc(c.id)}">
      <div class="card-head"><strong>${i + 1}. ${esc(c.title)}</strong><span class="muted small-text">${c.words}w</span></div>
      <textarea class="synopsis" rows="4" placeholder="Synopsis: what happens in this chapter?">${esc(c.synopsis)}</textarea>
      ${c.summary && !c.synopsis ? `<button class="ghost small" data-use-summary="${esc(c.id)}" title="${esc(c.summary)}">Use AI summary</button>` : ""}
      <div class="card-foot">
        <select class="status">${o.statuses.map((s) => `<option ${s === c.status ? "selected" : ""}>${s}</option>`).join("")}</select>
        <span class="spacer"></span>
        <button class="ghost small" data-move="-1" title="Move left">◀</button>
        <button class="ghost small" data-move="1" title="Move right">▶</button>
        <button class="ghost small" data-open="${esc(c.id)}">Open</button>
      </div>
    </div>`).join("");
}
async function patchOutline(id, body) {
  await api(bookUrl(`/outline/${encodeURIComponent(id)}`), { method: "PATCH", body });
}
$("corkboard").addEventListener("change", guard(async (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  if (e.target.classList.contains("synopsis")) await patchOutline(card.dataset.id, { synopsis: e.target.value });
  if (e.target.classList.contains("status")) {
    await patchOutline(card.dataset.id, { status: e.target.value });
    card.className = "card status-" + e.target.value;
  }
}));
async function moveChapter(id, target, after) {
  const order = S.bookData.chapters.map((c) => c.id).filter((x) => x !== id);
  let i = order.indexOf(target);
  if (after) i += 1;
  order.splice(i, 0, id);
  await api(bookUrl("/order"), { method: "PUT", body: { order } });
  await refreshBook();
  await loadOutline();
}
$("corkboard").addEventListener("click", guard(async (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  if (e.target.dataset.open) { await openChapter(e.target.dataset.open); showTab("write"); }
  if (e.target.dataset.useSummary) {
    const c = S.outline.chapters.find((x) => x.id === card.dataset.id);
    await patchOutline(c.id, { synopsis: c.summary }); await loadOutline();
  }
  if (e.target.dataset.move) {
    const ids = S.bookData.chapters.map((c) => c.id);
    const i = ids.indexOf(card.dataset.id), j = i + +e.target.dataset.move;
    if (j < 0 || j >= ids.length) return;
    await moveChapter(card.dataset.id, ids[j], +e.target.dataset.move > 0);
  }
}));
let cardDrag = null;
$("corkboard").addEventListener("dragstart", (e) => { cardDrag = e.target.closest(".card")?.dataset.id; });
$("corkboard").addEventListener("dragover", (e) => e.preventDefault());
$("corkboard").addEventListener("drop", guard(async (e) => {
  e.preventDefault();
  const target = e.target.closest(".card")?.dataset.id;
  if (!cardDrag || !target || target === cardDrag) return;
  const ids = S.bookData.chapters.map((c) => c.id);
  await moveChapter(cardDrag, target, ids.indexOf(cardDrag) < ids.indexOf(target));
}));

// ---------- plan: timeline / plot threads ----------
async function loadTimeline() {
  S.timeline = await api(bookUrl("/timeline"));
  if (!S.graph.entities.length) S.graph = await api(bookUrl("/knowledge"));
  $("planHint").textContent = "";
  renderTimeline();
}
async function saveTimeline() {
  S.timeline = await api(bookUrl("/timeline"), { method: "PUT", body: S.timeline });
  renderTimeline();
}
function renderTimeline() {
  const t = S.timeline, chs = S.bookData.chapters;
  const cols = [...chs.map((c) => ({ id: c.id, title: c.title })), { id: "", title: "Not placed" }];
  const head = `<div class="tl-cell tl-corner"></div>` + cols.map((c, i) =>
    `<div class="tl-cell tl-col">${c.id ? `${i + 1}. ` : ""}${esc(c.title)}</div>`).join("");
  const rows = t.threads.map((th) => {
    const cells = cols.map((c) => {
      const evs = t.events.filter((e) => e.thread === th.id && (e.chapter || "") === c.id).sort((a, b) => a.order - b.order);
      return `<div class="tl-cell">${evs.map((e) => `<div class="tl-event" data-ev="${esc(e.id)}" style="border-color:${esc(th.color)}">
        <strong>${esc(e.title)}</strong>${e.when ? `<div class="muted small-text">${esc(e.when)}</div>` : ""}</div>`).join("")}
        <button class="ghost small tl-add" data-add="${esc(th.id)}|${esc(c.id)}" title="Add event">+</button></div>`;
    }).join("");
    return `<div class="tl-cell tl-row" data-thread="${esc(th.id)}"><span class="dot" style="background:${esc(th.color)}"></span>${esc(th.name)}</div>${cells}`;
  }).join("");
  $("timelineGrid").innerHTML = `<div class="timeline" style="grid-template-columns: 150px repeat(${cols.length}, minmax(150px, 1fr))">${head}${rows}</div>`;
}
$("addThreadBtn").addEventListener("click", guard(async () => {
  const name = prompt("Name of the plot thread (e.g. Romance, The heist, Mara's secret):");
  if (!name) return;
  S.timeline.threads.push({ id: Math.random().toString(16).slice(2, 10), name });
  await saveTimeline();
}));
function editEvent(ev) {
  const t = S.timeline;
  const chs = S.bookData.chapters;
  openModal(ev.id ? "Edit event" : "New event", `
    <label>Title <input id="evTitle" value="${esc(ev.title || "")}" placeholder="e.g. Mara steals the key"></label>
    <label>When (story time, optional) <input id="evWhen" value="${esc(ev.when || "")}" placeholder="e.g. Day 3, night"></label>
    <label>Notes <textarea id="evNotes" rows="3">${esc(ev.notes || "")}</textarea></label>
    <div class="row wrap">
      <label>Plot thread <select id="evThread">${t.threads.map((th) => `<option value="${esc(th.id)}" ${th.id === ev.thread ? "selected" : ""}>${esc(th.name)}</option>`).join("")}</select></label>
      <label>Chapter <select id="evChapter"><option value="">Not placed</option>${chs.map((c) => `<option value="${esc(c.id)}" ${c.id === ev.chapter ? "selected" : ""}>${esc(c.title)}</option>`).join("")}</select></label>
    </div>
    ${S.graph.entities.length ? `<details><summary>Who and what is involved</summary><div class="ent-checks">${S.graph.entities.map((e) =>
      `<label class="toggle"><input type="checkbox" value="${esc(e.id)}" ${(ev.entities || []).includes(e.id) ? "checked" : ""}> ${esc(e.name)}</label>`).join("")}</div></details>` : ""}
    <div class="row end">${ev.id ? `<button id="evDelete" class="ghost small danger">Delete</button>` : ""}<button id="evSave" class="primary small">Save</button></div>`);
  $("evSave").onclick = guard(async () => {
    const data = { title: $("evTitle").value.trim(), when: $("evWhen").value.trim(), notes: $("evNotes").value.trim(),
      thread: $("evThread").value, chapter: $("evChapter").value,
      entities: [...document.querySelectorAll(".ent-checks input:checked")].map((x) => x.value) };
    if (!data.title) return toast("Give the event a title", true);
    if (ev.id) Object.assign(t.events.find((x) => x.id === ev.id), data);
    else t.events.push({ ...data, id: Math.random().toString(16).slice(2, 10), order: Date.now() / 1e12 });
    closeModal();
    await saveTimeline();
  });
  if (ev.id) $("evDelete").onclick = guard(async () => {
    t.events = t.events.filter((x) => x.id !== ev.id);
    closeModal();
    await saveTimeline();
  });
  setTimeout(() => $("evTitle")?.focus(), 50);
}
$("timelineGrid").addEventListener("click", guard(async (e) => {
  const add = e.target.dataset.add;
  if (add) { const [thread, chapter] = add.split("|"); return editEvent({ thread, chapter }); }
  const evEl = e.target.closest("[data-ev]");
  if (evEl) return editEvent(S.timeline.events.find((x) => x.id === evEl.dataset.ev));
  const row = e.target.closest("[data-thread]");
  if (row) {
    const th = S.timeline.threads.find((x) => x.id === row.dataset.thread);
    const name = prompt("Rename the plot thread (leave empty to delete it and its events):", th.name);
    if (name === null) return;
    if (!name.trim()) {
      if (S.timeline.threads.length === 1) return toast("Keep at least one plot thread", true);
      if (!confirm(`Delete "${th.name}" and its events?`)) return;
      S.timeline.threads = S.timeline.threads.filter((x) => x.id !== th.id);
      S.timeline.events = S.timeline.events.filter((x) => x.thread !== th.id);
    } else th.name = name.trim();
    await saveTimeline();
  }
}));

// ---------- installable app ----------
if ("serviceWorker" in navigator && location.protocol !== "file:") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// ---------- settings ----------
async function loadSettings() {
  $("providerTable").innerHTML = S.config.providers.map((p) =>
    `<tr><td>${esc(p.name)}${p.name === S.config.default_provider ? " <span class='muted'>(default)</span>" : ""}</td>
     <td>${esc(p.type)}</td><td>${esc(p.model)}</td><td id="pst-${esc(p.name)}" class="muted">checking…</td></tr>`).join("");
  for (const p of S.config.providers) {
    api(`/api/providers/${encodeURIComponent(p.name)}/check`).then((r) => {
      const td = $("pst-" + p.name);
      if (td) { td.textContent = (r.ok ? "✓ " : "✗ ") + r.status; td.style.color = r.ok ? "" : "var(--danger)"; }
    }).catch(() => {});
  }
  if (document.body.dataset.admin === "false") return; // only admins may read or change config files
  await loadCfgFile();
}
async function loadCfgFile() {
  $("cfgEditor").value = await api(`/api/config/files/${$("cfgFile").value}`);
  $("cfgStatus").textContent = "";
}
$("cfgFile").addEventListener("change", guard(loadCfgFile));
$("saveCfgBtn").addEventListener("click", guard(async () => {
  try {
    await api(`/api/config/files/${$("cfgFile").value}`, { method: "PUT", body: { text: $("cfgEditor").value } });
  } catch (e) { $("cfgStatus").textContent = "✗ " + e.message; throw e; }
  $("cfgStatus").textContent = "✓ Saved and reloaded";
  await loadConfig();
  await loadSettings();
}));

// ---------- start ----------
(async () => {
  try {
    await loadConfig();
    await loadBooks();
    renderJobs();
    const running = (await api("/api/jobs")).filter((j) => j.status === "running");
    running.forEach((j) => watchJob(j.id, S.book));
  } catch (e) { toast("Could not start: " + e.message, true); }
})();
