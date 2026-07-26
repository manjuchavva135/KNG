"""User administration from the shell — the bootstrap path for a fresh install.

    python -m kng.api.users add --email you@example.com --admin
    python -m kng.api.users list
    python -m kng.api.users disable --email someone@example.com
    python -m kng.api.users password --email you@example.com
    python -m kng.api.users role --email someone@example.com --role admin
    python -m kng.api.users delete --email someone@example.com --yes

`delete` also removes that account's conversation history, the same as the admin
page does — an account's questions should not outlive the account. It is the one
command here that cannot be undone, so it insists on `--yes`.

Passwords are prompted for with `getpass`, never taken as an argument: a password
on the command line ends up in shell history and in the process list, where
anyone on the box can read it.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import auth, history


def _last_admin(email: str) -> bool:
    """Guard the same case the API guards: no enabled admin left to sign in."""
    target = auth.get_user(email)
    if target is None or not target.is_admin or target.disabled:
        return False
    return auth.admin_count(exclude_email=email) == 0


def _prompt_password() -> str:
    first = getpass.getpass("password: ")
    if len(first) < 8:
        raise SystemExit("password must be at least 8 characters")
    if first != getpass.getpass("repeat: "):
        raise SystemExit("passwords do not match")
    return first


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kng.api.users")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="create a user")
    p.add_argument("--email", required=True)
    p.add_argument("--admin", action="store_true", help="grant the admin role")

    sub.add_parser("list", help="list users")

    p = sub.add_parser("disable", help="block a user from signing in")
    p.add_argument("--email", required=True)

    p = sub.add_parser("enable", help="unblock a user")
    p.add_argument("--email", required=True)

    p = sub.add_parser("password", help="change a password (signs that account out)")
    p.add_argument("--email", required=True)

    p = sub.add_parser("role", help="grant or remove the admin role")
    p.add_argument("--email", required=True)
    p.add_argument("--role", required=True, choices=("user", "admin"))

    p = sub.add_parser("delete", help="delete an account and its history")
    p.add_argument("--email", required=True)
    p.add_argument("--yes", action="store_true", help="confirm; required")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            user = auth.add_user(args.email, _prompt_password(),
                                 role="admin" if args.admin else "user")
            print(f"created {user.email} ({user.role}) → {auth.users_file()}")
        elif args.cmd == "list":
            users = auth.list_users()
            if not users:
                print(f"no users yet in {auth.users_file()} — add one with "
                      f"`python -m kng.api.users add --email …`")
            for u in users:
                flag = " [disabled]" if u.disabled else ""
                print(f"{u.role:<6} {u.email}{flag}  created {u.created_at}")
        elif args.cmd in ("disable", "enable"):
            user = auth.set_disabled(args.email, args.cmd == "disable")
            print(f"{user.email} is now {'disabled' if user.disabled else 'enabled'}")
        elif args.cmd == "password":
            user = auth.set_password(args.email, _prompt_password())
            print(f"password updated for {user.email} — any session it had is "
                  f"now signed out")
        elif args.cmd == "role":
            if args.role == "user" and _last_admin(args.email):
                raise ValueError("that is the last enabled admin — promote "
                                 "someone else first")
            user = auth.set_role(args.email, args.role)
            print(f"{user.email} is now {user.role}")
        elif args.cmd == "delete":
            if not args.yes:
                raise ValueError("refusing to delete without --yes")
            if _last_admin(args.email):
                raise ValueError("that is the last enabled admin — promote "
                                 "someone else first")
            user = auth.delete_user(args.email)
            gone = history.purge_user(user.id)
            print(f"deleted {user.email} and {gone} conversation(s)")
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
