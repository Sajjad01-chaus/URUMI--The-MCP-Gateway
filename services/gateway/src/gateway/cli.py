"""Operator commands, run inside the gateway pod:

  python -m gateway.cli create-user EMAIL --role member|admin [--password PW]
  python -m gateway.cli create-api-key EMAIL [--name NAME]
"""

import argparse
import asyncio

from sqlalchemy import select

from .config import get_settings
from .db import Database
from .models import ApiKey, User
from .security import generate_api_key, hash_password


async def create_user(db: Database, email: str, role: str, password: str | None) -> None:
    async with db.session() as session:
        if await session.scalar(select(User).where(User.email == email.lower())):
            raise SystemExit(f"user {email} already exists")
        session.add(User(email=email.lower(), role=role, password_hash=hash_password(password) if password else None))
        await session.commit()
    print(f"created {role} {email}")


async def create_api_key(db: Database, email: str, name: str) -> None:
    async with db.session() as session:
        user = await session.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            raise SystemExit(f"no user {email}")
        key, prefix, key_hash = generate_api_key()
        session.add(ApiKey(user_id=user.id, name=name, prefix=prefix, key_hash=key_hash))
        await session.commit()
    print(key)


def main() -> None:
    parser = argparse.ArgumentParser(prog="gateway.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    user = sub.add_parser("create-user")
    user.add_argument("email")
    user.add_argument("--role", choices=["member", "admin"], default="member")
    user.add_argument("--password")
    key = sub.add_parser("create-api-key")
    key.add_argument("email")
    key.add_argument("--name", default="cli")
    args = parser.parse_args()

    async def run() -> None:
        db = Database(get_settings().database_url)
        try:
            if args.command == "create-user":
                await create_user(db, args.email, args.role, args.password)
            else:
                await create_api_key(db, args.email, args.name)
        finally:
            await db.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
