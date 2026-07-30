"""Schemas de viajes."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import TripStatus


class TripCreate(BaseModel):
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
        if (self.destination_lat is None) != (self.destination_lng is None):
            raise ValueError("destination_lat y destination_lng deben ir juntos")
        return self


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vehicle_id: UUID
    driver_id: UUID
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
