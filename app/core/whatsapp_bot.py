"""Lógica de conversación del bot de WhatsApp.

Mismo camino de despacho que ya usan el dashboard (POST /trips/dispatch) y
las pruebas manuales: aquí se llama directo a dispatch_trip(), sin pasar por
el endpoint HTTP con auth de operador — tal como se dejó anotado desde que se
armó ese endpoint (ver docstring de dispatch_new_trip en app.api.trips).

La conversación es deliberadamente simple, sin árbol de menús: cualquier
mensaje de texto responde con instrucciones para compartir ubicación: en
cuanto llega una ubicación, se despacha. No hay paso de confirmación —
agregar uno es agregar fricción a alguien parado en la calle esperando un
taxi.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app.api.location import _point
from app.core.dispatch import dispatch_trip
from app.core.redis_client import redis_client
from app.database import SessionLocal
from app.models import Trip, TripStatus

# Una hora de inactividad y se olvida la conversación — si el cliente vuelve
# a escribir después de eso, empieza de cero como si fuera la primera vez.
_CONVERSATION_TTL_SECONDS = 3600

# Red de seguridad, no el camino normal: dispatch_trip cancela un viaje del
# bot en cuanto se rinde (ver app.core.dispatch), así que en el día a día
# nunca debería quedar uno "solicitado" tanto tiempo. Pero si el proceso del
# backend se cae a media tarea (pasó varias veces en desarrollo), ese viaje
# se queda huérfano para siempre sin este límite — y con él, un cliente
# bloqueado sin poder volver a pedir taxi hasta que alguien lo arregle a
# mano en la base de datos.
_ORPHAN_TRIP_MAX_AGE_SECONDS = _CONVERSATION_TTL_SECONDS

_GREETING = (
    "¡Hola! Soy el asistente de Los Tigres. Para pedir un taxi, comparte tu "
    "ubicación (el clip de adjuntar → Ubicación) y te buscamos el más cercano."
)
_ALREADY_ACTIVE = "Ya tienes un viaje en curso. En cuanto un chofer confirme, te avisamos por aquí."
_SEARCHING = "Buscando un taxi cerca de ti… te avisamos en cuanto uno confirme."


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
    """"solicitado" para un viaje del bot normalmente significa que
    dispatch_trip lo sigue recorriendo de verdad: en cuanto se agotan los
    candidatos (o no hay ninguno) sin que nadie acepte, dispatch_trip lo pasa
    a "cancelado" (ver app.core.dispatch). El tope de edad de abajo es solo
    la red de seguridad para cuando eso no llegó a pasar (el backend se cayó
    a media tarea), no la regla principal."""
    if trip is None:
        return False
    if trip.status in (TripStatus.ASIGNADO, TripStatus.EN_CURSO):
        return True
    if trip.status == TripStatus.SOLICITADO:
        cutoff = datetime.now(UTC) - timedelta(seconds=_ORPHAN_TRIP_MAX_AGE_SECONDS)
        return trip.requested_at > cutoff
    return False


async def handle_incoming_message(
    phone: str, latitude: float | None, longitude: float | None
) -> str:
    """Decide la respuesta al mensaje que acaba de mandar `phone`. Nunca
    lanza excepciones de negocio hacia arriba: el webhook necesita poder
    contestarle algo a Twilio siempre, o Twilio reintenta la entrega y
    duplica mensajes."""
    trip_id = await _get_active_trip_id(phone)
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
