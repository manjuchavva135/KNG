"""User administration from the shell — the bootstrap path for a fresh install.

    python -m kng.api.users add --email you@example.com --admin
    python -m kng.api.users list
    python -m kng.api.users disable --email someone@example.com
    python -m kng.api.users password --email you@example.com

Passwords are prompted for with `getpass`, never taken as an argument: a password
on the command line ends up in shell history and in the process list, where
anyone on the box can read it.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import auth


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

    p = sub.add_parser("password", help="change a user's password")
    p.add_argument("--email", required=True)

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
            print(f"password updated for {user.email}")
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
