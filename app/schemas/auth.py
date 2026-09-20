"""Schemas de entrada/salida del flujo de autenticación."""

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.phone import InvalidPhoneError, normalize_mx_phone


class DriverLoginRequest(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20, examples=["6621234567"])
    pin: str = Field(..., min_length=4, max_length=8)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        """Acepta 10 dígitos o +52… y compara contra ambos en el login.
        Si el formato no es un móvil MX, se deja pasar para que el login
        responda el mismo 401 genérico (no delatar el formato)."""
        try:
            normalized = normalize_mx_phone(value)
        except InvalidPhoneError:
            return value.strip()
        return normalized or value.strip()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    device_info: str | None = Field(None, max_length=255)

    @field_validator("password")
    @classmethod
    def _bcrypt_byte_limit(cls, v: str) -> str:
        """bcrypt trunca en silencio todo lo que pase de 72 bytes (no
        caracteres — un acento o emoji pesa varios bytes en UTF-8): dos
        contraseñas que compartan esos primeros 72 bytes se autentican igual.
        Se rechaza explícito en vez de dejar que password_hash trunque solo."""
        if len(v.encode("utf-8")) > 72:
            raise ValueError("La contraseña no puede superar los 72 bytes")
        return v


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # segundos de vida del access token


class DriverTokenResponse(BaseModel):
    """Sin refresh_token a propósito — ver POST /auth/driver-login: el
    chofer no tiene forma de renovar en silencio, cuando expires_in se
    cumple tiene que volver a capturar su PIN."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # segundos de vida del access token (DRIVER_ACCESS_TOKEN_HOURS)


class MessageResponse(BaseModel):
    detail: str
