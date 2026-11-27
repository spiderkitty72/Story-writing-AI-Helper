"""Manage user accounts from the command line.

    docker compose exec app python -m app.users add josh --admin
    docker compose exec app python -m app.users passwd josh
    docker compose exec app python -m app.users list
    docker compose exec app python -m app.users enable josh
    docker compose exec app python -m app.users delete someone

Passwords are asked for interactively, or read from standard input with
--password-stdin (one line).
"""
from __future__ import annotations

import argparse
import getpass
import sys

from app import auth
from app import config as config_mod


def _password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return pw


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.users", description="Manage Story Helper accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="create an account")
    a.add_argument("username")
    a.add_argument("--admin", action="store_true", help="can manage users and settings")
    a.add_argument("--password-stdin", action="store_true")
    p = sub.add_parser("passwd", help="set a new password (signs the user out everywhere)")
    p.add_argument("username")
    p.add_argument("--password-stdin", action="store_true")
    sub.add_parser("list", help="list accounts")
    for name, help_ in (("enable", "re-enable a disabled account"), ("disable", "block an account"),
                        ("delete", "delete an account (their books stay on disk)")):
        sub.add_parser(name, help=help_).add_argument("username")
    args = ap.parse_args(argv)

    cfg = config_mod.load_config()
    auth.configure(lambda: cfg.data_dir)
    store = auth.store()

    try:
        if args.cmd == "list":
            for u in store.list_users():
                flags = ", ".join(f for f, on in (("admin", u.is_admin), ("disabled", u.disabled)) if on)
                print(f"{u.id:>4}  {u.username}{'  (' + flags + ')' if flags else ''}")
            return
        if args.cmd == "add":
            u = store.create(args.username, _password(args), is_admin=args.admin)
            print(f"Created {'admin ' if u.is_admin else ''}account '{u.username}'. Books: {store.user_dir(u)}")
            return
        user = store.get_by_name(args.username)
        if user is None:
            sys.exit(f"No user named '{args.username}'")
        if args.cmd == "passwd":
            store.set_password(user.id, _password(args))
            print("Password changed. The user was signed out everywhere.")
        elif args.cmd == "enable":
            store.update(user.id, disabled=False)
            print("Enabled.")
        elif args.cmd == "disable":
            store.update(user.id, disabled=True)
            print("Disabled and signed out.")
        elif args.cmd == "delete":
            folder = store.user_dir(user)
            store.delete(user.id)
            print(f"Deleted. Their books are still in {folder}")
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
