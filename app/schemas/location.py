"""Schemas de telemetría GPS.

El endpoint acepta un ping suelto o un lote. El lote es indispensable: cuando
el taxi pasa por un túnel o zona sin cobertura, la app acumula posiciones en
almacenamiento local y las descarga todas juntas al recuperar señal.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.config import settings


class LocationPingIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    # Hora de captura del GPS del dispositivo, no la de llegada al servidor.
    timestamp: datetime
    speed: float | None = Field(None, ge=0)
    heading: float | None = Field(None, ge=0, le=360)
    accuracy: float | None = Field(None, ge=0)


class LocationBatchIn(BaseModel):
    pings: list[LocationPingIn] = Field(..., min_length=1)

    @field_validator("pings")
    @classmethod
    def limit_batch_size(cls, v: list[LocationPingIn]) -> list[LocationPingIn]:
        if len(v) > settings.LOCATION_BATCH_MAX:
            raise ValueError(
                f"Máximo {settings.LOCATION_BATCH_MAX} pings por lote. "
                "Divide el buffer en varias peticiones."
            )
        return v


class LocationOut(BaseModel):
    vehicle_id: UUID
    lat: float
    lng: float
    speed: float | None = None
    heading: float | None = None
    accuracy: float | None = None
    timestamp: datetime


class BatchAccepted(BaseModel):
    accepted: int
    duplicates: int = 0


class NearbyVehicle(BaseModel):
    vehicle_id: UUID
    plate: str
    lat: float
    lng: float
    distance_m: float
    timestamp: datetime


class PositionSummary(BaseModel):
    """Posición promedio de una unidad en un bucket de 5 minutos.

    Viene del agregado continuo de TimescaleDB, no de location_pings
    directamente: para reportería sobre rangos largos evita leer millones de
    pings crudos en cada consulta.
    """

    vehicle_id: UUID
    bucket: datetime
    avg_lat: float
    avg_lng: float
    avg_speed: float | None
    max_speed: float | None
    ping_count: int
