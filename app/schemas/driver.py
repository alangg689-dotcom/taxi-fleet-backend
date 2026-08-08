"""Schemas de choferes."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DriverStatus


class DriverCreate(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20, examples=["+525512345678"])
    full_name: str = Field(..., max_length=150)
    license_number: str = Field(..., max_length=50)


class DriverUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=150)
    status: DriverStatus | None = None


class PushTokenUpdate(BaseModel):
    """Token de push de Expo del teléfono del chofer — se sobreescribe en
    cada registro, así que el que quede es siempre el del dispositivo activo."""

    push_token: str = Field(..., min_length=1, max_length=255)


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    phone: str
    full_name: str
    license_number: str
    status: DriverStatus
    is_active: bool
    # Derivado (pin_hash is not None) — nunca el hash mismo. Para que el
    # dashboard distinga a quién le falta asignarle un PIN todavía (los
    # migrados del login por OTP nacieron sin uno).
    has_pin: bool


class DriverCreated(DriverOut):
    """El PIN se muestra UNA sola vez, al dar de alta al chofer o al
    regenerarlo (POST /drivers/{id}/pin). Después solo queda su hash."""

    pin: str
