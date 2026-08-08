"""Lógica de conversación del bot de WhatsApp.

Mismo camino de despacho que ya usan el dashboard (POST /trips/dispatch) y
las pruebas manuales: aquí se llama directo a dispatch_trip(), sin pasar por
el endpoint HTTP con auth de operador — tal como se dejó anotado desde que se
armó ese endpoint (ver docstring de dispatch_new_trip en app.api.trips).

La conversación es deliberadamente simple, sin árbol de menús: cualquier
mensaje de texto responde con instrucciones para compartir ubicación: en
cuanto llega una ubicación, se despacha. No hay paso de confirmación —
agregar uno es agregar fricción a alguien parado en la calle esperando un
taxi. La única palabra que el bot reconoce es "cancelar".

Un viaje sin candidatos (o que nadie aceptó) ya NO se cancela solo — se
queda "solicitado" y sweep_stuck_bot_trips lo reintenta periódicamente
(la disponibilidad de la flota cambia con el tiempo); solo se avisa y se
cancela cuando se agota BOT_TRIP_MAX_WAIT_SECONDS. Antes se cancelaba en
la primera pasada sin candidatos, lo cual era simple pero le daba al
cliente una unidad "no disponible" que segundos después sí lo estaba.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.api.location import _point
from app.config import settings
from app.core.dispatch import dispatch_trip, set_vehicle_status
from app.core.redis_client import redis_client
from app.core.whatsapp import send_whatsapp_message
from app.database import SessionLocal
from app.models import Trip, TripStatus, VehicleStatus

logger = logging.getLogger(__name__)

# Una hora de inactividad y se olvida la conversación — si el cliente vuelve
# a escribir después de eso, empieza de cero como si fuera la primera vez.
_CONVERSATION_TTL_SECONDS = 3600

_CANCEL_KEYWORDS = {"cancelar", "cancela", "cancel"}

_GREETING = (
    "¡Hola! Soy el asistente de Los Tigres. Para pedir un taxi, comparte tu "
    "ubicación (el clip de adjuntar → Ubicación) y te buscamos el más cercano. "
    "Escribe *cancelar* en cualquier momento para cancelar tu viaje."
)
_ALREADY_ACTIVE = "Ya tienes un viaje en curso. En cuanto un chofer confirme, te avisamos por aquí."
_SEARCHING = "Buscando un taxi cerca de ti… te avisamos en cuanto uno confirme."
_CANCELLED = "Tu viaje quedó cancelado. Escríbenos cuando quieras pedir otro."
_NOTHING_TO_CANCEL = "No tienes ningún viaje activo para cancelar."
_GAVE_UP = (
    "Ya llevamos un rato buscando y no encontramos un taxi disponible cerca de ti. "
    "Escríbenos de nuevo cuando quieras intentarlo otra vez."
)


def _state_key(phone: str) -> str:
    return f"wa:conv:{phone}"


async def _get_active_trip_id(phone: str) -> uuid.UUID | None:
    raw = await redis_client.get(_state_key(phone))
    return uuid.UUID(raw) if raw else None


async def _set_active_trip(phone: str, trip_id: uuid.UUID) -> None:
    await redis_client.set(_state_key(phone), str(trip_id), ex=_CONVERSATION_TTL_SECONDS)


async def _clear_active_trip(phone: str) -> None:
    await redis_client.delete(_state_key(phone))


async def _trip_still_active(trip: Trip | None) -> bool:
    """Sin criterio de edad a propósito: "solicitado" ya no es una señal de
    "dispatch_trip lo sigue recorriendo ahora mismo" — puede llevar rato
    esperando a que sweep_stuck_bot_trips lo reintente, y eso es válido. Lo
    único que de verdad importa es el estado: sigue activo mientras no esté
    completado/cancelado. El riesgo de un viaje huérfano que bloqueara al
    cliente para siempre (el motivo original de este chequeo) ya no existe
    con el barrido — cualquier "solicitado" del bot que se estanca, el
    barrido mismo lo cancela al llegar a BOT_TRIP_MAX_WAIT_SECONDS."""
    if trip is None:
        return False
    return trip.status in (TripStatus.SOLICITADO, TripStatus.ASIGNADO, TripStatus.EN_CURSO)


async def _cancel_trip_for_customer(phone: str, trip_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        if trip is not None and trip.status in (
            TripStatus.SOLICITADO, TripStatus.ASIGNADO, TripStatus.EN_CURSO
        ):
            trip.status = TripStatus.CANCELADO
            vehicle_id = trip.vehicle_id
            await db.commit()
            await set_vehicle_status(db, vehicle_id, VehicleStatus.DISPONIBLE, trip_id=trip_id)
    await _clear_active_trip(phone)


async def handle_incoming_message(
    phone: str,
    latitude: float | None,
    longitude: float | None,
    body: str | None = None,
) -> str:
    """Decide la respuesta al mensaje que acaba de mandar `phone`. Nunca
    lanza excepciones de negocio hacia arriba: el webhook necesita poder
    contestarle algo a Twilio siempre, o Twilio reintenta la entrega y
    duplica mensajes."""
    trip_id = await _get_active_trip_id(phone)

    if body is not None and body.strip().lower() in _CANCEL_KEYWORDS:
        if trip_id is None:
            return _NOTHING_TO_CANCEL
        async with SessionLocal() as db:
            trip = await db.get(Trip, trip_id)
        if not await _trip_still_active(trip):
            await _clear_active_trip(phone)
            return _NOTHING_TO_CANCEL
        await _cancel_trip_for_customer(phone, trip_id)
        return _CANCELLED

    if trip_id is not None:
        async with SessionLocal() as db:
            trip = await db.get(Trip, trip_id)
        if await _trip_still_active(trip):
            return _ALREADY_ACTIVE
        await _clear_active_trip(phone)

    if latitude is None or longitude is None:
        return _GREETING

    async with SessionLocal() as db:
        trip = Trip(origin=_point(latitude, longitude), customer_phone=phone)
        db.add(trip)
        await db.flush()
        new_trip_id = trip.id
        await db.commit()

    await _set_active_trip(phone, new_trip_id)
    asyncio.create_task(dispatch_trip(new_trip_id))
    return _SEARCHING


# --- Barrido: reintenta viajes del bot atorados -------------------------------


async def sweep_stuck_bot_trips() -> None:
    """Corre cada BOT_TRIP_SWEEP_INTERVAL_SECONDS (ver main.py). Dos cosas:
      - Viajes del bot "solicitado" sin oferta viva (dispatch_trip ya
        terminó su pasada, con o sin suerte) → se reintentan.
      - Los que ya llevan más de BOT_TRIP_MAX_WAIT_SECONDS esperando → se
        cancelan y se le avisa al cliente, en vez de dejarlo esperando para
        siempre.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Trip).where(
                Trip.status == TripStatus.SOLICITADO, Trip.customer_phone.isnot(None)
            )
        )
        stuck_trips = list(result.scalars().all())

    now = datetime.now(UTC)
    for trip in stuck_trips:
        age_seconds = (now - trip.requested_at).total_seconds()

        if age_seconds > settings.BOT_TRIP_MAX_WAIT_SECONDS:
            await _give_up_on_trip(trip)
            continue

        # Con una oferta viva, dispatch_trip (esta u otra pasada) ya la está
        # cuidando — no hay que meterse a medio cascadeo de candidatos.
        # offer_expires_at basta como señal: dispatch_trip siempre lo fija
        # junto con offered_driver_id, y lo limpia al rendirse.
        if trip.offer_expires_at is not None and trip.offer_expires_at > now:
            continue

        if not await _acquire_retry_lock(trip.id):
            continue
        logger.info("Viaje %s: reintentando despacho (barrido del bot)", trip.id)
        asyncio.create_task(dispatch_trip(trip.id))


def _retry_lock_key(trip_id: uuid.UUID) -> str:
    return f"wa:dispatch_retry:{trip_id}"


async def _acquire_retry_lock(trip_id: uuid.UUID) -> bool:
    """SET NX — evita relanzar dispatch_trip para el mismo viaje mientras el
    intento anterior sigue vivo (una pasada completa puede tardar hasta
    DISPATCH_MAX_CANDIDATES × DISPATCH_OFFER_TIMEOUT_SECONDS, más que el
    intervalo del barrido). El TTL es la red de seguridad si ese intento
    anterior nunca llegó a limpiar nada (proceso caído a medias)."""
    return bool(
        await redis_client.set(_retry_lock_key(trip_id), "1", ex=90, nx=True)
    )


async def _give_up_on_trip(trip: Trip) -> None:
    async with SessionLocal() as db:
        db_trip = await db.get(Trip, trip.id)
        if db_trip is not None and db_trip.status == TripStatus.SOLICITADO:
            db_trip.status = TripStatus.CANCELADO
            await db.commit()
    await _clear_active_trip(trip.customer_phone)
    await send_whatsapp_message(trip.customer_phone, _GAVE_UP)
    logger.info("Viaje %s: se agotó el tiempo de espera, cancelado", trip.id)
