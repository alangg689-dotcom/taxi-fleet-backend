"""Sitios (paradas de taxi) y su fila de espera.

Ver spec-sitios-y-fila-v2.md para el diseño completo. Decisiones tomadas
que la spec original no reflejaba, ya incorporadas aquí:

  - Son 6 sitios fijos; toda unidad pertenece a exactamente uno
    (Vehicle.stand_id NOT NULL, ver app.models.vehicle).
  - La máquina de estados evalúa cada unidad solo contra SU PROPIO
    polígono, no contra los seis — más simple, sin ambigüedad por
    traslape. La validación ST_Intersects al dar de alta un sitio
    (POST /stands) sigue como protección contra un polígono mal trazado
    que se encime con otro (no aquí, es de la capa de endpoints).
  - still_seconds / max_speed_kmh / el buffer del polígono son
    configurables por sitio, aunque hoy los seis compartan el mismo valor
    (los defaults de app.config son el fallback).
"""

import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import StandQueueStatus


class Stand(Base):
    __tablename__ = "stands"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100))
    # Trazado con holgura (ver polygon_buffer_meters) — 15 m en la calle
    # abierta, 8-10 m en sitios de banqueta angosta donde 15 cruzaría al
    # carril de enfrente.
    polygon: Mapped[str] = mapped_column(
        Geography(geometry_type="POLYGON", srid=4326, spatial_index=True)
    )
    center: Mapped[str] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True)
    )
    still_seconds: Mapped[int] = mapped_column(Integer, default=45)
    max_speed_kmh: Mapped[float] = mapped_column(Float, default=5.0)
    polygon_buffer_meters: Mapped[int] = mapped_column(Integer, default=15)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # True para los 6 sitios sembrados por la migración 0008 con un cuadro
    # de referencia, sin polígono real trazado. PATCH /stands/{id} debe
    # apagarla en cuanto un operador trace el polígono de verdad — sirve
    # para que el dashboard los marque distinto y nadie los confunda con
    # sitios reales en unas semanas.
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StandQueue(Base):
    """La fila de un sitio. La posición NO se guarda como número — se
    calcula con ROW_NUMBER() OVER (PARTITION BY stand_id ORDER BY
    position_held DESC, entered_at) en la consulta, no aquí."""

    __tablename__ = "stand_queue"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    stand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stands.id", ondelete="CASCADE"), index=True
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    # Tomado del turno (vehicle_assignments) abierto al momento de
    # formarse — precondición de la máquina de estados, nunca nulo aquí.
    driver_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drivers.id", ondelete="CASCADE"), index=True
    )
    # HORA DEL SERVIDOR, nunca ping.timestamp — ver sección 2 de la spec:
    # la hora del teléfono no puede definir el orden de la fila.
    entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[StandQueueStatus] = mapped_column(
        Enum(
            StandQueueStatus,
            name="stand_queue_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=StandQueueStatus.FORMADO,
        index=True,
    )
    # Conserva el lugar cuando el motor le asigna un viaje de otra zona
    # estando formado en la suya (ver sección 8, "Compensación").
    position_held: Mapped[bool] = mapped_column(Boolean, default=False)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_reason: Mapped[str | None] = mapped_column(String(30))

    __table_args__ = (
        Index("ix_queue_stand_status", "stand_id", "status"),
        # Una unidad no puede estar en dos filas a la vez. Parcial porque
        # el historial (asignado/salio) sí puede repetirse por unidad.
        Index(
            "uq_queue_vehicle_active",
            "vehicle_id",
            unique=True,
            postgresql_where=text("status = 'formado'"),
        ),
    )


class StandQueueEvent(Base):
    """Bitácora — resuelve las discusiones sobre el orden de la fila."""

    __tablename__ = "stand_queue_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    # Nulo permitido: un evento de sistema (p.ej. operator_override sin
    # unidad puntual todavía) no siempre tiene un chofer que culpar/premiar.
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drivers.id", ondelete="SET NULL")
    )
    stand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stands.id", ondelete="CASCADE"), index=True
    )
    # entered_polygon / still_confirmed / joined_queue / exit_confirmed /
    # signal_lost / dropped_no_signal / offered_trip / position_held /
    # operator_override — ver sección 4 de la spec.
    event: Mapped[str] = mapped_column(String(30))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
