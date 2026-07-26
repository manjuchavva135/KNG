/* PressMeets RAG — History.
 *
 * Same rule as the chat client: every piece of corpus or user text goes in with
 * `textContent`, never `innerHTML`. Titles here are user-supplied and answers are
 * OCR'd third-party material, so both are untrusted markup.
 *
 * The transcript drawer shows an answer exactly as it was stored — the verified
 * text from the `final` event, with its citation counts. It never re-runs the
 * question, so reading history costs nothing.
 */

const el = (id) => document.getElementById(id);
const state = { sessions: [], query: "" };

/* ── boot ──────────────────────────────────────────────────────────────── */
async function boot() {
  const me = await fetch("/api/me");
  if (!me.ok) { location.href = "/login"; return; }
  const { user } = await me.json();
  el("user-email").textContent = user.email;
  el("avatar").textContent = (user.email[0] || "?").toUpperCase();
  if (user.role === "admin") el("nav-admin").hidden = false;
  load();
}

async function load() {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  const res = await fetch(`/api/history?${params}`);
  if (!res.ok) { location.href = "/login"; return; }
  const { sessions } = await res.json();
  state.sessions = sessions;
  render();
}

/* ── grouping ──────────────────────────────────────────────────────────── */
/* Dates are stored as local "YYYY-MM-DDTHH:MM:SS" strings by the server. */
function dayKey(stamp) {
  return (stamp || "").slice(0, 10);
}

function groupLabel(key) {
  if (!key) return "Undated";
  const today = new Date();
  const iso = (d) => d.toISOString().slice(0, 10);
  const shift = (n) => {
    const d = new Date(today);
    d.setDate(d.getDate() - n);
    return iso(d);
  };
  if (key === iso(today)) return "Today";
  if (key === shift(1)) return "Yesterday";
  if (key > shift(7)) return "Earlier this week";
  if (key > shift(30)) return "Earlier this month";
  return key.slice(0, 7);            // YYYY-MM for anything older
}

function render() {
  const groups = el("groups");
  groups.textContent = "";

  const total = state.sessions.length;
  const turns = state.sessions.reduce((n, s) => n + (s.turns || 0), 0);
  const uncited = state.sessions.reduce((n, s) => n + (s.uncited_sentences || 0), 0);
  const stripped = state.sessions.reduce((n, s) => n + (s.stripped_citations || 0), 0);
  const box = el("totals");
  box.textContent = "";
  [["conversations", total], ["questions", turns],
   ["uncited sentences", uncited], ["citations stripped", stripped]]
    .forEach(([k, v]) => {
      const row = document.createElement("div");
      row.className = "stat-line";
      const kk = document.createElement("span");
      kk.textContent = k;
      const vv = document.createElement("strong");
      vv.textContent = v;
      row.appendChild(kk); row.appendChild(vv);
      box.appendChild(row);
    });

  el("page-sub").textContent = state.query
    ? `${total} conversation(s) matching “${state.query}”.`
    : "Every question you have asked, newest first. Open one to continue it in Chat.";
  // Set both lines every render: a search that matched nothing must not leave
  // "Nothing matched that search." behind once the box is cleared.
  const note = el("empty-note");
  note.hidden = total > 0;
  note.firstElementChild.textContent = state.query
    ? "Nothing matched that search."
    : "No conversations yet.";
  note.lastElementChild.textContent = state.query
    ? "Try fewer words, or clear the search box."
    : "Ask something in Chat and it will appear here.";

  let currentKey = null;
  state.sessions.forEach((s) => {
    const key = dayKey(s.updated_at);
    if (key !== currentKey) {
      currentKey = key;
      const h = document.createElement("h2");
      h.className = "day";
      h.textContent = groupLabel(key);
      groups.appendChild(h);
    }
    groups.appendChild(card(s));
  });
}

/* ── one conversation card ─────────────────────────────────────────────── */
function card(s) {
  const wrap = document.createElement("article");
  wrap.className = "hcard";

  const head = document.createElement("div");
  head.className = "hcard-head";
  const title = document.createElement("button");
  title.className = "hcard-title";
  title.textContent = s.title || s.last_question || s.session_id;
  title.title = "Open the transcript";
  title.onclick = () => openTranscript(s.session_id);
  const when = document.createElement("span");
  when.className = "hcard-when";
  when.textContent = (s.updated_at || "").replace("T", " ");
  head.appendChild(title);
  head.appendChild(when);

  const preview = document.createElement("p");
  preview.className = "hcard-preview";
  preview.textContent = s.preview ? `${s.preview}…` : "(no answer recorded)";

  const badges = document.createElement("div");
  badges.className = "hcard-badges";
  badge(badges, `${s.turns} question${s.turns === 1 ? "" : "s"}`);
  if (s.sources) badge(badges, `${s.cited} of ${s.sources} sources cited`);
  if (s.latency_s !== null && s.latency_s !== undefined) badge(badges, `${s.latency_s}s`);
  if (s.uncited_sentences) badge(badges, `${s.uncited_sentences} uncited`, "warn");
  if (s.stripped_citations) badge(badges, `${s.stripped_citations} citation(s) stripped`, "warn");

  const actions = document.createElement("div");
  actions.className = "hcard-actions";
  action(actions, "Continue in Chat", () => {
    location.href = `/?session=${encodeURIComponent(s.session_id)}`;
  });
  action(actions, "Rename", async () => {
    const next = prompt("Rename this conversation", s.title || "");
    if (next === null) return;
    const res = await fetch(`/api/history/${encodeURIComponent(s.session_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: next }),
    });
    if (res.ok) load();
  });
  action(actions, "Export JSON", () => {
    location.href = `/api/history/${encodeURIComponent(s.session_id)}`;
  });
  action(actions, "Delete", async () => {
    if (!confirm(`Delete “${s.title || s.session_id}”? This cannot be undone.`)) return;
    const res = await fetch(`/api/history/${encodeURIComponent(s.session_id)}`,
                            { method: "DELETE" });
    if (res.ok) load();
  }, "danger");

  wrap.appendChild(head);
  wrap.appendChild(preview);
  wrap.appendChild(badges);
  wrap.appendChild(actions);
  return wrap;
}

function badge(parent, text, kind) {
  const b = document.createElement("span");
  b.className = kind === "warn" ? "hbadge warn" : "hbadge";
  b.textContent = text;
  parent.appendChild(b);
}

function action(parent, label, fn, kind) {
  const b = document.createElement("button");
  b.className = kind === "danger" ? "btn-link danger" : "btn-link";
  b.textContent = label;
  b.onclick = fn;
  parent.appendChild(b);
}

/* ── transcript drawer ─────────────────────────────────────────────────── */
async function openTranscript(id) {
  const drawer = el("drawer");
  drawer.classList.add("open");
  el("drawer-title").textContent = "Conversation";
  el("drawer-meta").textContent = "Loading…";
  el("drawer-turns").textContent = "";

  const res = await fetch(`/api/history/${encodeURIComponent(id)}`);
  if (!res.ok) {
    el("drawer-meta").textContent = `Could not open this conversation (${res.status}).`;
    return;
  }
  const session = await res.json();
  el("drawer-title").textContent = session.title || id;
  el("drawer-meta").textContent =
    `${(session.turns || []).length} question(s) · started ${(session.created_at || "").replace("T", " ")}`;

  const box = el("drawer-turns");
  (session.turns || []).forEach((t) => {
    const q = document.createElement("div");
    q.className = "question";
    q.textContent = t.question || "";
    const a = document.createElement("div");
    a.className = "answer";
    a.textContent = t.answer || "(no answer recorded)";

    const meta = document.createElement("div");
    meta.className = "hcard-badges";
    badge(meta, `${(t.cited || []).length} cited`);
    if ((t.sources || []).length) badge(meta, `${t.sources.length} sources`);
    if (t.latency_s) badge(meta, `${t.latency_s}s`);
    if (t.uncited_sentences) badge(meta, `${t.uncited_sentences} uncited`, "warn");
    if ((t.invalid_citations || []).length) {
      badge(meta, `stripped ${t.invalid_citations.join(", ")}`, "warn");
    }

    const turn = document.createElement("div");
    turn.className = "turn";
    turn.appendChild(q);
    turn.appendChild(a);
    turn.appendChild(meta);
    box.appendChild(turn);
  });
}

/* ── wiring ────────────────────────────────────────────────────────────── */
let searchTimer = null;
el("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const value = e.target.value;
  searchTimer = setTimeout(() => { state.query = value.trim(); load(); }, 180);
});

el("clear-all").onclick = async () => {
  if (!confirm("Delete every conversation in your history? This cannot be undone.")) return;
  const res = await fetch("/api/history", { method: "DELETE" });
  if (res.ok) { state.query = ""; el("search").value = ""; load(); }
};

el("new-chat").onclick = () => { location.href = "/"; };
el("drawer-close").onclick = () => el("drawer").classList.remove("open");
el("signout").onclick = async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
};

boot();
