"""Importa todos los modelos para que Alembic los detecte en el metadata."""

from app.models.enums import (
    DriverStatus,
    PermissionLevel,
    TripStatus,
    UserRole,
    VehicleStatus,
)
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
    "Trip",
    "TripStatus",
    "User",
    "UserRole",
    "Vehicle",
    "VehicleAssignment",
    "VehicleStatus",
]
