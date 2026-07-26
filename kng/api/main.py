"""PressMeets RAG — the FastAPI application (WP5).

    KNG_SESSION_SECRET=$(openssl rand -hex 32) \\
      uvicorn kng.api.main:app --host 127.0.0.1 --port 8000

Every route sits behind a session cookie except `/api/login`, the login page and
the static assets. Asking a question streams: retrieval evidence lands in about a
fifth of a second, the answer follows token by token, and a final event carries
the citation-verified text.

The corpus is politically sensitive, so two rules hold everywhere in here: an
answer is only ever what the retrieved sources support, and a citation the reader
cannot open is not shipped — `/api/source` resolves each one back to its passage.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import settings
from ..retrieval import hybrid
from . import auth, history, meta, sources

STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent / "static"

app = FastAPI(title="PressMeets RAG", version="0.1.0",
              docs_url="/api/docs", redoc_url=None)


# ── request models ─────────────────────────────────────────────────────────────
class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class AskBody(BaseModel):
    question: str = Field(
        min_length=1, max_length=settings().answer_max_question_chars)
    k: int = Field(default=12, ge=1, le=30)
    language: Optional[str] = Field(default=None, max_length=16)
    press_meet_id: Optional[str] = Field(default=None, max_length=160)
    source_type: Optional[str] = Field(default=None, max_length=48)
    publication: Optional[str] = Field(default=None, max_length=120)
    since: Optional[str] = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    until: Optional[str] = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    passage_language: Optional[str] = Field(default=None, max_length=16)
    use_graph: bool = True
    graph_hops: int = Field(default=1, ge=1, le=3)
    session_id: Optional[str] = Field(default=None, max_length=96,
                                      pattern=r"^[A-Za-z0-9_-]+$")

    def filters(self) -> hybrid.Filters:
        return hybrid.Filters(
            language=self.passage_language, source_type=self.source_type,
            press_meet_id=self.press_meet_id, publication=self.publication,
            since=self.since, until=self.until)


class UserBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    admin: bool = False


class DisableBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    disabled: bool = True


class RoleBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(pattern="^(user|admin)$")


class PasswordBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)


class DeleteUserBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    # The client must echo the address it means to delete. A mis-click on a row
    # button should not be able to remove an account and its history.
    confirm: str = Field(min_length=3, max_length=254)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


# ── auth dependencies ──────────────────────────────────────────────────────────
def current_user(request: Request) -> auth.User:
    user = auth.user_from_token(request.cookies.get(auth.COOKIE_NAME, ""))
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return user


def admin_user(user: auth.User = Depends(current_user)) -> auth.User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return user


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    ip = request.client.host if request.client else "?"
    if auth.throttled(ip, body.email):
        raise HTTPException(status_code=429,
                            detail="too many attempts — wait a few minutes")
    user = auth.authenticate(body.email, body.password)
    if user is None:
        auth.record_attempt(ip, body.email)
        # One message for every failure: a wrong password, an unknown address and
        # a disabled account must be indistinguishable from outside.
        raise HTTPException(status_code=401, detail="invalid email or password")
    auth.clear_attempts(ip, body.email)
    response.set_cookie(
        auth.COOKIE_NAME, auth.issue_token(user),
        httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
        max_age=settings().session_hours * 3600, path="/")
    return {"user": user.public()}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    # The attributes must match the ones the cookie was set with, or the browser
    # keeps the original and "Sign out" only appears to work.
    response.delete_cookie(auth.COOKIE_NAME, path="/", httponly=True,
                           samesite="lax", secure=request.url.scheme == "https")
    return {"ok": True}


@app.get("/api/me")
def me(user: auth.User = Depends(current_user)) -> dict[str, Any]:
    return {"user": user.public()}


# ── corpus metadata ────────────────────────────────────────────────────────────
@app.get("/api/meta")
def get_meta(user: auth.User = Depends(current_user)) -> dict[str, Any]:
    return meta.corpus_meta()


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Unauthenticated, for a proxy's health check. Leaks nothing about content."""
    return {"ok": True, "app": "PressMeets RAG"}


@app.get("/api/ready")
def ready(response: Response) -> dict[str, Any]:
    """Deployment readiness without loading the embedding model or leaking paths."""
    s = settings()
    checks = {
        "session_signing": bool(s.session_secret),
        "vector_index": s.path(s.lancedb_path).is_dir(),
        "graph_index": (s.path(s.graph_path) / "graph.json").is_file(),
        "chunk_provenance": (s.index_dir / "chunks").is_dir(),
    }
    available = all(checks.values())
    if not available:
        response.status_code = 503
    return {"ready": available, "checks": checks}


# ── ask (streaming) ────────────────────────────────────────────────────────────
_ask_lock = threading.Lock()
_ask_attempts: dict[str, deque[float]] = defaultdict(deque)
_ask_slots = threading.BoundedSemaphore(max(1, settings().ask_max_concurrent))


def _take_ask_quota(user_id: str) -> bool:
    """Process-local paid-call throttle, keyed by authenticated account."""
    now = time.monotonic()
    window = max(1, settings().ask_window_seconds)
    limit = max(1, settings().ask_max_requests)
    with _ask_lock:
        q = _ask_attempts[user_id]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/ask")
def ask(body: AskBody, user: auth.User = Depends(current_user)) -> StreamingResponse:
    """Server-sent events: `sources` → validated `delta`* → `final`.

    Provider output is buffered until citation and claim-support validation
    passes, so unsupported prose is never sent as a provisional delta. `final`
    remains authoritative and carries the grounding/refusal record.
    """
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="a question is required")
    if not _take_ask_quota(user.id):
        raise HTTPException(
            status_code=429,
            detail="question limit reached — wait before making another paid request")
    if not _ask_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="the answer service is at capacity — retry shortly")

    session_id = body.session_id or history.new_session_id()

    def events():
        from ..generation.synthesize import stream_answer
        started = time.monotonic()
        answer = None
        yield _sse("meta", {"session_id": session_id})
        try:
            try:
                for kind, payload in stream_answer(
                        question, k=body.k, filters=body.filters(),
                        use_graph=body.use_graph, graph_hops=body.graph_hops,
                        language=body.language):
                    if kind == "sources":
                        yield _sse("sources", payload)
                    elif kind == "delta":
                        yield _sse("delta", {"text": payload})
                    elif kind == "error":
                        yield _sse("error", {"message": payload})
                    elif kind == "final":
                        answer = payload
                        yield _sse("final", {
                            "text": payload.text,
                            "cited": payload.cited,
                            "invalid_citations": payload.invalid_citations,
                            "uncited_sentences": payload.uncited_sentences,
                            "grounding_passed": payload.grounding_passed,
                            "refused": payload.refused,
                            "refusal_reason": payload.refusal_reason,
                            "sources": payload.sources,
                            "diagnostics": payload.diagnostics,
                            "session_id": session_id,
                        })
            except Exception as e:                # never leave the client hanging
                yield _sse("error", {"message": f"{type(e).__name__}: {e}"})
                return

            if answer is None:
                return
            latency = round(time.monotonic() - started, 2)
            turn = {
                "question": question, "answer": answer.text,
                "cited": answer.cited, "sources": answer.sources,
                "uncited_sentences": answer.uncited_sentences,
                "invalid_citations": answer.invalid_citations,
                "grounding_passed": answer.grounding_passed,
                "refused": answer.refused,
                "refusal_reason": answer.refusal_reason,
                "language": body.language, "latency_s": latency,
                "filters": {k: v for k, v in body.model_dump().items()
                            if k in ("press_meet_id", "source_type", "publication",
                                     "since", "until", "passage_language") and v},
                "asked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            try:
                history.append_turn(user.id, session_id, turn)
            except (OSError, ValueError):
                pass                          # a failed write must not break the answer
            history.log_query({
                "user": user.email, "question": question, "latency_s": latency,
                "sources": len(answer.sources), "cited": len(answer.cited),
                "uncited_sentences": answer.uncited_sentences,
                "invalid_citations": answer.invalid_citations,
                "grounding_passed": answer.grounding_passed,
                "refused": answer.refused,
                "model": (answer.diagnostics.get("llm") or {}).get("model"),
            })
        finally:
            _ask_slots.release()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── source viewer ──────────────────────────────────────────────────────────────
@app.get("/api/source")
def get_source(file: str, chunk_id: Optional[str] = None, page: Optional[int] = None,
               user: auth.User = Depends(current_user)) -> dict[str, Any]:
    try:
        return sources.passage(file, chunk_id=chunk_id, page=page)
    except sources.SourceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/raw")
def get_raw(file: str, user: auth.User = Depends(current_user)) -> FileResponse:
    """The original document, when this machine has `/data/`.

    A fresh clone has `index/` and `extracted/` but not the 584 MB source
    archive, so this is a convenience: the citation itself always resolves
    through `/api/source`.
    """
    try:
        path = sources.raw_file(file)
    except sources.SourceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return FileResponse(path, filename=path.name)


# ── history ────────────────────────────────────────────────────────────────────
@app.get("/api/history")
def get_history(q: Optional[str] = None, limit: int = 200,
                user: auth.User = Depends(current_user)) -> dict[str, Any]:
    """This user's conversations, newest first. `q` searches titles *and* turns.

    Scoped to `user.id` throughout — history is per account, and one user must
    never be able to read another's questions by guessing a session id.
    """
    sessions = history.list_sessions(user.id, limit=max(1, min(limit, 500)), q=q)
    return {"sessions": sessions, "query": q or ""}


@app.get("/api/history/{session_id}")
def get_history_session(session_id: str,
                        user: auth.User = Depends(current_user)) -> dict[str, Any]:
    session = history.get_session(user.id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    return session


@app.patch("/api/history/{session_id}")
def rename_history_session(session_id: str, body: RenameBody,
                           user: auth.User = Depends(current_user)) -> dict[str, Any]:
    title = history.rename_session(user.id, session_id, body.title)
    if title is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    return {"session_id": session_id, "title": title}


@app.delete("/api/history/{session_id}")
def delete_history_session(session_id: str,
                           user: auth.User = Depends(current_user)) -> dict[str, bool]:
    return {"deleted": history.delete_session(user.id, session_id)}


@app.delete("/api/history")
def clear_history(user: auth.User = Depends(current_user)) -> dict[str, int]:
    return {"deleted": history.delete_all(user.id)}


# ── admin ──────────────────────────────────────────────────────────────────────
@app.get("/api/admin/users")
def admin_list_users(user: auth.User = Depends(admin_user)) -> dict[str, Any]:
    return {"users": [u.public() for u in auth.list_users()]}


@app.post("/api/admin/users")
def admin_add_user(body: UserBody,
                   user: auth.User = Depends(admin_user)) -> dict[str, Any]:
    try:
        created = auth.add_user(body.email, body.password,
                                role="admin" if body.admin else "user")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"user": created.public()}


@app.post("/api/admin/users/disable")
def admin_disable_user(body: DisableBody,
                       user: auth.User = Depends(admin_user)) -> dict[str, Any]:
    email = body.email.strip().lower()
    if email == user.email and body.disabled:
        # Locking the last admin out of their own instance needs a shell to undo.
        raise HTTPException(status_code=400, detail="you cannot disable yourself")
    if body.disabled and _would_orphan(email):
        raise HTTPException(status_code=400,
                            detail="that is the last enabled admin — promote "
                                   "someone else first")
    try:
        changed = auth.set_disabled(body.email, body.disabled)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"user": changed.public()}


def _would_orphan(email: str) -> bool:
    """True if removing this account's admin rights leaves no way back in.

    Every destructive admin action funnels through here. An instance with no
    enabled admin can only be repaired from a shell on the host, which for a
    deployment someone else is running means a support call.
    """
    target = auth.get_user(email)
    if target is None or not target.is_admin or target.disabled:
        return False
    return auth.admin_count(exclude_email=email) == 0


@app.post("/api/admin/users/role")
def admin_set_role(body: RoleBody,
                   user: auth.User = Depends(admin_user)) -> dict[str, Any]:
    email = body.email.strip().lower()
    if body.role == "user" and _would_orphan(email):
        raise HTTPException(status_code=400,
                            detail="that is the last enabled admin — promote "
                                   "someone else first")
    try:
        changed = auth.set_role(body.email, body.role)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"user": changed.public()}


@app.post("/api/admin/users/password")
def admin_set_password(body: PasswordBody,
                       user: auth.User = Depends(admin_user)) -> dict[str, Any]:
    """Reset a password. Every session that account had stops working.

    `auth.set_password` bumps the record's credential version, which every issued
    token pins — so a cookie stolen before the reset is dead, which is the whole
    reason an admin resets a password in the first place.
    """
    try:
        changed = auth.set_password(body.email, body.password)
    except ValueError as e:
        code = 404 if str(e).startswith("no such user") else 400
        raise HTTPException(status_code=code, detail=str(e))
    return {"user": changed.public(), "sessions_revoked": True}


@app.post("/api/admin/users/delete")
def admin_delete_user(body: DeleteUserBody,
                      user: auth.User = Depends(admin_user)) -> dict[str, Any]:
    """Delete an account and its conversation history. Not reversible.

    Refused in three cases: deleting yourself (an admin locking themselves out),
    deleting the last enabled admin, and a `confirm` field that does not echo the
    address — the UI asks the operator to type it.
    """
    email = body.email.strip().lower()
    if email != body.confirm.strip().lower():
        raise HTTPException(status_code=400,
                            detail="type the address to confirm the deletion")
    if email == user.email:
        raise HTTPException(status_code=400, detail="you cannot delete your own account")
    if _would_orphan(email):
        raise HTTPException(status_code=400,
                            detail="that is the last enabled admin — promote "
                                   "someone else first")
    try:
        removed = auth.delete_user(body.email)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # The account is gone; its questions must go with it.
    conversations = history.purge_user(removed.id)
    return {"deleted": removed.public(), "conversations_deleted": conversations}


@app.get("/api/admin/stats")
def admin_stats(user: auth.User = Depends(admin_user)) -> dict[str, Any]:
    return {"corpus": meta.corpus_meta(), "queries": history.query_stats()}


# ── pages ──────────────────────────────────────────────────────────────────────
def _page(name: str) -> HTMLResponse:
    fp = STATIC_DIR / name
    if not fp.exists():
        return HTMLResponse("<h1>PressMeets RAG</h1><p>static assets missing</p>",
                            status_code=500)
    return HTMLResponse(fp.read_text(encoding="utf-8"))


def _signed_in(request: Request) -> Optional[auth.User]:
    return auth.user_from_token(request.cookies.get(auth.COOKIE_NAME, ""))


@app.get("/login")
def login_page(request: Request) -> Any:
    if _signed_in(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _page("login.html")


@app.get("/admin")
def admin_page() -> HTMLResponse:
    # Gating happens in `/api/admin/*`; this only serves markup.
    return _page("admin.html")


@app.get("/history")
def history_page() -> HTMLResponse:
    return _page("history.html")


@app.get("/")
def index(request: Request) -> Any:
    if _signed_in(request) is None:
        return RedirectResponse("/login", status_code=303)
    return _page("index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    """Fail fast on a missing secret, and warm the metadata cache."""
    auth._secret()                            # raises with instructions if unset
    meta.corpus_meta()
