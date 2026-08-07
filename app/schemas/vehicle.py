"""Schemas de vehículos y asignaciones de turno."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import VehicleStatus


class VehicleCreate(BaseModel):
    plate: str = Field(..., max_length=15)
    model: str = Field(..., max_length=100)
    year: int | None = Field(None, ge=1990, le=2100)
    # Son 6 sitios fijos y toda unidad pertenece a uno — ver
    # spec-sitios-y-fila-v2.md. Requerido a propósito: no existe un "sin
    # sitio" válido, ni siquiera como default.
    stand_id: UUID


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
