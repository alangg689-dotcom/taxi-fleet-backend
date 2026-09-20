"""Vehículos y su historial de asignación a choferes.

VEHICLE_ASSIGNMENT sustituye a un campo `current_driver_id` suelto: al guardar
started_at/ended_at se conserva el historial completo de turnos para auditoría.
El chofer actual es simplemente la asignación con ended_at = NULL.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, func, text
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
    # Distinto del device_token del teléfono del chofer (driver_devices).
    device_key_hash: Mapped[str | None] = mapped_column(String(255), index=True)
    # ID operativo de la unidad (ej. CTM-045). Visible. No es device_key
    # ni PIN. Único entre las unidades que sí lo tienen; varios choferes
    # pueden referenciar el mismo folio desde drivers.folio_ctm.
    folio_ctm: Mapped[str | None] = mapped_column(String(20))
    # NOT NULL a propósito: son 6 sitios fijos y toda unidad pertenece a uno
    # (decisión de negocio, ver spec-sitios-y-fila-v2.md). La unidad es la
    # que pertenece al sitio, no el chofer — si rota de unidad vía
    # vehicle_assignments, rota de sitio con ella.
    stand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stands.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    assignments: Mapped[list["VehicleAssignment"]] = relationship(
        back_populates="vehicle"
    )

    __table_args__ = (
        Index(
            "ix_vehicles_folio_ctm",
            "folio_ctm",
            unique=True,
            postgresql_where=text("folio_ctm IS NOT NULL"),
        ),
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
