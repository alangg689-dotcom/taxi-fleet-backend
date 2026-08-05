"""Viajes realizados por la flotilla."""

import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import TripStatus


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Nullable: un viaje despachado automáticamente nace sin saber todavía
    # quién lo va a tomar (ver offered_driver_id más abajo). Un viaje creado
    # a mano por un operador sí trae ambos desde el alta.
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id"), index=True
    )
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drivers.id"), index=True
    )

    # A quién se le está ofreciendo el viaje ahora mismo mientras el motor de
    # despacho recorre candidatos cercanos. Se limpia al aceptar (pasa a
    # driver_id/vehicle_id), al rechazar, o al expirar.
    offered_driver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drivers.id")
    )
    offered_vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id")
    )
    offer_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    origin: Mapped[str] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True)
    )
    destination: Mapped[str | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True)
    )
    origin_address: Mapped[str | None] = mapped_column(String(255))
    destination_address: Mapped[str | None] = mapped_column(String(255))

    # Solo se llena en viajes que nacieron del bot de WhatsApp — es a dónde
    # se le contesta con el estado del viaje (chofer asignado, etc). Un viaje
    # de operador/dashboard no tiene cliente identificado, así que queda nulo.
    customer_phone: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[TripStatus] = mapped_column(
        Enum(TripStatus, name="trip_status", values_callable=lambda e: [m.value for m in e]),
        default=TripStatus.SOLICITADO,
        index=True,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Lo que cobró el chofer, capturado a mano al completar el viaje — no hay
    # tarifa calculada automáticamente (ni por distancia ni por tiempo) en
    # este proyecto. Sirve para que el chofer lleve su propio registro de
    # ingresos, no para facturar al pasajero.
    fare: Mapped[float | None] = mapped_column(Float)
