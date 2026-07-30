"""Hashing de credenciales y emisión/validación de tokens JWT."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --- Contraseñas y secretos ---------------------------------------------------

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def generate_token() -> str:
    """Genera un secreto opaco (refresh token o device key)."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """SHA-256 para tokens de alta rotación.

    A diferencia de las contraseñas, aquí no se usa bcrypt: estos tokens ya
    tienen entropía alta (no son adivinables por fuerza bruta) y se validan en
    cada request, donde el costo deliberadamente lento de bcrypt sería un
    cuello de botella.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# --- JWT ----------------------------------------------------------------------

def create_access_token(subject: str, role: str, extra: dict | None = None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
        "type": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    return payload


def refresh_token_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_DAYS)
