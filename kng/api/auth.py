"""Users and sessions — stdlib only, no new dependency.

Scope, stated plainly: this is **application-level auth for a small trusted
deployment**. It binds loopback by default, terminates no TLS, and has no
password-reset flow, MFA, or account lockout beyond a per-IP login throttle. Put
it behind a reverse proxy that terminates HTTPS before exposing it to a network.

What it does do carefully:

* passwords are stored as `scrypt` hashes with a per-user random salt, never in
  plaintext and never logged;
* both password and session-token checks use `hmac.compare_digest`, so neither
  leaks its answer through timing;
* sessions are HMAC-SHA256-signed tokens in an httpOnly, SameSite=Lax cookie,
  carrying only an id, role and expiry — no server-side session table to lose;
* the signing secret has **no default**. A guessable default would let anyone
  mint an admin cookie, so the app refuses to start without `KNG_SESSION_SECRET`.

State (`users.json`) lives under `KNG_VAR_DIR` (default `var/`), which is
git-ignored: user records are deployment state, not project artifacts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from ..config import settings

COOKIE_NAME = "kng_session"

# scrypt parameters. n=2**14 keeps a single verification near ~50-100 ms on this
# hardware: slow enough to make offline guessing expensive, fast enough that a
# login feels instant.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

# Login throttle: attempts per IP inside a window. Crude but enough to stop a
# script working through a password list against a known address.
_MAX_ATTEMPTS = 10
_WINDOW_S = 300.0
_attempts: dict[str, deque] = defaultdict(deque)


@dataclass
class User:
    id: str
    email: str
    role: str = "user"                       # "user" | "admin"
    disabled: bool = False
    created_at: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email, "role": self.role,
                "disabled": self.disabled, "created_at": self.created_at}


# ── state location ─────────────────────────────────────────────────────────────
# Both of these read the environment at call time rather than through the frozen
# Settings dataclass. `Settings` evaluates its `_env(...)` defaults when
# `kng.config` is imported, so a test (or a second instance) could not otherwise
# redirect its state or supply a secret after import.
def var_dir() -> Path:
    value = os.environ.get("KNG_VAR_DIR") or settings().var_dir
    p = Path(value)
    return p if p.is_absolute() else settings().path(value)


def users_file() -> Path:
    return var_dir() / "users.json"


def _secret() -> str:
    value = os.environ.get("KNG_SESSION_SECRET") or settings().session_secret
    if not value:
        raise RuntimeError(
            "KNG_SESSION_SECRET is not set. Generate one with "
            "`openssl rand -hex 32` and pass it in the environment; there is no "
            "default because a predictable signing key lets anyone forge an "
            "admin session.")
    return value


# ── password hashing ───────────────────────────────────────────────────────────
def hash_password(password: str) -> tuple[str, str]:
    """(salt_hex, hash_hex) for a new or changed password."""
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return hmac.compare_digest(digest, expected)


# ── user store ─────────────────────────────────────────────────────────────────
def _load() -> dict[str, Any]:
    fp = users_file()
    if not fp.exists():
        return {"users": []}
    try:
        blob = json.loads(fp.read_text(encoding="utf-8"))
    except ValueError:
        raise RuntimeError(f"{fp} is not valid JSON — refusing to overwrite it")
    return blob if isinstance(blob, dict) and "users" in blob else {"users": []}


def _save(data: dict[str, Any]) -> None:
    fp = users_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)                     # password hashes: owner-only
    tmp.replace(fp)


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def add_user(email: str, password: str, role: str = "user") -> User:
    email = _norm_email(email)
    if not email or "@" not in email:
        raise ValueError("a valid email address is required")
    if role not in ("user", "admin"):
        raise ValueError("role must be 'user' or 'admin'")
    data = _load()
    if any(_norm_email(u["email"]) == email for u in data["users"]):
        raise ValueError(f"{email} already exists")
    salt, digest = hash_password(password)
    record = {
        "id": secrets.token_hex(8), "email": email, "role": role,
        "disabled": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "salt": salt, "hash": digest,
    }
    data["users"].append(record)
    _save(data)
    return User(id=record["id"], email=email, role=role,
                created_at=record["created_at"])


def list_users() -> list[User]:
    return [User(id=u["id"], email=u["email"], role=u.get("role", "user"),
                 disabled=bool(u.get("disabled")), created_at=u.get("created_at", ""))
            for u in _load()["users"]]


def set_disabled(email: str, disabled: bool) -> User:
    email = _norm_email(email)
    data = _load()
    for u in data["users"]:
        if _norm_email(u["email"]) == email:
            u["disabled"] = disabled
            _save(data)
            return User(id=u["id"], email=u["email"], role=u.get("role", "user"),
                        disabled=disabled, created_at=u.get("created_at", ""))
    raise ValueError(f"no such user: {email}")


def set_password(email: str, password: str) -> User:
    email = _norm_email(email)
    data = _load()
    for u in data["users"]:
        if _norm_email(u["email"]) == email:
            u["salt"], u["hash"] = hash_password(password)
            _save(data)
            return User(id=u["id"], email=u["email"], role=u.get("role", "user"),
                        disabled=bool(u.get("disabled")),
                        created_at=u.get("created_at", ""))
    raise ValueError(f"no such user: {email}")


def authenticate(email: str, password: str) -> Optional[User]:
    """The matching, enabled user, or None. Same answer for every failure mode.

    A wrong password, an unknown address and a disabled account are
    indistinguishable to the caller on purpose — telling them apart tells an
    attacker which addresses are worth attacking. The dummy hash keeps the
    unknown-user path as slow as the real one so timing does not leak it either.
    """
    email = _norm_email(email)
    for u in _load()["users"]:
        if _norm_email(u["email"]) == email:
            if u.get("disabled"):
                return None
            if verify_password(password, u.get("salt", ""), u.get("hash", "")):
                return User(id=u["id"], email=u["email"], role=u.get("role", "user"),
                            disabled=False, created_at=u.get("created_at", ""))
            return None
    hashlib.scrypt(b"dummy", salt=b"dummy-salt-16byt", **_SCRYPT)
    return None


# ── login throttle ─────────────────────────────────────────────────────────────
def throttled(ip: str) -> bool:
    now = time.monotonic()
    q = _attempts[ip or "?"]
    while q and now - q[0] > _WINDOW_S:
        q.popleft()
    return len(q) >= _MAX_ATTEMPTS


def record_attempt(ip: str) -> None:
    _attempts[ip or "?"].append(time.monotonic())


def clear_attempts(ip: str) -> None:
    _attempts.pop(ip or "?", None)


# ── session tokens ─────────────────────────────────────────────────────────────
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_token(user: User, hours: Optional[int] = None) -> str:
    ttl = hours if hours is not None else settings().session_hours
    payload = {"sub": user.id, "email": user.email, "role": user.role,
               "exp": int(time.time()) + int(ttl) * 3600}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret().encode("utf-8"), body.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str) -> Optional[dict]:
    """Decoded payload for a validly signed, unexpired token; else None."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".", 1)
    expected = hmac.new(_secret().encode("utf-8"), body.encode("ascii"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def user_from_token(token: str) -> Optional[User]:
    """Re-read the user record, so a disabled or deleted account stops working.

    The token alone would be enough to identify the caller, but trusting it
    blindly means a revoked account keeps access until its cookie expires.
    """
    payload = verify_token(token)
    if payload is None:
        return None
    for u in _load()["users"]:
        if u["id"] == payload.get("sub"):
            if u.get("disabled"):
                return None
            return User(id=u["id"], email=u["email"], role=u.get("role", "user"),
                        disabled=False, created_at=u.get("created_at", ""))
    return None


__all__ = ["User", "COOKIE_NAME", "add_user", "list_users", "set_disabled",
           "set_password", "authenticate", "issue_token", "verify_token",
           "user_from_token", "throttled", "record_attempt", "clear_attempts",
           "var_dir", "users_file", "hash_password", "verify_password", "asdict"]
