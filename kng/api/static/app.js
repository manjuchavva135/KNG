/* PressMeets RAG — chat client.
 *
 * Two things here are load-bearing rather than cosmetic:
 *
 * 1. The streamed text is *provisional*. Citations can only be verified once the
 *    model stops, so when the `final` event arrives its text replaces what was
 *    streamed. Leaving the raw stream on screen would show a hallucinated `[9]`
 *    as though the server had checked it.
 * 2. All source text is inserted with `textContent`, never `innerHTML`. The
 *    corpus is OCR'd third-party material; treating it as markup would let a
 *    scanned page inject script.
 */

const state = {
  meta: null,
  sessionId: null,
  language: "",
  sources: [],
  busy: false,
};

const el = (id) => document.getElementById(id);

/* ── boot ──────────────────────────────────────────────────────────────── */
async function boot() {
  const me = await fetch("/api/me");
  if (!me.ok) { location.href = "/login"; return; }
  const { user } = await me.json();
  el("user-email").textContent = user.email;
  el("avatar").textContent = (user.email[0] || "?").toUpperCase();
  if (user.role === "admin") el("nav-admin").hidden = false;

  state.meta = await (await fetch("/api/meta")).json();
  fillFilters(state.meta);
  loadHistory();
}

function fillFilters(meta) {
  const meetSel = el("f-meet");
  meta.press_meets.forEach((m) => {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.date ? `${m.date} — ${m.title}` : m.title;
    meetSel.appendChild(o);
  });

  const typeSel = el("f-type");
  meta.source_types.forEach((t) => {
    const o = document.createElement("option");
    o.value = t.value;
    o.textContent = `${t.value} (${t.count})`;
    typeSel.appendChild(o);
  });

  const { start, end } = meta.coverage;
  el("coverage").textContent =
    `Archive covers ${start} → ${end}. Dates outside this range return no results.`;
  el("f-since").min = start; el("f-since").max = end;
  el("f-until").min = start; el("f-until").max = end;
  el("graph-pill").textContent =
    `${meta.graph.nodes.toLocaleString()} nodes · ${meta.chunks.toLocaleString()} passages`;
}

/* ── history sidebar ───────────────────────────────────────────────────── */
async function loadHistory() {
  const res = await fetch("/api/history");
  if (!res.ok) return;
  const { sessions } = await res.json();
  const list = el("history-list");
  list.textContent = "";
  el("history-field").hidden = sessions.length === 0;
  sessions.forEach((s) => {
    const b = document.createElement("button");
    b.className = "history-item";
    b.textContent = s.title || s.session_id;
    b.title = `${s.turns} turn(s) · ${s.updated_at}`;
    b.onclick = () => openSession(s.session_id);
    list.appendChild(b);
  });
}

async function openSession(id) {
  const res = await fetch(`/api/history/${encodeURIComponent(id)}`);
  if (!res.ok) return;
  const session = await res.json();
  state.sessionId = session.session_id;
  const stream = el("stream");
  stream.textContent = "";
  const wrap = document.createElement("div");
  wrap.className = "centered";
  stream.appendChild(wrap);
  (session.turns || []).forEach((t) => {
    const turn = addTurn(t.question, wrap);
    turn.answerBox.textContent = "";
    renderAnswer(turn.answerBox, t.answer || "", t.sources || []);
    renderWarnings(turn.answerBox, t);
    renderSources(turn.answerBox, t.sources || [], t.cited || []);
  });
}

/* ── asking ────────────────────────────────────────────────────────────── */
function currentBody(question) {
  return {
    question,
    k: parseInt(el("f-k").value, 10) || 8,
    language: state.language || null,
    press_meet_id: el("f-meet").value || null,
    source_type: el("f-type").value || null,
    since: el("f-since").value || null,
    until: el("f-until").value || null,
    use_graph: el("f-graph").checked,
    session_id: state.sessionId,
  };
}

function addTurn(question, container) {
  const parent = container || document.querySelector("#stream .centered");
  const wrap = document.createElement("div");
  wrap.className = "turn";

  const q = document.createElement("div");
  q.className = "question";
  q.textContent = question;

  const a = document.createElement("div");
  a.className = "answer";
  const status = document.createElement("div");
  status.className = "status";
  status.textContent = "Retrieving sources…";
  a.appendChild(status);

  wrap.appendChild(q);
  wrap.appendChild(a);
  parent.appendChild(wrap);
  el("stream").scrollTop = el("stream").scrollHeight;
  return { answerBox: a, status };
}

/* Render answer text, turning [n] into clickable pills. textContent only. */
function renderAnswer(box, text, sources) {
  box.textContent = "";
  const body = document.createElement("div");
  const parts = text.split(/(\[\d+(?:\s*,\s*\d+)*\])/g);
  parts.forEach((part) => {
    const m = part.match(/^\[([\d,\s]+)\]$/);
    if (!m) { body.appendChild(document.createTextNode(part)); return; }
    m[1].split(",").map((s) => s.trim()).filter(Boolean).forEach((n) => {
      const pill = document.createElement("span");
      pill.className = "cite";
      pill.textContent = n;
      const src = (sources || []).find((s) => String(s.n) === n);
      if (src) pill.onclick = () => openSource(src);
      body.appendChild(pill);
      body.appendChild(document.createTextNode(" "));
    });
  });
  box.appendChild(body);
}

function renderWarnings(box, info) {
  const bits = [];
  if (info.invalid_citations && info.invalid_citations.length) {
    bits.push(`${info.invalid_citations.length} citation(s) pointed at no source and were removed (${info.invalid_citations.join(", ")}).`);
  }
  if (info.uncited_sentences) {
    bits.push(`${info.uncited_sentences} sentence(s) carry no citation — treat them with care.`);
  }
  if (!bits.length) return;
  const bar = document.createElement("div");
  bar.className = "warnbar";
  bar.textContent = bits.join(" ");
  box.appendChild(bar);
}

function renderSources(box, sources, cited) {
  if (!sources.length) return;
  const wrap = document.createElement("div");
  wrap.className = "sources";
  const h = document.createElement("h3");
  const used = cited && cited.length ? `Sources (${cited.length} cited of ${sources.length})` : `Sources (${sources.length})`;
  h.textContent = used;
  wrap.appendChild(h);

  const show = cited && cited.length ? sources.filter((s) => cited.includes(s.n)) : sources.slice(0, 8);
  show.forEach((s) => {
    const row = document.createElement("div");
    row.className = "source";
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = `[${s.n}]`;
    const mid = document.createElement("div");
    const title = document.createElement("div");
    title.textContent = s.citation || s.source_file || "(source)";
    const meta = document.createElement("div");
    meta.className = "meta";
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = s.kind || "passage";
    meta.appendChild(kind);
    meta.appendChild(document.createTextNode(
      ` ${[s.date, s.language, s.source_type].filter(Boolean).join(" · ")}`));
    mid.appendChild(title);
    mid.appendChild(meta);
    row.appendChild(n);
    row.appendChild(mid);
    row.onclick = () => openSource(s);
    wrap.appendChild(row);
  });
  box.appendChild(wrap);
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  el("send").disabled = true;
  const empty = el("empty");
  if (empty) empty.remove();
  if (!document.querySelector("#stream .centered")) {
    const wrap = document.createElement("div");
    wrap.className = "centered";
    el("stream").appendChild(wrap);
  }

  const { answerBox, status } = addTurn(question);
  let streamed = "";
  let sources = [];

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentBody(question)),
    });
    if (!res.ok) throw new Error(`server said ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      for (const frame of frames) {
        const evLine = frame.split("\n").find((l) => l.startsWith("event:"));
        const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!evLine || !dataLine) continue;
        const event = evLine.slice(6).trim();
        let data;
        try { data = JSON.parse(dataLine.slice(5).trim()); } catch { continue; }

        if (event === "meta") {
          state.sessionId = data.session_id;
        } else if (event === "sources") {
          sources = data;
          state.sources = data;
          status.textContent = `${data.length} source(s) retrieved — writing the answer…`;
        } else if (event === "delta") {
          streamed += data.text;
          // Provisional text: citations are not verified until `final`.
          answerBox.textContent = streamed;
        } else if (event === "error") {
          status.textContent = `Error: ${data.message}`;
        } else if (event === "final") {
          renderAnswer(answerBox, data.text || streamed, data.sources || sources);
          renderWarnings(answerBox, data);
          renderSources(answerBox, data.sources || sources, data.cited || []);
          loadHistory();
        }
      }
      el("stream").scrollTop = el("stream").scrollHeight;
    }
  } catch (e) {
    const err = document.createElement("div");
    err.className = "warnbar";
    err.textContent = `Could not complete the answer: ${e.message}`;
    answerBox.appendChild(err);
  } finally {
    state.busy = false;
    el("send").disabled = false;
    el("q").value = "";
  }
}

/* ── source drawer ─────────────────────────────────────────────────────── */
async function openSource(source) {
  const drawer = el("drawer");
  drawer.classList.add("open");
  el("drawer-title").textContent = source.citation || "Source";
  el("drawer-meta").textContent = "Loading…";
  el("drawer-text").textContent = "";
  el("drawer-neighbours").textContent = "";

  const file = source.source_file;
  if (!file) {
    // Graph facts carry a citation string but not always a file path.
    el("drawer-meta").textContent = "This is a graph fact; its evidence quote is shown in the answer.";
    el("drawer-text").textContent = source.text || "";
    return;
  }
  const params = new URLSearchParams({ file });
  // chunk_id targets the exact cited passage; page is the fallback when a fact's
  // evidence carries only a page number.
  if (source.chunk_id) params.set("chunk_id", source.chunk_id);
  else if (source.page) params.set("page", source.page);
  const res = await fetch(`/api/source?${params}`);
  if (!res.ok) {
    el("drawer-meta").textContent = `Could not open this source (${res.status}).`;
    return;
  }
  const s = await res.json();
  const bits = [s.press_meet_title, s.date, s.source_type, s.language,
                s.page ? `p.${s.page}` : null,
                s.slide ? `slide ${s.slide}` : null,
                `passage ${s.position.index + 1} of ${s.position.total}`]
    .filter(Boolean).join(" · ");
  el("drawer-meta").textContent = bits;
  el("drawer-text").textContent = s.text || "(no text indexed)";

  if (s.raw_available) {
    const a = document.createElement("a");
    a.href = `/api/raw?file=${encodeURIComponent(file)}`;
    a.textContent = "Open the original file";
    a.style.cssText = "display:inline-block;margin-top:14px;color:var(--green)";
    el("drawer-neighbours").appendChild(a);
  }
  (s.neighbours || []).forEach((n) => {
    const b = document.createElement("button");
    b.className = "history-item";
    b.style.cssText = "display:block;width:100%;margin-top:6px;white-space:normal";
    b.textContent = `${n.page ? "p." + n.page + " — " : ""}${n.preview}…`;
    b.onclick = () => openSource({ source_file: file, chunk_id: n.chunk_id, citation: n.citation });
    el("drawer-neighbours").appendChild(b);
  });
}

/* ── wiring ────────────────────────────────────────────────────────────── */
document.addEventListener("click", (e) => {
  if (e.target.classList.contains("chip")) ask(e.target.textContent);
});

el("send").onclick = () => ask(el("q").value);

el("q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(el("q").value); }
});
el("q").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 180) + "px";
});

el("lang-seg").addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") return;
  [...el("lang-seg").children].forEach((b) => b.classList.remove("on"));
  e.target.classList.add("on");
  state.language = e.target.dataset.lang || "";
});

el("new-chat").onclick = () => {
  state.sessionId = null;
  location.reload();
};

el("nav-history").onclick = (e) => {
  e.preventDefault();
  el("history-field").hidden = false;
  loadHistory();
};

el("drawer-close").onclick = () => el("drawer").classList.remove("open");

el("signout").onclick = async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
};

boot();
