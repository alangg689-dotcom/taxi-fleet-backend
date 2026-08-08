"""Endpoints de autenticación.

Dos caminos según el rol:
  - Choferes: teléfono + PIN, asignado por el operador (sin Twilio, sin
    contraseñas que memorizar en campo).
  - Operadores/admin: email + contraseña desde el dashboard web.

Ambos terminan emitiendo el mismo par access/refresh token.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import login_throttle
from app.core.deps import get_current_user
from app.core.security import (
    create_access_token,
    generate_token,
    hash_token,
    refresh_token_expiry,
    verify_password,
)
from app.database import get_db
from app.models import Driver, RefreshToken, User, UserRole
from app.schemas.auth import (
    DriverLoginRequest,
    DriverTokenResponse,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    TokenPair,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


async def _issue_token_pair(
    db: AsyncSession, user: User, device_info: str | None
) -> TokenPair:
    """Emite access token (JWT) + refresh token (opaco, hasheado en BD)."""
    access = create_access_token(str(user.id), user.role.value)

    raw_refresh = generate_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            device_info=device_info,
            expires_at=refresh_token_expiry(),
        )
    )
    await db.flush()

    return TokenPair(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
    )


# --- Login con PIN (choferes) --------------------------------------------------

@router.post("/driver-login", response_model=DriverTokenResponse)
async def driver_login(payload: DriverLoginRequest, db: AsyncSession = Depends(get_db)):
    """Teléfono + PIN — el PIN lo asigna el operador (POST /drivers o
    POST /drivers/{id}/pin), no lo elige el chofer. Un solo paso, a
    diferencia del OTP anterior: no hay nada que enumerar aparte con un
    segundo endpoint, así que el throttle y el mensaje genérico de error
    (igual sea teléfono inexistente, sin PIN asignado, o PIN incorrecto)
    bastan, mismo patrón que /auth/login.

    Sin refresh token: emite directo un access token de
    DRIVER_ACCESS_TOKEN_HOURS (no pasa por _issue_token_pair, que sí crea
    uno). Decisión de negocio — el PIN no se guarda en el teléfono, así
    que tampoco debe quedar ahí un refresh token de larga vida que
    reemplace esa protección: un aparato perdido o prestado no debe seguir
    siendo la credencial completa más allá de lo que dure el turno."""
    try:
        await login_throttle.check_not_locked(payload.phone)
    except login_throttle.LoginLockedError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

    result = await db.execute(select(User).where(User.phone == payload.phone))
    user = result.scalar_one_or_none()

    driver = None
    if user is not None:
        driver_result = await db.execute(select(Driver).where(Driver.user_id == user.id))
        driver = driver_result.scalar_one_or_none()

    if (
        user is None
        or driver is None
        or driver.pin_hash is None
        or not secrets.compare_digest(driver.pin_hash, hash_token(payload.pin))
        or not user.is_active
    ):
        await login_throttle.record_failure(payload.phone)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales inválidas")

    await login_throttle.reset(payload.phone)
    access = create_access_token(
        str(user.id),
        user.role.value,
        expires_delta=timedelta(hours=settings.DRIVER_ACCESS_TOKEN_HOURS),
    )
    return DriverTokenResponse(
        access_token=access, expires_in=settings.DRIVER_ACCESS_TOKEN_HOURS * 3600
    )


# --- Login con contraseña (operadores) ----------------------------------------

@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    try:
        await login_throttle.check_not_locked(payload.email)
    except login_throttle.LoginLockedError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if (
        user is None
        or user.password_hash is None
        or not verify_password(payload.password, user.password_hash)
        or not user.is_active
    ):
        await login_throttle.record_failure(payload.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales inválidas")

    # Contraseña correcta: ya no es un intento de fuerza bruta, aunque el
    # rechazo de abajo (rol chofer) impida emitir tokens.
    await login_throttle.reset(payload.email)

    if user.role == UserRole.DRIVER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Los choferes ingresan con teléfono y PIN"
        )

    return await _issue_token_pair(db, user, payload.device_info)


# --- Renovación y cierre de sesión --------------------------------------------

@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """Rota el refresh token: el anterior se revoca al emitir uno nuevo, así un
    token robado deja de servir en cuanto el dueño legítimo lo usa."""
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(payload.refresh_token)
        )
    )
    token = result.scalar_one_or_none()

    now = datetime.now(UTC)
    if token is None or token.revoked_at is not None or token.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token inválido")

    user = await db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario inactivo")

    token.revoked_at = now
    return await _issue_token_pair(db, user, token.device_info)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(payload.refresh_token),
            RefreshToken.user_id == user.id,
        )
    )
    token = result.scalar_one_or_none()
    if token is not None:
        token.revoked_at = datetime.now(UTC)

    return MessageResponse(detail="Sesión cerrada")
