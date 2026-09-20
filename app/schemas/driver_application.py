"""Schemas del autorregistro de choferes y del bind de dispositivo."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.phone import InvalidPhoneError, normalize_mx_phone
from app.models.enums import DriverAccountStatus, DriverUnitRole


def _normalize_folio(value: str) -> str:
    folio = value.strip().upper()
    if not folio:
        raise ValueError("El folio CTM es obligatorio")
    return folio


def _normalize_plate(value: str) -> str:
    plate = value.strip().upper()
    if not plate:
        raise ValueError("Las placas son obligatorias")
    return plate


def _optional_phone(value: str | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return normalize_mx_phone(value)
    except InvalidPhoneError as exc:
        raise ValueError(str(exc)) from exc


def _empty_email(value: str | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


class DriverApplicationCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=150, examples=["Juan Pérez"])
    phone: str | None = Field(None, max_length=20, examples=["6621234567"])
    email: EmailStr | None = Field(None, examples=[None])
    folio_ctm: str = Field(
        ...,
        min_length=1,
        max_length=20,
        examples=["CTM-045"],
        description="ID operativo de la unidad. Visible. Nunca es password.",
    )
    license_plate: str = Field(..., min_length=1, max_length=15, examples=["VZE-123-A"])
    unit_role: DriverUnitRole = Field(..., examples=["SHIFT_DRIVER"])
    photo_profile_url: str | None = Field(None, max_length=512)
    photo_license_url: str | None = Field(None, max_length=512)

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _optional_phone(value)

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return _empty_email(value)

    @field_validator("folio_ctm")
    @classmethod
    def _folio(cls, value: str) -> str:
        return _normalize_folio(value)

    @field_validator("license_plate")
    @classmethod
    def _plate(cls, value: str) -> str:
        return _normalize_plate(value)

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("El nombre es obligatorio")
        return name


class DriverApplicationCreated(BaseModel):
    application_id: UUID
    status: DriverAccountStatus = DriverAccountStatus.PENDING_APPROVAL


class DriverApplicationStatusOut(BaseModel):
    application_id: UUID
    status: DriverAccountStatus
    rejection_reason: str | None = None
    must_set_pin: bool = False
    folio_ctm: str


class DriverApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    phone: str | None
    email: str | None
    folio_ctm: str
    license_plate: str
    unit_role: DriverUnitRole
    photo_profile_url: str | None
    photo_license_url: str | None
    status: DriverAccountStatus
    rejection_reason: str | None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    driver_id: UUID | None
    created_at: datetime
    updated_at: datetime


class DriverApplicationReject(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("El motivo de rechazo es obligatorio")
        return reason


class DriverApplicationApprove(BaseModel):
    """Al aprobar se crea User+Driver sin PIN. Si la placa no existe y
    viene stand_id, se da de alta la unidad (sin device_key GPS)."""

    stand_id: UUID | None = None
    model: str | None = Field(None, max_length=100)


class ApplicationApproveOut(BaseModel):
    application_id: UUID
    status: DriverAccountStatus
    driver_id: UUID
    user_id: UUID
    must_set_pin: bool = True
    folio_ctm: str
    vehicle_id: UUID | None = None


class UploadKind(BaseModel):
    kind: Literal["profile", "license"]


class UploadCreated(BaseModel):
    url: str
    kind: Literal["profile", "license"]


class DriverSetPinRequest(BaseModel):
    phone: str = Field(..., examples=["6621234567"])
    folio_ctm: str = Field(
        ...,
        examples=["CTM-045"],
        description="ID operativo de la unidad para identificar al chofer. No es el PIN.",
    )
    pin: str = Field(..., min_length=4, max_length=6, examples=["482910"])
    pin_confirm: str = Field(..., min_length=4, max_length=6, examples=["482910"])

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        try:
            normalized = normalize_mx_phone(value)
        except InvalidPhoneError as exc:
            raise ValueError(str(exc)) from exc
        if normalized is None:
            raise ValueError("El teléfono es obligatorio")
        return normalized

    @field_validator("folio_ctm")
    @classmethod
    def _folio(cls, value: str) -> str:
        return _normalize_folio(value)

    @field_validator("pin", "pin_confirm")
    @classmethod
    def _pin_digits(cls, value: str) -> str:
        if not value.isdigit() or not (4 <= len(value) <= 6):
            raise ValueError("El PIN debe ser de 4 a 6 dígitos")
        return value


class DriverChangePinRequest(BaseModel):
    phone: str = Field(..., examples=["6621234567"])
    current_pin: str = Field(..., min_length=4, max_length=8)
    new_pin: str = Field(..., min_length=4, max_length=6, examples=["119933"])
    new_pin_confirm: str = Field(..., min_length=4, max_length=6, examples=["119933"])

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        try:
            normalized = normalize_mx_phone(value)
        except InvalidPhoneError as exc:
            raise ValueError(str(exc)) from exc
        if normalized is None:
            raise ValueError("El teléfono es obligatorio")
        return normalized

    @field_validator("new_pin", "new_pin_confirm")
    @classmethod
    def _pin_digits(cls, value: str) -> str:
        if not value.isdigit() or not (4 <= len(value) <= 6):
            raise ValueError("El PIN debe ser de 4 a 6 dígitos")
        return value


class DriverDeviceBindRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=255)
    push_token: str | None = Field(None, max_length=255)


class DriverDeviceBindOut(BaseModel):
    device_token: str
    folio_ctm: str | None = None


class MessageCountResponse(BaseModel):
    detail: str
    revoked: int = 0
