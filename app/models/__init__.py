"""Importa todos los modelos para que Alembic los detecte en el metadata."""

from app.models.enums import (
    DriverStatus,
    PermissionLevel,
    StandQueueStatus,
    TripStatus,
    UserRole,
    VehicleStatus,
)
from app.models.stand import Stand, StandQueue, StandQueueEvent
from app.models.telemetry import LocationPing
from app.models.trip import Trip
from app.models.user import Driver, Operator, RefreshToken, User
from app.models.vehicle import Vehicle, VehicleAssignment

__all__ = [
    "Driver",
    "DriverStatus",
    "LocationPing",
    "Operator",
    "PermissionLevel",
    "RefreshToken",
    "Stand",
    "StandQueue",
    "StandQueueEvent",
    "StandQueueStatus",
    "Trip",
    "TripStatus",
    "User",
    "UserRole",
    "Vehicle",
    "VehicleAssignment",
    "VehicleStatus",
]
