"""Schemas de vehículos y asignaciones de turno."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.phone import InvalidPhoneError, normalize_mx_phone
from app.models.enums import VehicleStatus


def _optional_folio(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip().upper()


def _optional_phone(value: str | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return normalize_mx_phone(value)
    except InvalidPhoneError as exc:
        raise ValueError(str(exc)) from exc


class VehicleCreate(BaseModel):
    plate: str = Field(..., max_length=15)
    model: str = Field(..., max_length=100)
    year: int | None = Field(None, ge=1990, le=2100)
    # Son 6 sitios fijos y toda unidad pertenece a uno — ver
    # spec-sitios-y-fila-v2.md. Requerido a propósito: no existe un "sin
    # sitio" válido, ni siquiera como default.
    stand_id: UUID
    # ID operativo visible (CTM-045). No es device_key ni PIN.
    folio_ctm: str | None = Field(None, max_length=20)
    # Si viene, se WhatsAppea la device_key a este número al dar de alta.
    driver_phone: str | None = Field(None, max_length=20, examples=["6621234567"])
    notify: bool = True

    @field_validator("folio_ctm")
    @classmethod
    def _folio(cls, value: str | None) -> str | None:
        return _optional_folio(value)

    @field_validator("driver_phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _optional_phone(value)


class DeviceKeyNotifyIn(BaseModel):
    """Cuerpo opcional de POST /vehicles/{id}/device-key.

    Sin cuerpo (o vacío) se WhatsAppea al chofer del turno abierto, si
    hay teléfono. `notify=false` solo regenera y devuelve la clave.
    """

    phone: str | None = Field(None, max_length=20, examples=["6621234567"])
    notify: bool = True

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _optional_phone(value)


class VehicleUpdate(BaseModel):
    model: str | None = None
    year: int | None = None
    status: VehicleStatus | None = None
    stand_id: UUID | None = None


class VehicleStatusUpdate(BaseModel):
    """A diferencia de VehicleUpdate (solo staff), esto lo puede mandar el
    propio chofer — disponible/ocupado para el corte de calle, y offline
    para el switch de "entrar/salir a trabajar" de la app (conectarse ya no
    lo pone disponible solo, ver app.ws.fleet.driver_socket). Mantenimiento
    sigue siendo decisión de un operador."""

    status: Literal[VehicleStatus.DISPONIBLE, VehicleStatus.OCUPADO, VehicleStatus.OFFLINE]


class VehicleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    plate: str
    model: str
    year: int | None
    status: VehicleStatus
    stand_id: UUID
    # Del chofer del turno abierto (vehicle_assignments.ended_at IS NULL).
    # Nulos si la unidad no trae turno abierto, o si ese chofer es anterior a
    # la migración 0011 y todavía no tiene numeral. El mapa de flota rotula
    # con el numeral y cae de vuelta a la placa cuando viene nulo.
    driver_numeral: str | None = None
    driver_name: str | None = None
    # ID operativo de la unidad (ej. CTM-045). Visible. No es device_key.
    folio_ctm: str | None = None


class VehicleCreated(VehicleOut):
    """La clave del dispositivo se muestra UNA sola vez, al dar de alta la
    unidad. Después solo queda su hash en la base de datos."""

    device_key: str


class AssignmentCreate(BaseModel):
    driver_id: UUID


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vehicle_id: UUID
    driver_id: UUID
    started_at: datetime
    ended_at: datetime | None
