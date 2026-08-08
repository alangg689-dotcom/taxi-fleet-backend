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


def generate_pin(length: int = 6) -> str:
    """PIN numérico para el login del chofer — secrets.randbelow (no
    random.randint, que no es criptográficamente seguro).

    A diferencia de generate_token(), un PIN de 6 dígitos tiene poca
    entropía (10^6 combinaciones) — se guarda con hash_token igual que
    pidió el negocio, pero eso es SHA-256 sin sal, pensado para secretos de
    alta entropía (ver su docstring). Con la base comprometida, un PIN así
    es reversible con una tabla precalculada en segundos; lo que sí lo
    protege es login_throttle (5 intentos y bloqueo) contra fuerza bruta
    en línea, que es el vector real mientras el PIN no se filtre de la
    base directamente.
    """
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


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
