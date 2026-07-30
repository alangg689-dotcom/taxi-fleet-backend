"""Crea el usuario administrador inicial.

Uso:
    python -m scripts.seed_admin admin@flotilla.mx MiPasswordSegura123
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.security import hash_password
from app.database import SessionLocal
from app.models import Operator, PermissionLevel, User, UserRole


async def main(email: str, password: str) -> None:
    async with SessionLocal() as db:
        existing = await db.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none() is not None:
            print(f"El usuario {email} ya existe.")
            return

        user = User(
            email=email,
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
        )
        db.add(user)
        await db.flush()

        db.add(
            Operator(
                user_id=user.id,
                full_name="Administrador",
                permission_level=PermissionLevel.ADMIN,
            )
        )
        await db.commit()
        print(f"Administrador creado: {email}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
