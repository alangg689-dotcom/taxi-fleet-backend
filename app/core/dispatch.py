"""Motor de despacho automático: encuentra al chofer disponible más cercano
y le ofrece el viaje, en cascada, hasta que alguien acepta o se acaban los
candidatos.

Quién cuenta como "candidato" para un viaje:
  1. Tiene un turno abierto (vehicle_assignments.ended_at IS NULL) — es decir,
     hay un chofer manejando esa unidad ahora mismo.
  2. Esa unidad mandó un ping de GPS reciente (DISPATCH_POSITION_FRESHNESS_SECONDS)
     dentro del radio de búsqueda del origen del viaje.
  3. Esa unidad no tiene ya otro viaje activo (solicitado/asignado/en_curso).
  4. Vehicle.status == 'disponible' — el propio chofer decide cuándo con el
     switch de "entrar/salir a trabajar" en su app (POST /vehicles/{id}/status,
     acepta disponible/ocupado/offline — ver VehicleStatusUpdate). Conectarse
     a /ws/driver ya NO cambia el status solo; accept/complete sí lo mueven
     entre ocupado/disponible automáticamente durante un viaje despachado
     por la app.

El "ofrecer y esperar" no usa Pub/Sub para la respuesta: es más simple leer
el propio renglón de trips cada DISPATCH_POLL_INTERVAL_SECONDS que armar un
segundo canal de eventos solo para esto. Pub/Sub sí hace falta para *empujar*
la oferta al WebSocket del chofer, porque ese socket puede estar conectado a
otra instancia del backend (mismo motivo que el resto del pub/sub del
proyecto).

Cada oferta también se manda como notificación push (app.core.push) si el
chofer tiene un token registrado — el Pub/Sub de arriba solo le llega a un
socket vivo; si trae la app en segundo plano o cerrada, el push es la única
forma en que se entera.
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
from app.core.push import send_push_notification
from app.core.redis_client import publish_trip_offer
from app.core.stands import nearest_stand_id_for_trip
from app.core.whatsapp import send_whatsapp_message
from app.database import SessionLocal
from app.models import Driver, Trip, TripStatus

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("solicitado", "asignado", "en_curso")


@dataclass
class Candidate:
    driver_id: uuid.UUID
    vehicle_id: uuid.UUID
    distance_m: float


def _eta_seconds(distance_m: float) -> float:
    """Proxy de ETA sin ruteo real: distancia en línea recta a
    DISPATCH_ETA_SPEED_KMH (spec-sitios-y-fila-v2.md, sección 8)."""
    return distance_m / (settings.DISPATCH_ETA_SPEED_KMH * 1000 / 3600)


async def _tier1_head_of_queue(db: AsyncSession, trip_id: uuid.UUID, zone_id: uuid.UUID) -> list[Candidate]:
    """Escalón 1: la unidad al frente de la fila del sitio de la zona del
    viaje. Solo esa — si rechaza, el resto de la cascada sigue con los
    demás escalones, no con la 2a de la fila (que no es candidata mientras
    no sea ella la primera)."""
    row = (
        await db.execute(
            text(
                """
                SELECT sq.vehicle_id, sq.driver_id, ST_Distance(p.location, t.origin) AS distance_m
                FROM stand_queue sq
                JOIN trips t ON t.id = :trip_id
                JOIN LATERAL (
                    SELECT location FROM location_pings lp
                    WHERE lp.vehicle_id = sq.vehicle_id
                    ORDER BY lp.timestamp DESC LIMIT 1
                ) p ON true
                WHERE sq.stand_id = :zone_id AND sq.status = 'formado'
                ORDER BY sq.position_held DESC, sq.entered_at ASC
                LIMIT 1
                """
            ),
            {"trip_id": trip_id, "zone_id": zone_id},
        )
    ).mappings().first()
    if row is None:
        return []
    return [Candidate(driver_id=row["driver_id"], vehicle_id=row["vehicle_id"], distance_m=row["distance_m"])]


async def _tier4_other_stands_heads(
    db: AsyncSession, trip_id: uuid.UUID, zone_id: uuid.UUID
) -> list[Candidate]:
    """Escalón 4: la unidad al frente de la fila de CADA OTRO sitio activo,
    ordenadas por ETA — es el único escalón donde una unidad formada en SU
    sitio se ofrece para el viaje de otra zona."""
    rows = await db.execute(
        text(
            """
            SELECT DISTINCT ON (sq.stand_id)
                   sq.vehicle_id, sq.driver_id, ST_Distance(p.location, t.origin) AS distance_m
            FROM stand_queue sq
            JOIN trips t ON t.id = :trip_id
            JOIN stands s ON s.id = sq.stand_id AND s.active = true AND s.id != :zone_id
            JOIN LATERAL (
                SELECT location FROM location_pings lp
                WHERE lp.vehicle_id = sq.vehicle_id
                ORDER BY lp.timestamp DESC LIMIT 1
            ) p ON true
            WHERE sq.status = 'formado'
            ORDER BY sq.stand_id, sq.position_held DESC, sq.entered_at ASC
            """
        ),
        {"trip_id": trip_id, "zone_id": zone_id},
    )
    candidates = [
        Candidate(driver_id=r["driver_id"], vehicle_id=r["vehicle_id"], distance_m=r["distance_m"])
        for r in rows.mappings().all()
    ]
    return sorted(candidates, key=lambda c: c.distance_m)


async def _tier2_and_tier3_roaming(
    db: AsyncSession, trip_id: uuid.UUID, zone_id: uuid.UUID | None
) -> tuple[list[Candidate], list[Candidate]]:
    """Unidades disponibles rodando (no formadas en ninguna fila): escalón 2
    si su sitio de origen es la zona del viaje, escalón 3 si es otro.
    Misma consulta base que la versión sin escalones de find_candidate_drivers,
    más NOT EXISTS contra stand_queue: una unidad formada solo se ofrece vía
    escalón 1 (su propia fila) o 4 (la fila de otro sitio), nunca aquí."""
    result = await db.execute(
        text(
            """
            SELECT DISTINCT ON (p.vehicle_id)
                   p.vehicle_id,
                   va.driver_id,
                   v.stand_id,
                   ST_Distance(p.location, t.origin) AS distance_m
            FROM trips t
            JOIN location_pings p ON true
            JOIN vehicle_assignments va
                ON va.vehicle_id = p.vehicle_id AND va.ended_at IS NULL
            JOIN vehicles v ON v.id = p.vehicle_id AND v.status = 'disponible'
            WHERE t.id = :trip_id
              AND p.timestamp > now() - make_interval(secs => :freshness)
              AND ST_DWithin(p.location, t.origin, :radius)
              AND NOT EXISTS (
                  SELECT 1 FROM trips t2
                  WHERE t2.vehicle_id = p.vehicle_id
                    AND t2.id != t.id
                    AND t2.status IN :active_statuses
              )
              AND NOT EXISTS (
                  SELECT 1 FROM stand_queue sq
                  WHERE sq.vehicle_id = p.vehicle_id AND sq.status = 'formado'
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
    tier2, tier3 = [], []
    for r in rows:
        candidate = Candidate(driver_id=r["driver_id"], vehicle_id=r["vehicle_id"], distance_m=r["distance_m"])
        (tier2 if r["stand_id"] == zone_id else tier3).append(candidate)
    return tier2, tier3


async def find_candidate_drivers(db: AsyncSession, trip_id: uuid.UUID) -> list[Candidate]:
    """Candidatos ordenados por los escalones de prioridad de sitios
    (spec-sitios-y-fila-v2.md, sección 8):
      1. Primera de la fila del sitio de la zona del viaje.
      2. Unidad disponible del mismo sitio, rodando (no formada), por ETA.
      3. Unidad disponible de otro sitio, rodando, por ETA.
      4. Primera de la fila de cada otro sitio, por ETA.

    Un escalón inferior solo desplaza al mejor candidato encontrado hasta
    ahora si su ETA es al menos DISPATCH_TIER_ADVANTAGE_SECONDS mejor — si
    no, gana el escalón más alto que tenga algún candidato. El resto de los
    escalones (y el resto de cada uno) queda como respaldo de la cascada
    para cuando el primero rechaza o no responde.

    Si no hay ningún sitio activo (no debería pasar en operación normal:
    siempre hay al menos los sitios placeholder), se degrada a la lista
    plana por distancia de antes de que existiera la fila."""
    zone_id = await nearest_stand_id_for_trip(db, trip_id)
    tier2, tier3 = await _tier2_and_tier3_roaming(db, trip_id, zone_id)

    if zone_id is None:
        return sorted(tier2 + tier3, key=lambda c: c.distance_m)[: settings.DISPATCH_MAX_CANDIDATES]

    tiers = {
        1: await _tier1_head_of_queue(db, trip_id, zone_id),
        2: tier2,
        3: tier3,
        4: await _tier4_other_stands_heads(db, trip_id, zone_id),
    }

    def best_eta(tier_num: int) -> float | None:
        candidates = tiers[tier_num]
        return _eta_seconds(candidates[0].distance_m) if candidates else None

    chosen: int | None = None
    for tier_num in (1, 2, 3, 4):
        if not tiers[tier_num]:
            continue
        if chosen is None:
            chosen = tier_num
            continue
        challenger_eta = best_eta(tier_num)
        current_eta = best_eta(chosen)
        if challenger_eta is not None and current_eta is not None:
            if challenger_eta <= current_eta - settings.DISPATCH_TIER_ADVANTAGE_SECONDS:
                chosen = tier_num

    if chosen is None:
        return []

    ordered = list(tiers[chosen])
    for tier_num in (1, 2, 3, 4):
        if tier_num != chosen:
            ordered.extend(tiers[tier_num])

    return ordered[: settings.DISPATCH_MAX_CANDIDATES]


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
        if trip.customer_phone:
            # Un viaje del bot que se queda "solicitado" para siempre bloquea
            # que ese número pueda pedir otro (ver _trip_still_active en
            # app.core.whatsapp_bot) — a diferencia de uno de operador, aquí
            # no hay nadie viendo el dashboard para redespacharlo a mano.
            async with SessionLocal() as db:
                trip = await db.get(Trip, trip_id)
                if trip is not None and trip.status == TripStatus.SOLICITADO:
                    trip.status = TripStatus.CANCELADO
                    await db.commit()
            await send_whatsapp_message(
                trip.customer_phone,
                "Por ahora no hay taxis disponibles cerca de ti. Intenta de nuevo en unos minutos.",
            )
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

            driver = await db.get(Driver, candidate.driver_id)
            push_token = driver.push_token if driver is not None else None

        await publish_trip_offer(str(candidate.driver_id), payload_base)
        if push_token:
            await send_push_notification(
                push_token,
                title="Nuevo viaje",
                body=payload_base["origin_address"] or "Viaje cerca de ti",
                data={"type": "trip_offer", "trip_id": payload_base["trip_id"]},
            )
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
            customer_phone = trip.customer_phone
            # Mismo motivo que la rama de "sin candidatos" arriba: un viaje
            # del bot que se queda "solicitado" bloquea que ese número pueda
            # pedir otro.
            if customer_phone:
                trip.status = TripStatus.CANCELADO
            await db.commit()
            if customer_phone:
                await send_whatsapp_message(
                    customer_phone,
                    "No encontramos un taxi disponible por ahora. Intenta de nuevo en unos minutos.",
                )
    logger.info("Viaje %s: ningún candidato aceptó", trip_id)
