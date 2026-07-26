# WP5 — PressMeets RAG: FastAPI backend + chat web UI

**Status:** ✅ done and verified 2026-07-26. **79 tests pass** (34 existing + 45
API cases). Verified against the real Sarvam API end to end, offline with
`KNG_FAKE_LLM=1`, and **in a real browser** — 31 scripted UI checks with a
screenshot per feature (see "Browser verification").

Revised the same day (WP5b) after the user asked for a proper History page, a
fuller Admin console including account deletion, and a pass over authentication:
**changing a password now revokes that account's live sessions**, the
disabled-account login path no longer answers faster than a wrong password, and
the login throttle counts per account as well as per IP.

WP4 could only be reached from a terminal. WP5 makes it an application: sign in,
ask in English or Telugu, watch the answer stream in, click a citation and read
the exact passage it came from.

---

## What was built

| file | role |
|---|---|
| `kng/api/main.py` | the FastAPI app — login, `/api/meta`, SSE `/api/ask`, source viewer, history, admin |
| `kng/api/auth.py` | users (`scrypt`), HMAC-signed session cookies, login throttle — stdlib only |
| `kng/api/users.py` | `python -m kng.api.users add|list|disable|enable|password` |
| `kng/api/meta.py` | corpus facts the filter sidebar is built from, cached at startup |
| `kng/api/sources.py` | citation → extracted passage, plus the path-containment guard |
| `kng/api/history.py` | per-user conversations and the query log behind admin stats |
| `kng/api/static/` | `index.html`, `login.html`, `history.html`, `admin.html`, `app.js`, `history.js`, `admin.js`, `styles.css` |
| `kng/providers/sarvam.py` | `chat_completion_stream` — SSE parsing for `stream: true` |
| `kng/providers/llm.py` | `SarvamLLM.complete_stream`, `FakeLLM.complete_stream` |
| `kng/generation/synthesize.py` | `stream_answer`, per-request `language` override, `chunk_id`/`page` on sources |
| `tests/test_api.py` | 23 cases: auth, admin gating, SSE contract, filters, path traversal |

## How to run it

```bash
pip install -e '.[api]'
python -m kng.api.users add --email you@example.com --admin     # prompts for the password
KNG_SESSION_SECRET=$(openssl rand -hex 32) \
  uvicorn kng.api.main:app --host 127.0.0.1 --port 8000
# free UI work, no spend, no key:
KNG_FAKE_LLM=1 KNG_SESSION_SECRET=… uvicorn kng.api.main:app --port 8000
```

`KNG_LLM_TRACE=1` logs one line per model call (limiter wait, stream duration,
chars). App state lives under `KNG_VAR_DIR` (default `var/`, git-ignored).

## History (`/history`)

The sidebar list was never a history feature — it was a shortcut. `/history` is a
page in the same shell:

- **Cards, not links.** Each conversation shows its title, the last question, a
  preview of the answer and badges for turns, `cited of sources`, latency, and —
  the ones that matter — uncited sentences and stripped citations. Judging a past
  answer's grounding should not require reopening it.
- **Search reads inside conversations.** `q` matches titles *and* every question
  and answer, because matching titles alone misses the follow-ups, which is where
  most of a conversation is.
- **Grouped by day** (Today / Yesterday / Earlier this week / Earlier this month /
  `YYYY-MM`), newest first.
- **Actions:** Continue in Chat (deep-links `/?session=<id>`, which `app.js` loads),
  Rename (`PATCH /api/history/{id}`), Export JSON, Delete, plus a Delete-all in a
  clearly-marked danger zone. Every destructive one confirms first.
- **Transcript drawer** renders the stored turns — the verified `final` text with
  its citation counts. It never re-asks, so reading history costs nothing.
- **Per-account isolation is tested**: another user gets 404 on `GET`, `PATCH` and
  a `false` from `DELETE` for a session id they guessed.

## Admin (`/admin`)

Beyond the earlier stats and add/disable:

- **Role change** (`POST /api/admin/users/role`) — promote or demote.
- **Password reset** (`POST /api/admin/users/password`) — and it *signs that
  account out everywhere*, which is the only version of the feature worth having.
- **Delete account** (`POST /api/admin/users/delete`) — removes the record **and
  purges that user's conversation history**. An account's questions should not
  outlive the account.
- **Three guards, enforced server-side and mirrored in the UI:** you cannot delete
  or disable yourself, you cannot remove or demote the last enabled admin, and a
  delete must echo the address back in a `confirm` field. Buttons that would be
  refused render disabled with the reason in their `title`, so a click cannot
  produce a pointless error.

`python -m kng.api.users` gained `role` and `delete --yes` for the same operations
from a shell, including the same last-admin guard.

## Authentication fixes

| defect | why it mattered | fix |
|---|---|---|
| a password change left every existing session valid | an admin resetting a compromised account believed access was revoked; the stolen cookie kept working to expiry | records carry `cred_version`, tokens pin it as `cv`, `user_from_token` compares the two; `set_password` increments it |
| a disabled account was rejected without hashing | ~80 ms faster than a wrong password — the message said "invalid email or password" while the clock said "this account exists and is switched off" | `authenticate` always runs one scrypt verification, then decides |
| the login throttle counted only the IP, and its table never shrank | behind a proxy every user shares one bucket, and one IP spraying one password across many accounts never tripped it | attempts are counted per IP **and** per account; expired buckets are pruned |
| `delete_cookie` did not match the attributes the cookie was set with | "Sign out" could leave the cookie in place | same `httponly`/`samesite`/`secure` on deletion |
| signed-out `/` returned a JSON body with a `Location` header | worked by accident | `RedirectResponse`, and `/login` bounces an already-signed-in visitor to `/` |

A token minted before versioning existed (no `cv`) is treated as version 1, so
this did not sign anybody out on deploy. Both behaviours are pinned by tests.

## Design decisions

**The `final` SSE event is authoritative; the deltas are not.** Citations can only
be verified once the model stops, so `stream_answer` emits provisional text and
then a `final` event carrying `verify_citations` output. `app.js` replaces what it
streamed with `final.text`. Leaving the raw stream on screen would display a
hallucinated `[9]` as though the server had checked it — the one failure this
project cannot afford.

**Sources stream before the answer.** Retrieval is ~0.2 s and synthesis 10–30 s,
so evidence lands almost immediately and the reader is looking at real citable
sources while the model writes.

**Citations open the passage that was cited.** `build_sources` now carries
`chunk_id` and `page`. Without them the viewer fell back to a file's first chunk,
so a citation reading "p.7" opened p.1 — found during verification, and exactly
the sort of quiet wrongness that makes citations worthless.

**The viewer reads extracted text, not the PDF.** `/data/` is git-ignored and
absent on a fresh clone, so citations must resolve without it. `index/chunks/` is
committed and is the same text retrieval ranked. `/api/raw` serves the original
only when the machine happens to have it.

**All source text is inserted with `textContent`.** The corpus is OCR'd
third-party material; rendering it as HTML would let a scanned page inject script.

**Filters offer only what the corpus can satisfy** — press meets, source types and
the real date range come from `/api/meta`. Publication is a secondary filter, not
a headline one like Sakshi's "Region", because only 275 of 4267 chunks carry one.

**No answer-model toggle.** Sarvam only, as agreed. `AnthropicLLM` already exists,
so adding Claude is a key plus one dropdown entry.

## Security posture — read before deploying

This is **application-level auth for a small trusted deployment**, and the
handover should not pretend otherwise:

- Passwords are `scrypt` (n=2¹⁴) with per-user salts, never stored or logged in
  plaintext (asserted by a test). Both password and token checks use
  `hmac.compare_digest`.
- Sessions are HMAC-SHA256-signed tokens in an httpOnly, SameSite=Lax cookie,
  `Secure` over HTTPS. `user_from_token` re-reads the user record, so disabling,
  **deleting or resetting the password on** an account revokes a live cookie
  immediately (all three tested). There is no server-side session table, so
  revocation works through the record's `cred_version`, not by forgetting a
  session id.
- `KNG_SESSION_SECRET` has **no default** — the app refuses to start without it,
  because a predictable signing key lets anyone forge an admin session.
- Login failures are indistinguishable (wrong password, unknown address, disabled
  account all return one message), with a per-IP throttle of 10 attempts / 5 min.
- `/api/raw` and `/api/source` resolve every caller-supplied path and verify
  containment under the data root; traversal attempts return 404, which is the
  same answer as "not found" so probing learns nothing.
- **Not provided:** TLS termination, password reset, MFA, CSRF tokens (the API is
  JSON-only and SameSite=Lax, but a hardened deployment would add them), or audit
  logging beyond the query log. Bind loopback and put it behind an HTTPS reverse
  proxy before exposing it to a network.
- `var/` is git-ignored: it holds password hashes and real user questions.

## Verification performed

| check | result |
|---|---|
| full test suite | **79 pass** (`python -m unittest discover -s tests`) |
| password change → old cookie | 401; the new password signs in and works |
| deleted account with a live cookie | 401 |
| pre-versioning token (no `cv`) | still accepted |
| disabled-account login timing | within the same order as a wrong password (both hash) |
| login throttle on one account | 429 inside the window |
| normal user → role / password / delete routes | 403 |
| last enabled admin demote · disable · delete | 400 on all three |
| delete with a mistyped `confirm` | 400, account untouched |
| delete an account with history | 200, record gone, conversations gone, directory removed |
| history isolation (another user, guessed id) | 404 / 404 / `deleted: false` |
| history search inside answers, rename bounds, delete one, delete all | pass |
| pages `/`, `/history`, `/admin`, static `history.js` / `admin.js` | 200 |
| signed-out `/` · signed-in `/login` | 303 → `/login` · 303 → `/` |
| unauthenticated `/api/meta`, `/api/ask`, `/api/history` | 401 |
| normal user → `/api/admin/*` | 403 |
| tampered session signature | 401 |
| disabled user with a valid cookie | 401 |
| plaintext password in `users.json` | absent |
| `/api/meta` over HTTP | 33 press meets · 4267 chunks · 2024-06-04→2026-07-21 · 8120 graph nodes |
| SSE ordering (fixture) | `meta` → `sources` → 25 × `delta` → `final` |
| **SSE with the real Sarvam API** | 352 deltas in **17 s**, `sarvam-105b`, 12,994-char prompt → 1442-char answer, **7 sources cited, 0 invalid citations, 0 uncited sentences** |
| citation targeting | cited "p.7" opens p.7 (position 7 of 20), not p.1 |
| `/api/raw` traversal (`../../etc/passwd`, `/etc/passwd`, `README.md`, `../.env`) | 404 on all |
| raw download of a real PDF | 200, 4.36 MB |
| history + admin stats | 4 sessions recorded; stats show volume, latency, citation coverage |
| pages `/`, `/login`, `/admin`, static assets | 200 |

## Browser verification

The API tests cannot see a page that renders wrong, so 31 checks run against a
live server in real Chrome (`playwright`, `executable_path=/usr/bin/google-chrome`,
no browser download), screenshotting each feature: login and its single failure
message, the chat empty state, a streamed answer with citation pills, the citation
drawer on the exact passage, History (grouped, badged, searched, empty-search
state), the transcript drawer, rename, Continue-in-Chat deep link, delete,
the Admin console, role change, disable, password reset **plus the victim's
browser being bounced to `/login`**, the disabled-guard buttons, deletion, and a
mistyped confirmation. Run it against a throwaway `KNG_VAR_DIR` with
`KNG_FAKE_LLM=1`, never the real `var/`.

Two defects were found by *looking* at the screenshots, which no assertion in the
suite would have caught:

- **`[hidden]` lost to a class.** "No conversations yet." sat under a full list of
  three conversations: `.empty-note { display: flex }` outranks the UA rule behind
  the `hidden` attribute, so `el.hidden = true` did nothing. Fixed globally with
  `[hidden] { display: none !important }`, and asserted.
- **Long OCR runs pushed out of the source drawer.** `overflow-wrap: anywhere` on
  the drawer body; the transcript's width is now asserted against the drawer's.

## Known gaps / what WP6 should pick up

- **No reranker** (`RERANK_PROVIDER=none`) — a cross-encoder over the fused top-30
  is the next retrieval-quality step; the seam exists.
- **Cross-script fact relevance.** An English question scores zero against Telugu
  evidence quotes in `graph_context._relevance`; `text_en` is unpopulated.
- **`ANSWER_REASONING_EFFORT` defaults to `null`** and has not been A/B'd for
  *synthesis* quality (WP3 measured it only for extraction). The real answer above
  was produced with it off and cited cleanly, but two or three comparisons would
  settle it.
- **No CSRF tokens and no TLS** — see the security section. Admin-initiated
  password reset exists; self-service reset (which needs email) does not.
- **History search is a linear scan** of the user's session files — fine at tens
  of conversations, wrong at thousands. There is still no pagination: the page
  requests up to 200 (`limit`, capped at 500).
- **Admin stats read the whole query log** (last 2000 lines). Fine at this scale,
  wrong if it ever grows large.
- **No eval harness.** `uncited_sentences` and `invalid_citations` are already
  recorded per answer in `var/queries.jsonl` — that is the raw material for
  scoring answer quality over a fixed question set.
