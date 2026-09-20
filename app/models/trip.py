"""Viajes realizados por la flotilla."""

import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, SmallInteger, String, Text, func
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

    # Los tres solo se llenan en viajes que nacieron de un bot de cliente — es
    # a dónde se le contesta con el estado del viaje (chofer asignado, llegada,
    # etc). Un viaje de operador/dashboard no tiene cliente identificado, así
    # que quedan nulos.
    #
    # `customer_channel` decide por cuál de los dos leer: WhatsApp identifica
    # al cliente por teléfono y Telegram por chat_id. No se unificaron en una
    # sola columna a propósito — el teléfono sigue siendo dato útil por sí
    # mismo (la operadora puede marcarle), el chat_id no le sirve a nadie
    # fuera del bot.
    customer_channel: Mapped[str | None] = mapped_column(String(16), index=True)
    customer_phone: Mapped[str | None] = mapped_column(String(32))
    customer_chat_id: Mapped[str | None] = mapped_column(String(64))

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
    # Calificación del cliente (1-5), capturada por el bot al terminar el
    # viaje. Nula si no contestó — calificar es opcional y no se insiste.
    # El CHECK de rango vive en la base (migración 0014).
    rating: Mapped[int | None] = mapped_column(SmallInteger)


class TripMessage(Base):
    """Un mensaje del hilo cliente↔chofer de un viaje.

    El hilo es el propio viaje: no hay tabla de threads. Solo se escribe
    mientras el viaje está `asignado` o `en_curso` (lo impone
    app.core.trip_chat, no un trigger). `sender` es texto a propósito —
    ver TripMessageSender.
    """

    __tablename__ = "trip_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_trip_messages_trip_id_created_at", "trip_id", "created_at"),
        # El CHECK de sender/body vive en la migración 0015; el índice
        # compuesto es el que usa GET /trips/{id}/messages al paginar.
    )
