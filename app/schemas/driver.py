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


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    phone: str
    full_name: str
    license_number: str
    status: DriverStatus
