"""Schemas de entrada/salida del flujo de autenticación."""

from pydantic import BaseModel, EmailStr, Field


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


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # segundos de vida del access token


class MessageResponse(BaseModel):
    detail: str
