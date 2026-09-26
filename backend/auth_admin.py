"""Operator account provisioning. Passwords are prompted, never CLI arguments."""

import argparse
import asyncio
import getpass

from sqlalchemy import delete, select

from .content import auth
from .core import config, db, migrations
from .core.db import User, UserSession
from .data import users


async def _provision(name: str, password: str) -> None:
    await db.init_db()
    await migrations.run()
    name = users.norm(name)
    if not name:
        raise SystemExit("username is required")
    auth.validate_new_password(password)
    if await users.get(name) is None:
        await users.create(name)
    async with db.session() as session:
        user = (await session.execute(select(User).where(User.name == name))).scalar_one()
        user.password_hash = await asyncio.to_thread(auth.hash_password, password)
        user.is_active = True
        await session.execute(delete(UserSession).where(UserSession.user_name == name))
        await session.commit()
    await db.dispose()
    print(f"Account {name!r} is ready; prior sessions were revoked.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a KNDB password account")
    parser.add_argument("username")
    args = parser.parse_args()
    if config.AUTH_MODE != "password":
        raise SystemExit("Set auth.mode: password before provisioning accounts.")
    first = getpass.getpass("Password (14+ characters): ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise SystemExit("Passwords do not match.")
    asyncio.run(_provision(args.username, first))


if __name__ == "__main__":
    main()
