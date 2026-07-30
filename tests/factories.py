"""Helpers para sembrar datos directamente en la sesión de una prueba.

Van directo al ORM en vez de pasar por /auth o /vehicles: es el "arrange" de
cada prueba, no lo que se está probando, y hacerlo por HTTP solo agregaría
ruido y tiempo de ejecución.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models import Driver, DriverStatus, Operator, PermissionLevel, User, UserRole, Vehicle

DEFAULT_PASSWORD = "Password123"


async def make_staff_user(
    db: AsyncSession, *, role: UserRole = UserRole.OPERATOR, email: str | None = None
) -> tuple[User, str]:
    """Crea un operador/admin y devuelve (user, access_token)."""
    email = email or f"{role.value}-{uuid.uuid4().hex[:8]}@flotilla.mx"
    user = User(email=email, password_hash=hash_password(DEFAULT_PASSWORD), role=role)
    db.add(user)
    await db.flush()

    db.add(
        Operator(
            user_id=user.id,
            full_name="Operador de prueba",
            permission_level=(
                PermissionLevel.ADMIN if role == UserRole.ADMIN else PermissionLevel.DESPACHADOR
            ),
        )
    )
    await db.flush()

    token = create_access_token(str(user.id), role.value)
    return user, token


async def make_driver(
    db: AsyncSession, *, phone: str | None = None, license_number: str | None = None
) -> tuple[Driver, str]:
    """Crea un chofer (User + Driver) y devuelve (driver, access_token)."""
    phone = phone or f"+5255{uuid.uuid4().int % 10**8:08d}"
    license_number = license_number or f"LIC-{uuid.uuid4().hex[:10]}"

    user = User(phone=phone, role=UserRole.DRIVER)
    db.add(user)
    await db.flush()

    driver = Driver(
        user_id=user.id,
        full_name="Chofer de prueba",
        license_number=license_number,
        status=DriverStatus.ACTIVO,
    )
    db.add(driver)
    await db.flush()

    token = create_access_token(str(user.id), UserRole.DRIVER.value)
    return driver, token


async def make_vehicle(db: AsyncSession, *, plate: str | None = None) -> Vehicle:
    plate = plate or f"TST-{uuid.uuid4().hex[:6].upper()}"
    vehicle = Vehicle(plate=plate, model="Vehículo de prueba")
    db.add(vehicle)
    await db.flush()
    return vehicle


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
