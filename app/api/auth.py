"""Endpoints de autenticación.

Dos caminos según el rol:
  - Choferes: teléfono + OTP por SMS (sin contraseñas que memorizar en campo).
  - Operadores/admin: email + contraseña desde el dashboard web.

Ambos terminan emitiendo el mismo par access/refresh token.
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import otp as otp_service
from app.core.deps import get_current_user
from app.core.security import (
    create_access_token,
    generate_token,
    hash_token,
    refresh_token_expiry,
    verify_password,
)
from app.database import get_db
from app.models import RefreshToken, User, UserRole
from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    OTPRequest,
    OTPVerify,
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


# --- Flujo OTP (choferes) -----------------------------------------------------

@router.post("/otp/request", response_model=MessageResponse)
async def request_otp(payload: OTPRequest, db: AsyncSession = Depends(get_db)):
    """Paso 1: el chofer pide un código.

    Se responde igual exista o no el teléfono, para no revelar qué números
    están dados de alta en el sistema.
    """
    result = await db.execute(select(User).where(User.phone == payload.phone))
    user = result.scalar_one_or_none()

    if user is not None and user.is_active:
        try:
            code = await otp_service.request_otp(payload.phone)
        except otp_service.OTPError as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

        try:
            await otp_service.send_sms(payload.phone, code)
        except otp_service.SMSDeliveryError:
            # No se refleja al cliente: distinguir "SMS falló" de "teléfono no
            # registrado" sería el mismo hueco de enumeración que este endpoint
            # evita respondiendo siempre el mismo mensaje genérico.
            logger.error("Falló el envío de OTP a %s", payload.phone, exc_info=True)

    return MessageResponse(detail="Si el número está registrado, recibirás un código.")


@router.post("/otp/verify", response_model=TokenPair)
async def verify_otp(payload: OTPVerify, db: AsyncSession = Depends(get_db)):
    """Paso 2: el chofer envía el código y recibe sus tokens."""
    try:
        valid = await otp_service.verify_otp(payload.phone, payload.code)
    except otp_service.OTPError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Código incorrecto")

    result = await db.execute(select(User).where(User.phone == payload.phone))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario no encontrado")

    return await _issue_token_pair(db, user, payload.device_info)


# --- Login con contraseña (operadores) ----------------------------------------

@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if (
        user is None
        or user.password_hash is None
        or not verify_password(payload.password, user.password_hash)
        or not user.is_active
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales inválidas")

    if user.role == UserRole.DRIVER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Los choferes ingresan con teléfono y código"
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
