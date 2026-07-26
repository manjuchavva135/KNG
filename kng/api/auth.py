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
  carrying an id, role, expiry and **credential version** — no server-side
  session table to lose, but still revocable (see below);
* the signing secret has **no default**. A guessable default would let anyone
  mint an admin cookie, so the app refuses to start without `KNG_SESSION_SECRET`.

**Changing a password revokes every session that account already has.** Each
record carries a `cred_version` that `set_password` increments and every token
pins; a token whose `cv` no longer matches the record is refused. Without it a
password reset was cosmetic — the attacker's stolen cookie kept working until it
expired, while the admin who reset it believed the account was secured. Deleting
or disabling an account revokes its cookies the same way, because
`user_from_token` re-reads the record on every request.

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

# Login throttle: failed attempts inside a window, counted per *IP* and per
# *account*. Per-IP alone is not enough in either direction — behind a reverse
# proxy every user shares one address, and one address spraying a common password
# across many accounts never trips a per-IP-and-nothing-else counter.
_MAX_ATTEMPTS = 10
_WINDOW_S = 300.0
_MAX_TRACKED = 4096                          # bound the table; it is process-local
_attempts: dict[str, deque] = defaultdict(deque)


@dataclass
class User:
    id: str
    email: str
    role: str = "user"                       # "user" | "admin"
    disabled: bool = False
    created_at: str = ""
    cred_version: int = 1                    # bumped by set_password → revokes tokens

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


def _user(record: dict[str, Any]) -> User:
    """One place that turns a stored record into a `User`.

    It was five copies, and a field added to one of them (`cred_version`) would
    have been silently missing from the others.
    """
    return User(id=record["id"], email=record["email"],
                role=record.get("role", "user"),
                disabled=bool(record.get("disabled")),
                created_at=record.get("created_at", ""),
                cred_version=int(record.get("cred_version", 1)))


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
        "salt": salt, "hash": digest, "cred_version": 1,
    }
    data["users"].append(record)
    _save(data)
    return _user(record)


def list_users() -> list[User]:
    return [_user(u) for u in _load()["users"]]


def get_user(email: str) -> Optional[User]:
    email = _norm_email(email)
    for u in _load()["users"]:
        if _norm_email(u["email"]) == email:
            return _user(u)
    return None


def admin_count(exclude_email: str = "") -> int:
    """Enabled admins, optionally ignoring one address.

    The callers use it to refuse the change that would leave the instance with no
    way in: removing or demoting the last admin needs a shell to undo.
    """
    skip = _norm_email(exclude_email)
    return sum(1 for u in _load()["users"]
               if u.get("role") == "admin" and not u.get("disabled")
               and _norm_email(u["email"]) != skip)


def _mutate(email: str, change) -> User:
    email = _norm_email(email)
    data = _load()
    for u in data["users"]:
        if _norm_email(u["email"]) == email:
            change(u)
            _save(data)
            return _user(u)
    raise ValueError(f"no such user: {email}")


def set_disabled(email: str, disabled: bool) -> User:
    return _mutate(email, lambda u: u.update({"disabled": bool(disabled)}))


def set_role(email: str, role: str) -> User:
    if role not in ("user", "admin"):
        raise ValueError("role must be 'user' or 'admin'")
    return _mutate(email, lambda u: u.update({"role": role}))


def set_password(email: str, password: str) -> User:
    """Change a password **and invalidate every session it had.**

    Bumping `cred_version` is the whole point: otherwise a reset changes what the
    owner types and nothing else, and a cookie taken before the reset keeps full
    access until it expires.
    """
    salt, digest = hash_password(password)          # validates length before write

    def change(u: dict[str, Any]) -> None:
        u["salt"], u["hash"] = salt, digest
        u["cred_version"] = int(u.get("cred_version", 1)) + 1

    return _mutate(email, change)


def delete_user(email: str) -> User:
    """Remove the account. Its live cookies stop working on the next request.

    Conversation history is owned by the account, so the caller is expected to
    delete it too (`main.py` does); leaving a user's questions behind after their
    account is gone would keep personal data nobody can reach or manage.
    """
    email = _norm_email(email)
    data = _load()
    for i, u in enumerate(data["users"]):
        if _norm_email(u["email"]) == email:
            removed = _user(u)
            del data["users"][i]
            _save(data)
            return removed
    raise ValueError(f"no such user: {email}")


def authenticate(email: str, password: str) -> Optional[User]:
    """The matching, enabled user, or None. Same answer for every failure mode.

    A wrong password, an unknown address and a disabled account are
    indistinguishable to the caller on purpose — telling them apart tells an
    attacker which addresses are worth attacking.

    Every path does one scrypt verification, including the two that already know
    they will fail. Returning early for a disabled account skipped ~80 ms of
    hashing, which is plainly visible in the response time: the message said
    "invalid email or password" while the clock said "this account exists and is
    switched off".
    """
    email = _norm_email(email)
    for u in _load()["users"]:
        if _norm_email(u["email"]) == email:
            ok = verify_password(password, u.get("salt", ""), u.get("hash", ""))
            if ok and not u.get("disabled"):
                return _user(u)
            return None
    # Unknown address: burn the same work so timing does not separate it either.
    hashlib.scrypt(b"dummy", salt=b"dummy-salt-16byt", **_SCRYPT)
    return None


# ── login throttle ─────────────────────────────────────────────────────────────
def _keys(ip: str, email: str = "") -> list[str]:
    keys = [f"ip:{ip or '?'}"]
    if email:
        keys.append(f"user:{_norm_email(email)}")
    return keys


def _recent(key: str) -> int:
    now = time.monotonic()
    q = _attempts[key]
    while q and now - q[0] > _WINDOW_S:
        q.popleft()
    if not q:
        _attempts.pop(key, None)              # do not keep an empty deque forever
    return len(q)


def _prune() -> None:
    """Drop expired buckets. Unpruned, one bucket per attacking IP is a slow leak."""
    if len(_attempts) <= _MAX_TRACKED:
        return
    for key in list(_attempts):
        _recent(key)


def throttled(ip: str, email: str = "") -> bool:
    """True when either this address or this account has burnt its attempts."""
    return any(_recent(key) >= _MAX_ATTEMPTS for key in _keys(ip, email))


def record_attempt(ip: str, email: str = "") -> None:
    now = time.monotonic()
    for key in _keys(ip, email):
        _attempts[key].append(now)
    _prune()


def clear_attempts(ip: str, email: str = "") -> None:
    for key in _keys(ip, email):
        _attempts.pop(key, None)


# ── session tokens ─────────────────────────────────────────────────────────────
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_token(user: User, hours: Optional[int] = None) -> str:
    ttl = hours if hours is not None else settings().session_hours
    payload = {"sub": user.id, "email": user.email, "role": user.role,
               "cv": int(user.cred_version),
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
    """Re-read the user record, so a revoked account stops working immediately.

    The signature alone would be enough to identify the caller, but trusting it
    blindly means a disabled, deleted or password-reset account keeps access
    until its cookie expires. Three things are re-checked against the record:
    the account still exists, it is not disabled, and its `cred_version` still
    matches the one the token was minted with.
    """
    payload = verify_token(token)
    if payload is None:
        return None
    for u in _load()["users"]:
        if u["id"] == payload.get("sub"):
            if u.get("disabled"):
                return None
            # Missing `cv` means a token issued before versioning existed; treat
            # it as version 1, which is what those records hold.
            if int(u.get("cred_version", 1)) != int(payload.get("cv", 1)):
                return None
            return _user(u)
    return None


__all__ = ["User", "COOKIE_NAME", "add_user", "list_users", "get_user",
           "admin_count", "set_disabled", "set_role", "set_password",
           "delete_user", "authenticate", "issue_token", "verify_token",
           "user_from_token", "throttled", "record_attempt", "clear_attempts",
           "var_dir", "users_file", "hash_password", "verify_password", "asdict"]
