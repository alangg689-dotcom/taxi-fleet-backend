"""Importa todos los modelos para que Alembic los detecte en el metadata."""

from app.models.enums import (
    CustomerChannel,
    DriverStatus,
    PermissionLevel,
    StandQueueStatus,
    TripMessageSender,
    TripStatus,
    UserRole,
    VehicleStatus,
)
from app.models.stand import Stand, StandQueue, StandQueueEvent
from app.models.telemetry import LocationPing
from app.models.trip import Trip, TripMessage
from app.models.user import Driver, Operator, RefreshToken, User
from app.models.vehicle import Vehicle, VehicleAssignment

__all__ = [
    "CustomerChannel",
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
    "TripMessage",
    "TripMessageSender",
    "TripStatus",
    "User",
    "UserRole",
    "Vehicle",
    "VehicleAssignment",
    "VehicleStatus",
]
