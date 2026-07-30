"""Schemas de vehículos y asignaciones de turno."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import VehicleStatus


class VehicleCreate(BaseModel):
    plate: str = Field(..., max_length=15)
    model: str = Field(..., max_length=100)
    year: int | None = Field(None, ge=1990, le=2100)


class VehicleUpdate(BaseModel):
    model: str | None = None
    year: int | None = None
    status: VehicleStatus | None = None


class VehicleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    plate: str
    model: str
    year: int | None
    status: VehicleStatus


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
