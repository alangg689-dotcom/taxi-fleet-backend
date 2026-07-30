"""Vehículos y su historial de asignación a choferes.

VEHICLE_ASSIGNMENT sustituye a un campo `current_driver_id` suelto: al guardar
started_at/ended_at se conserva el historial completo de turnos para auditoría.
El chofer actual es simplemente la asignación con ended_at = NULL.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import VehicleStatus


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plate: Mapped[str] = mapped_column(String(15), unique=True, index=True)
    model: Mapped[str] = mapped_column(String(100))
    year: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[VehicleStatus] = mapped_column(
        Enum(
            VehicleStatus,
            name="vehicle_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=VehicleStatus.OFFLINE,
    )
    # Credencial ligera del dispositivo: el endpoint de telemetría la valida en
    # lugar de un JWT completo, porque se invoca cada 5-10 segundos por unidad.
    device_key_hash: Mapped[str | None] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    assignments: Mapped[list["VehicleAssignment"]] = relationship(
        back_populates="vehicle"
    )


class VehicleAssignment(Base):
    __tablename__ = "vehicle_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    driver_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drivers.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    vehicle: Mapped["Vehicle"] = relationship(back_populates="assignments")
    driver: Mapped["Driver"] = relationship(back_populates="assignments")

    __table_args__ = (
        # Acelera la búsqueda del turno activo de cada unidad.
        Index("ix_assignment_vehicle_active", "vehicle_id", "ended_at"),
    )
