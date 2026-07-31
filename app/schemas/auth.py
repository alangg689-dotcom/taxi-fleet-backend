"""Schemas de entrada/salida del flujo de autenticación."""

from pydantic import BaseModel, EmailStr, Field, field_validator


class OTPRequest(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20, examples=["+525512345678"])


class OTPVerify(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)
    code: str = Field(..., min_length=4, max_length=8)
    device_info: str | None = Field(None, max_length=255, examples=["Pixel 7 / Android 14"])


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


class MessageResponse(BaseModel):
    detail: str
