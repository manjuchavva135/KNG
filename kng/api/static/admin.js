/* PressMeets RAG — admin console.
 *
 * `textContent` only, as everywhere else: the users table shows addresses and the
 * query table shows real questions, and neither is markup.
 *
 * Destructive actions are refused server-side too (`_would_orphan`, self-delete,
 * confirm echo). The dialogs here are the courtesy; the API is the rule.
 */

const el = (id) => document.getElementById(id);
const state = { me: null, users: [] };

function say(message, ok) {
  el("user-error").textContent = ok ? "" : message;
  el("user-ok").textContent = ok ? message : "";
}

function stat(container, key, value) {
  const d = document.createElement("div");
  d.className = "stat";
  const v = document.createElement("div");
  v.className = "v";
  v.textContent = value === null || value === undefined ? "—" : value;
  const k = document.createElement("div");
  k.className = "k";
  k.textContent = key;
  d.appendChild(v); d.appendChild(k);
  container.appendChild(d);
}

async function boot() {
  const me = await fetch("/api/me");
  if (!me.ok) { location.href = "/login"; return; }
  const { user } = await me.json();
  state.me = user;
  el("user-email").textContent = user.email;
  el("avatar").textContent = (user.email[0] || "?").toUpperCase();

  const res = await fetch("/api/admin/stats");
  if (res.status === 403) {
    document.body.textContent = "Admin access required.";
    return;
  }
  const { corpus, queries } = await res.json();

  const s = el("stats");
  stat(s, "questions asked", queries.queries);
  stat(s, "mean latency (s)", queries.mean_latency_s);
  stat(s, "mean uncited sentences", queries.mean_uncited_sentences);
  stat(s, "answers with stripped citations", queries.answers_with_stripped_citations);

  const c = el("corpus");
  stat(c, "passages indexed", corpus.chunks.toLocaleString());
  stat(c, "press meets", corpus.press_meets.length);
  stat(c, "graph nodes", corpus.graph.nodes.toLocaleString());
  stat(c, "graph edges", corpus.graph.edges.toLocaleString());
  stat(c, "coverage", `${corpus.coverage.start} → ${corpus.coverage.end}`);

  const qbody = el("queries");
  (queries.entries || []).forEach((q) => {
    const tr = document.createElement("tr");
    [q.ts, q.user, q.question, q.latency_s, q.cited, q.uncited_sentences]
      .forEach((v, i) => {
        const td = document.createElement("td");
        if (i >= 3) td.className = "num";
        td.textContent = v === null || v === undefined ? "—" : v;
        tr.appendChild(td);
      });
    qbody.appendChild(tr);
  });

  loadUsers();
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) return res.json();
  const detail = (await res.json().catch(() => ({}))).detail;
  throw new Error(detail || `request failed (${res.status})`);
}

function rowAction(parent, label, fn, kind, blocked) {
  const b = document.createElement("button");
  b.className = kind ? `btn-small ${kind}` : "btn-small ghost";
  b.textContent = label;
  if (blocked) {
    // The API refuses these anyway; showing them live invites a click that can
    // only produce an error message.
    b.disabled = true;
    b.title = blocked;
  } else {
    b.onclick = async () => {
      b.disabled = true;
      try { await fn(); } catch (e) { say(e.message, false); }
      b.disabled = false;
    };
  }
  parent.appendChild(b);
}

async function loadUsers() {
  const { users } = await (await fetch("/api/admin/users")).json();
  state.users = users;
  const body = el("users");
  body.textContent = "";

  // The same two guards the API enforces, mirrored so the buttons tell the truth
  // before they are pressed.
  const enabledAdmins = users.filter((u) => u.role === "admin" && !u.disabled).length;

  users.forEach((u) => {
    const self = u.email === state.me.email;
    const lastAdmin = u.role === "admin" && !u.disabled && enabledAdmins === 1;
    const noAdminLeft = lastAdmin
      ? "This is the last enabled admin — promote someone else first."
      : "";
    const tr = document.createElement("tr");

    const email = document.createElement("td");
    email.textContent = u.email;
    if (self) {
      const you = document.createElement("span");
      you.className = "hbadge";
      you.textContent = "you";
      you.style.marginLeft = "7px";
      email.appendChild(you);
    }
    tr.appendChild(email);

    const role = document.createElement("td");
    role.textContent = u.role;
    tr.appendChild(role);

    const status = document.createElement("td");
    status.textContent = u.disabled ? "disabled" : "active";
    if (u.disabled) status.style.color = "var(--danger)";
    tr.appendChild(status);

    const created = document.createElement("td");
    created.textContent = (u.created_at || "").replace("T", " ");
    tr.appendChild(created);

    const actions = document.createElement("td");
    actions.className = "row-actions";

    rowAction(actions, u.disabled ? "Enable" : "Disable", async () => {
      await post("/api/admin/users/disable", { email: u.email, disabled: !u.disabled });
      say(`${u.email} is now ${u.disabled ? "active" : "disabled"}.`, true);
      loadUsers();
    }, u.disabled ? "" : "warn",
       u.disabled ? "" : (self ? "You cannot disable yourself." : noAdminLeft));

    rowAction(actions, u.role === "admin" ? "Make user" : "Make admin", async () => {
      const role = u.role === "admin" ? "user" : "admin";
      await post("/api/admin/users/role", { email: u.email, role });
      say(`${u.email} is now ${role}.`, true);
      loadUsers();
    }, "", u.role === "admin" ? noAdminLeft : "");

    rowAction(actions, "Delete", async () => {
      const typed = prompt(
        `Deleting ${u.email} also deletes every conversation in their history. ` +
        `This cannot be undone.\n\nType the address to confirm:`);
      if (typed === null) return;
      const out = await post("/api/admin/users/delete",
                             { email: u.email, confirm: typed });
      say(`Deleted ${u.email} and ${out.conversations_deleted} conversation(s).`, true);
      loadUsers();
    }, "warn", self ? "You cannot delete your own account." : noAdminLeft);

    tr.appendChild(actions);
    body.appendChild(tr);
  });

  const picker = el("reset-email");
  picker.textContent = "";
  users.forEach((u) => {
    const o = document.createElement("option");
    o.value = u.email;
    o.textContent = u.email;
    picker.appendChild(o);
  });
}

el("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  say("", true);
  try {
    await post("/api/admin/users", {
      email: el("new-email").value,
      password: el("new-password").value,
      admin: el("new-role").value === "admin",
    });
    say(`Added ${el("new-email").value}.`, true);
    el("new-email").value = ""; el("new-password").value = "";
    loadUsers();
  } catch (err) { say(err.message, false); }
});

el("reset-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  say("", true);
  const email = el("reset-email").value;
  try {
    await post("/api/admin/users/password",
               { email, password: el("reset-password").value });
    el("reset-password").value = "";
    say(`Password reset for ${email}. Any session it had is now signed out.`, true);
  } catch (err) { say(err.message, false); }
});

el("signout").onclick = async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
};

boot();
