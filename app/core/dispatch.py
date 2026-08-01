"""Motor de despacho automático: encuentra al chofer disponible más cercano
y le ofrece el viaje, en cascada, hasta que alguien acepta o se acaban los
candidatos.

Quién cuenta como "candidato" para un viaje:
  1. Tiene un turno abierto (vehicle_assignments.ended_at IS NULL) — es decir,
     hay un chofer manejando esa unidad ahora mismo.
  2. Esa unidad mandó un ping de GPS reciente (DISPATCH_POSITION_FRESHNESS_SECONDS)
     dentro del radio de búsqueda del origen del viaje.
  3. Esa unidad no tiene ya otro viaje activo (solicitado/asignado/en_curso).

El "ofrecer y esperar" no usa Pub/Sub para la respuesta: es más simple leer
el propio renglón de trips cada DISPATCH_POLL_INTERVAL_SECONDS que armar un
segundo canal de eventos solo para esto. Pub/Sub sí hace falta para *empujar*
la oferta al WebSocket del chofer, porque ese socket puede estar conectado a
otra instancia del backend (mismo motivo que el resto del pub/sub del
proyecto).
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from geoalchemy2 import Geometry
from sqlalchemy import bindparam, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.redis_client import publish_trip_offer
from app.database import SessionLocal
from app.models import Trip, TripStatus

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("solicitado", "asignado", "en_curso")


@dataclass
class Candidate:
    driver_id: uuid.UUID
    vehicle_id: uuid.UUID
    distance_m: float


async def find_candidate_drivers(db: AsyncSession, trip_id: uuid.UUID) -> list[Candidate]:
    """Candidatos ordenados por cercanía al origen del viaje `trip_id`."""
    result = await db.execute(
        text(
            """
            SELECT DISTINCT ON (p.vehicle_id)
                   p.vehicle_id,
                   va.driver_id,
                   ST_Distance(p.location, t.origin) AS distance_m
            FROM trips t
            JOIN location_pings p ON true
            JOIN vehicle_assignments va
                ON va.vehicle_id = p.vehicle_id AND va.ended_at IS NULL
            WHERE t.id = :trip_id
              AND p.timestamp > now() - make_interval(secs => :freshness)
              AND ST_DWithin(p.location, t.origin, :radius)
              AND NOT EXISTS (
                  SELECT 1 FROM trips t2
                  WHERE t2.vehicle_id = p.vehicle_id
                    AND t2.id != t.id
                    AND t2.status IN :active_statuses
              )
            ORDER BY p.vehicle_id, p.timestamp DESC
            """
        ).bindparams(bindparam("active_statuses", expanding=True)),
        {
            "trip_id": trip_id,
            "freshness": settings.DISPATCH_POSITION_FRESHNESS_SECONDS,
            "radius": settings.DISPATCH_SEARCH_RADIUS_METERS,
            "active_statuses": _ACTIVE_STATUSES,
        },
    )
    rows = sorted(result.mappings().all(), key=lambda r: r["distance_m"])
    return [
        Candidate(driver_id=r["driver_id"], vehicle_id=r["vehicle_id"], distance_m=r["distance_m"])
        for r in rows[: settings.DISPATCH_MAX_CANDIDATES]
    ]


async def _offer_payload(db: AsyncSession, trip: Trip) -> dict:
    result = await db.execute(
        select(
            func.ST_Y(cast(Trip.origin, Geometry)).label("origin_lat"),
            func.ST_X(cast(Trip.origin, Geometry)).label("origin_lng"),
        ).where(Trip.id == trip.id)
    )
    point = result.mappings().one()
    return {
        "trip_id": str(trip.id),
        "origin_lat": point["origin_lat"],
        "origin_lng": point["origin_lng"],
        "origin_address": trip.origin_address,
        "destination_address": trip.destination_address,
        "expires_in": settings.DISPATCH_OFFER_TIMEOUT_SECONDS,
    }


async def _wait_for_response(trip_id: uuid.UUID, offered_driver_id: uuid.UUID) -> bool:
    """True si `driver_id` quedó asignado (aceptado). False si lo rechazaron,
    expiró, o el viaje desapareció/se canceló mientras tanto."""
    deadline = time.monotonic() + settings.DISPATCH_OFFER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        async with SessionLocal() as db:
            trip = await db.get(Trip, trip_id)
            if trip is None or trip.status == TripStatus.CANCELADO:
                return False
            if trip.driver_id is not None:
                return True
            if trip.offered_driver_id != offered_driver_id:
                # Rechazo explícito (u otra cosa limpió la oferta): no vale
                # la pena esperar el resto del timeout.
                return False
        await asyncio.sleep(settings.DISPATCH_POLL_INTERVAL_SECONDS)
    return False


async def dispatch_trip(trip_id: uuid.UUID) -> None:
    """Recorre candidatos cercanos en orden hasta que alguien acepte.

    Pensado para lanzarse con `asyncio.create_task` justo después de crear un
    viaje sin chofer asignado; no se espera su resultado desde el endpoint
    que lo dispara.
    """
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        if trip is None or trip.status != TripStatus.SOLICITADO:
            return
        candidates = await find_candidate_drivers(db, trip_id)
        payload_base = await _offer_payload(db, trip)

    if not candidates:
        logger.info("Viaje %s: sin unidades candidatas cerca del origen", trip_id)
        return

    for candidate in candidates:
        async with SessionLocal() as db:
            trip = await db.get(Trip, trip_id)
            if trip is None or trip.status != TripStatus.SOLICITADO:
                return  # se canceló o ya lo tomó alguien por otra vía

            trip.offered_driver_id = candidate.driver_id
            trip.offered_vehicle_id = candidate.vehicle_id
            trip.offer_expires_at = datetime.now(UTC) + timedelta(
                seconds=settings.DISPATCH_OFFER_TIMEOUT_SECONDS
            )
            await db.commit()

        await publish_trip_offer(str(candidate.driver_id), payload_base)
        logger.info(
            "Viaje %s: ofrecido a chofer %s (unidad %s, %.0fm)",
            trip_id, candidate.driver_id, candidate.vehicle_id, candidate.distance_m,
        )

        if await _wait_for_response(trip_id, candidate.driver_id):
            logger.info("Viaje %s: aceptado por chofer %s", trip_id, candidate.driver_id)
            return

    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        if trip is not None and trip.status == TripStatus.SOLICITADO:
            trip.offered_driver_id = None
            trip.offered_vehicle_id = None
            trip.offer_expires_at = None
            await db.commit()
    logger.info("Viaje %s: ningún candidato aceptó", trip_id)
