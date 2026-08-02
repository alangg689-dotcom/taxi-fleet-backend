"""Schemas de viajes."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import TripStatus


def _check_destination_pair(lat: float | None, lng: float | None) -> None:
    if (lat is None) != (lng is None):
        raise ValueError("destination_lat y destination_lng deben ir juntos")


class TripCreate(BaseModel):
    """Alta manual: el operador ya eligió qué unidad y qué chofer despachar."""

    vehicle_id: UUID
    driver_id: UUID
    origin_lat: float = Field(..., ge=-90, le=90)
    origin_lng: float = Field(..., ge=-180, le=180)
    origin_address: str | None = Field(None, max_length=255)
    destination_lat: float | None = Field(None, ge=-90, le=90)
    destination_lng: float | None = Field(None, ge=-180, le=180)
    destination_address: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _destination_pair(self) -> "TripCreate":
        _check_destination_pair(self.destination_lat, self.destination_lng)
        return self


class TripDispatchCreate(BaseModel):
    """Alta por despacho automático: sin vehicle_id/driver_id — el motor de
    despacho busca y ofrece el viaje al chofer disponible más cercano."""

    origin_lat: float = Field(..., ge=-90, le=90)
    origin_lng: float = Field(..., ge=-180, le=180)
    origin_address: str | None = Field(None, max_length=255)
    destination_lat: float | None = Field(None, ge=-90, le=90)
    destination_lng: float | None = Field(None, ge=-180, le=180)
    destination_address: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _destination_pair(self) -> "TripDispatchCreate":
        _check_destination_pair(self.destination_lat, self.destination_lng)
        return self


class TripStreetHailCreate(BaseModel):
    """Corte de calle: el propio chofer toma un pasaje sin operador ni motor
    de despacho de por medio. A diferencia de TripDispatchCreate, el origen
    es solo informativo (no dispara una búsqueda de candidatos) — el viaje
    nace ya asignado a quien lo está creando."""

    origin_lat: float = Field(..., ge=-90, le=90)
    origin_lng: float = Field(..., ge=-180, le=180)
    origin_address: str | None = Field(None, max_length=255)


class TripComplete(BaseModel):
    """Lo que cobró el chofer — a mano, no hay cálculo automático de tarifa.
    Opcional: no todos los operadores van a querer llevar este registro."""

    fare: float | None = Field(None, ge=0)


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vehicle_id: UUID | None
    driver_id: UUID | None
    origin_lat: float
    origin_lng: float
    origin_address: str | None
    destination_lat: float | None
    destination_lng: float | None
    destination_address: str | None
    status: TripStatus
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    fare: float | None
    # A quién le está ofreciendo el viaje el motor de despacho ahora mismo
    # (app.core.dispatch); null si el viaje no nació de /trips/dispatch, si
    # ya lo aceptó alguien, o si se acabaron los candidatos.
    offered_driver_id: UUID | None
    offered_vehicle_id: UUID | None
    offer_expires_at: datetime | None
