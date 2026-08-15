"""Aviso automático de "el taxi ya llegó".

Cuelga del mismo camino que la evaluación de la fila de sitios: cada lote de
pings que entra dispara una comprobación en una tarea aparte, para no meter
otra consulta geoespacial en el camino caliente de _persist_pings.

Solo mira viajes en `asignado`: el chofer aceptó y va en camino a recoger. En
`en_curso` el pasajero ya va a bordo y avisar que llegó no significa nada; en
`solicitado` todavía no hay unidad asignada.

El aviso se manda UNA sola vez por viaje. La unidad sigue mandando pings
mientras espera afuera con el motor encendido, y sin candado el cliente
recibiría "tu taxi llegó" cada cinco segundos hasta que se subiera.
"""

import logging
import uuid

from sqlalchemy import text

from app.config import settings
from app.core.customer_notify import notify_customer
from app.core.redis_client import redis_client
from app.database import SessionLocal
from app.models import Trip

logger = logging.getLogger(__name__)

# TTL del candado: lo bastante largo para cubrir la espera más generosa del
# chofer afuera del domicilio, y aun así finito para que la llave no se quede
# para siempre en Redis si el viaje nunca se completa.
_ARRIVED_LOCK_TTL_SECONDS = 3600


def _arrived_key(trip_id: uuid.UUID) -> str:
    return f"trip:arrived:{trip_id}"


async def _claim_arrival(trip_id: uuid.UUID) -> bool:
    """SET NX: True solo para el primer ping que entra al radio. Va en Redis y
    no en una columna porque es coordinación entre instancias del backend, no
    un dato del viaje que alguien vaya a consultar después."""
    return bool(
        await redis_client.set(_arrived_key(trip_id), "1", ex=_ARRIVED_LOCK_TTL_SECONDS, nx=True)
    )


async def run_arrival_check(vehicle_id: uuid.UUID, lat: float, lng: float) -> None:
    """Se lanza con asyncio.create_task por lote de pings; nunca lanza hacia
    arriba, igual que el resto de los enganches del camino caliente."""
    try:
        async with SessionLocal() as db:
            # La distancia se calcula en Postgres, no en Python: `origin` es
            # geography, así que ST_DWithin ya trabaja en metros sobre el
            # elipsoide y usa el índice espacial de la columna.
            row = (
                await db.execute(
                    text(
                        """
                        SELECT t.id, v.plate
                        FROM trips t
                        JOIN vehicles v ON v.id = t.vehicle_id
                        WHERE t.vehicle_id = :vehicle_id
                          AND t.status = 'asignado'
                          AND t.customer_channel IS NOT NULL
                          AND ST_DWithin(
                                t.origin,
                                ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                                :radius
                              )
                        LIMIT 1
                        """
                    ),
                    {
                        "vehicle_id": vehicle_id,
                        "lat": lat,
                        "lng": lng,
                        "radius": settings.TRIP_ARRIVAL_RADIUS_METERS,
                    },
                )
            ).mappings().first()

            if row is None:
                return

            trip_id = row["id"]
            if not await _claim_arrival(trip_id):
                return  # ya se avisó por este viaje

            trip = await db.get(Trip, trip_id)
            if trip is None:
                return

            await notify_customer(
                trip,
                f"🚖 ¡El taxi ha llegado a tu ubicación! Unidad {row['plate']} — "
                "el conductor te espera afuera.",
            )

        logger.info(
            "Viaje %s: unidad %s llegó al punto de recogida (< %.0f m), cliente avisado",
            trip_id,
            vehicle_id,
            settings.TRIP_ARRIVAL_RADIUS_METERS,
        )
    except Exception:
        # Un aviso que no salió no puede tumbar la ingesta de telemetría.
        logger.exception("Error comprobando la llegada de la unidad %s", vehicle_id)
