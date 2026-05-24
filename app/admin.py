from __future__ import annotations

import argparse

from .database import get_db, init_db


def set_role(username: str, role: str) -> None:
    if role not in {"admin", "user"}:
        raise SystemExit("role must be admin or user")

    init_db()
    with get_db() as db:
        cursor = db.execute(
            "UPDATE users SET role = ? WHERE username = ?",
            (role, username),
        )
        if cursor.rowcount == 0:
            raise SystemExit(f"user not found: {username}")
    print(f"{username} -> {role}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local admin tools")
    sub = parser.add_subparsers(dest="command", required=True)

    set_admin = sub.add_parser("set-admin", help="promote a user to admin")
    set_admin.add_argument("username")

    set_user = sub.add_parser("set-user", help="demote a user to normal user")
    set_user.add_argument("username")

    args = parser.parse_args()
    if args.command == "set-admin":
        set_role(args.username, "admin")
    elif args.command == "set-user":
        set_role(args.username, "user")


if __name__ == "__main__":
    main()
