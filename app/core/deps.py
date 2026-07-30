"""Dependencias de autenticación y autorización (RBAC)."""

import uuid

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token, hash_token
from app.database import get_db
from app.models import User, UserRole, Vehicle

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta el token de acceso")

    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o expirado")

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario inactivo")
    return user


def require_roles(*roles: UserRole):
    """Restringe un endpoint a ciertos roles.

    Ejemplo: `Depends(require_roles(UserRole.ADMIN))` en endpoints de alta de
    unidades, o `require_roles(UserRole.OPERATOR, UserRole.ADMIN)` para el
    dashboard, donde el chofer no debe poder ver toda la flota.
    """

    async def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "No tienes permiso para esta operación"
            )
        return user

    return _guard


async def get_vehicle_by_device_key(
    x_device_key: str = Header(..., alias="X-Device-Key"),
    db: AsyncSession = Depends(get_db),
) -> Vehicle:
    """Autenticación ligera para el endpoint de telemetría.

    Validar un JWT completo en cada ping (uno cada 5-10 s por unidad) es un
    gasto innecesario. La app manda una clave fija del dispositivo y se compara
    contra su hash, que está indexado.
    """
    result = await db.execute(
        select(Vehicle).where(Vehicle.device_key_hash == hash_token(x_device_key))
    )
    vehicle = result.scalar_one_or_none()
    if vehicle is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Clave de dispositivo inválida")
    return vehicle
