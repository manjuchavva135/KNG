"""Per-user conversation history and the query log.

Two separate things, deliberately:

* **history** — what a user asked and the answer they got, so the sidebar can
  reopen a conversation. Owned by that user, stored per user id.
* **query log** — one JSONL line per answered question with the citation-quality
  numbers WP4 already computes (`cited`, `uncited_sentences`,
  `invalid_citations`). It powers the admin stats page, and it is the raw
  material WP6's eval harness needs; collecting it now costs nothing and
  regenerating it later is impossible.

Both live under `KNG_VAR_DIR` (git-ignored). They contain real user questions, so
they stay local: nothing here is committed or sent anywhere.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from .auth import var_dir


def _history_root() -> Path:
    return var_dir() / "history"


def _user_dir(user_id: str) -> Path:
    # `user_id` is server-generated hex, never caller input, so it cannot walk
    # out of the directory — but keep it strict anyway.
    safe = "".join(ch for ch in user_id if ch.isalnum())
    if not safe:
        raise ValueError("invalid user id")
    return _history_root() / safe


def _session_path(user_id: str, session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("invalid session id")
    return _user_dir(user_id) / f"{safe}.json"


def new_session_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def append_turn(user_id: str, session_id: str, turn: dict[str, Any]) -> str:
    """Add one question/answer turn, creating the session if needed."""
    path = _session_path(user_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            session = {}
    else:
        session = {}
    session.setdefault("session_id", session_id)
    session.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    # The first question becomes the sidebar label; later ones must not rename a
    # conversation the user already recognises.
    session.setdefault("title", " ".join((turn.get("question") or "").split())[:70])
    session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    session.setdefault("turns", []).append(turn)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return session_id


def _summary(session: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    """The card the History page renders — enough to judge a conversation without
    opening it, including the citation-quality numbers WP4 already computed."""
    turns = session.get("turns", []) or []
    last = turns[-1] if turns else {}
    latencies = [t["latency_s"] for t in turns
                 if isinstance(t.get("latency_s"), (int, float))]
    return {
        "session_id": session.get("session_id", fallback_id),
        "title": session.get("title", ""),
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        "turns": len(turns),
        "last_question": last.get("question", ""),
        "preview": " ".join((last.get("answer") or "").split())[:180],
        "cited": len(last.get("cited") or []),
        "sources": len(last.get("sources") or []),
        "uncited_sentences": sum(t.get("uncited_sentences") or 0 for t in turns),
        "stripped_citations": sum(len(t.get("invalid_citations") or []) for t in turns),
        "latency_s": round(sum(latencies) / len(latencies), 2) if latencies else None,
    }


def _matches(session: dict[str, Any], needle: str) -> bool:
    """Search titles *and* the questions and answers inside a conversation.

    Matching only the title would miss the follow-up questions, which is where
    most of a conversation actually is.
    """
    if session.get("title", "").lower().find(needle) >= 0:
        return True
    for turn in session.get("turns", []) or []:
        for field in ("question", "answer"):
            if (turn.get(field) or "").lower().find(needle) >= 0:
                return True
    return False


def list_sessions(user_id: str, limit: int = 200,
                  q: Optional[str] = None) -> list[dict[str, Any]]:
    directory = _user_dir(user_id)
    if not directory.exists():
        return []
    needle = (q or "").strip().lower()
    out = []
    for fp in directory.glob("*.json"):
        try:
            session = json.loads(fp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if needle and not _matches(session, needle):
            continue
        out.append(_summary(session, fp.stem))
    out.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return out[:limit]


def rename_session(user_id: str, session_id: str, title: str) -> Optional[str]:
    """Set a conversation's label. Returns the stored title, or None if missing."""
    path = _session_path(user_id, session_id)
    if not path.exists():
        return None
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    clean = " ".join((title or "").split())[:120]
    if not clean:
        return None
    session["title"] = clean
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return clean


def get_session(user_id: str, session_id: str) -> Optional[dict[str, Any]]:
    path = _session_path(user_id, session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def delete_session(user_id: str, session_id: str) -> bool:
    path = _session_path(user_id, session_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def delete_all(user_id: str) -> int:
    """Every conversation this user has. Returns how many were removed."""
    directory = _user_dir(user_id)
    if not directory.exists():
        return 0
    removed = 0
    for fp in list(directory.glob("*.json")) + list(directory.glob("*.tmp")):
        try:
            fp.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def purge_user(user_id: str) -> int:
    """Delete a user's history directory when their account is deleted.

    An account can be removed from `users.json` in a second; the questions they
    asked would otherwise sit on disk indefinitely with no account left that can
    reach or manage them.
    """
    removed = delete_all(user_id)
    directory = _user_dir(user_id)
    try:
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
    except OSError:
        pass
    return removed


# ── query log ──────────────────────────────────────────────────────────────────
def log_query(entry: dict[str, Any]) -> None:
    """Append one line to the query log. Never raises into a request."""
    try:
        fp = var_dir() / "queries.jsonl"
        fp.parent.mkdir(parents=True, exist_ok=True)
        with fp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


def query_stats(limit: int = 2000) -> dict[str, Any]:
    """Rollup for the admin page: volume, latency, and citation coverage."""
    fp = var_dir() / "queries.jsonl"
    if not fp.exists():
        return {"queries": 0, "entries": []}
    rows: list[dict[str, Any]] = []
    for line in fp.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    if not rows:
        return {"queries": 0, "entries": []}

    latencies = [r["latency_s"] for r in rows if isinstance(r.get("latency_s"), (int, float))]
    uncited = [r["uncited_sentences"] for r in rows
               if isinstance(r.get("uncited_sentences"), int)]
    stripped = sum(1 for r in rows if r.get("invalid_citations"))
    by_user: dict[str, int] = {}
    for r in rows:
        by_user[r.get("user", "?")] = by_user.get(r.get("user", "?"), 0) + 1
    return {
        "queries": len(rows),
        "mean_latency_s": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "mean_uncited_sentences": round(sum(uncited) / len(uncited), 2) if uncited else None,
        "answers_with_stripped_citations": stripped,
        "by_user": by_user,
        "entries": [{k: r.get(k) for k in
                     ("ts", "user", "question", "latency_s", "sources", "cited",
                      "uncited_sentences", "invalid_citations")}
                    for r in rows[-25:]][::-1],
    }
