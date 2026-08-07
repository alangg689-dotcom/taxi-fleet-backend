"""Telemetría GPS: la tabla de mayor volumen de escritura del sistema.

Dos decisiones de diseño clave:

1. `location` usa PostGIS GEOGRAPHY(Point, 4326) en vez de dos columnas decimal.
   Con un índice GiST, las consultas de cercanía y geofencing (ST_DWithin) se
   resuelven en milisegundos aunque la tabla tenga millones de filas.

2. `timestamp` es la hora que reporta el GPS del teléfono, NO la de llegada al
   servidor. Cuando la app vacía su buffer offline tras pasar por un túnel,
   los pings llegan tarde pero deben ordenarse por su hora real de captura.
   `received_at` conserva la hora de llegada solo para diagnóstico de latencia.

La tabla se convierte en hypertable de TimescaleDB en la migración inicial, lo
que resuelve el particionado por fecha y la retención automáticamente.
"""

import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LocationPing(Base):
    __tablename__ = "location_pings"

    # PK compuesta: TimescaleDB exige que la columna de particionado
    # (timestamp) forme parte de cualquier índice único.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    location: Mapped[str] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True)
    )
    speed: Mapped[float | None] = mapped_column(Float)      # km/h
    heading: Mapped[float | None] = mapped_column(Float)    # grados, 0-360
    accuracy: Mapped[float | None] = mapped_column(Float)   # metros
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Capa de validación (spec-sitios-y-fila-v2.md, sección 6): el ping se
    # guarda siempre para el historial de rutas, pero uno con mala precisión,
    # un salto imposible, o posible GPS falso no debe mover la máquina de
    # estados de la fila de sitios. `flag_reason` es None cuando pasó todo.
    queue_eligible: Mapped[bool] = mapped_column(Boolean, server_default="true")
    flag_reason: Mapped[str | None] = mapped_column(String(20))

    __table_args__ = (
        # Consulta típica: "ruta del vehículo X entre dos fechas".
        Index("ix_ping_vehicle_time", "vehicle_id", "timestamp"),
        # Deduplicación: si la app reenvía un lote porque no recibió el ACK,
        # los pings repetidos se descartan en vez de duplicar el historial.
        UniqueConstraint("vehicle_id", "timestamp", name="uq_ping_vehicle_time"),
    )
