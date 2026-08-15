"""Puerta de entrada de los bots de cliente (Telegram, y lo que venga).

A diferencia del webhook de WhatsApp, que recibe la conversación cruda de
Twilio y la interpreta aquí dentro (app.core.whatsapp_bot), este endpoint
recibe una petición ya interpretada: el proceso del bot conversa con el
cliente por su cuenta y solo llama aquí cuando tiene una ubicación. Eso deja
la lógica de conversación fuera del backend, que es donde debe estar cuando
cada canal tiene su propia forma de teclados, botones y adjuntos.

El despacho es el mismo de siempre — dispatch_trip(), el que ya usan el
dashboard y el bot de WhatsApp. Aquí no se reimplementa nada de la regla de
sitios ni de la cascada de candidatos: se crea el viaje y se lanza el motor.

**Autenticación**: X-Bot-Key. Este endpoint crea viajes reales sin sesión de
operador ni de chofer; abierto, cualquiera podría llenar la flotilla de
servicios fantasma y dejar a la gente sin taxi. Con BOT_API_KEY vacía el
endpoint contesta 503 en vez de quedar abierto — apagado es un estado
seguro, abierto no.
"""

import asyncio
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.location import _point
from app.config import settings
from app.core.demand import customer_wait_message, measure_demand
from app.core.dispatch import dispatch_trip, set_vehicle_status
from app.core.redis_client import incr_with_ttl
from app.database import get_db
from app.models import CustomerChannel, Trip, TripStatus, VehicleStatus
from app.schemas.bot import (
    BotCancelRequest,
    BotCancelResponse,
    BotRideRequest,
    BotRideResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bot", tags=["bot de clientes"])

_ACTIVE_STATUSES = (TripStatus.SOLICITADO, TripStatus.ASIGNADO, TripStatus.EN_CURSO)


async def require_bot_key(x_bot_key: str = Header(..., alias="X-Bot-Key")) -> None:
    if not settings.BOT_API_KEY:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "El canal de bots está apagado (BOT_API_KEY sin configurar)",
        )
    # compare_digest y no ==: la comparación de cadenas de Python corta en el
    # primer byte distinto y filtra la clave a quien mida los tiempos.
    if not secrets.compare_digest(x_bot_key, settings.BOT_API_KEY):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Clave de bot inválida")


def _identity_filter(channel: CustomerChannel, customer_id: str):
    """WhatsApp identifica por teléfono y Telegram por chat_id: son columnas
    distintas y no se pueden mezclar (un chat_id no es un teléfono aunque los
    dos sean dígitos)."""
    column = Trip.customer_phone if channel is CustomerChannel.WHATSAPP else Trip.customer_chat_id
    return (Trip.customer_channel == channel.value) & (column == customer_id)


async def _find_active_trip(
    db: AsyncSession, channel: CustomerChannel, customer_id: str
) -> Trip | None:
    result = await db.execute(
        select(Trip)
        .where(_identity_filter(channel, customer_id), Trip.status.in_(_ACTIVE_STATUSES))
        .order_by(Trip.requested_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.post(
    "/request-ride",
    response_model=BotRideResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_bot_key)],
)
async def request_ride(
    payload: BotRideRequest, db: AsyncSession = Depends(get_db)
) -> BotRideResponse:
    """Crea el viaje y lanza el motor de despacho.

    202 y no 201 a propósito: cuando esto contesta, el viaje existe pero
    todavía no tiene chofer — la cascada de candidatos apenas empieza y puede
    tardar hasta DISPATCH_MAX_CANDIDATES × DISPATCH_OFFER_TIMEOUT_SECONDS. Al
    cliente se le avisa por su canal cuando alguien acepte (ver
    app.api.trips.accept_trip) y cuando la unidad llegue
    (app.core.arrival).
    """
    existing = await _find_active_trip(db, payload.channel, payload.customer_id)
    if existing is not None:
        return BotRideResponse(
            trip_id=existing.id, status=existing.status, already_active=True
        )

    # Techo de VIAJES CREADOS por cliente, no de peticiones: se cuenta después
    # del already_active de arriba a propósito. Alguien parado en la calle
    # reenvía su ubicación media docena de veces mientras espera, y cobrarle
    # esas al presupuesto lo dejaría con un "demasiadas solicitudes" sin haber
    # hecho nada malo. Lo que hay que frenar es el bucle de cancelar-y-repedir
    # que llena la flotilla de servicios fantasma.
    created = await incr_with_ttl(
        f"bot:ride_rl:{payload.channel.value}:{payload.customer_id}",
        settings.BOT_RIDE_WINDOW_SECONDS,
    )
    if created > settings.BOT_RIDE_MAX_PER_WINDOW:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Demasiadas solicitudes seguidas; espera unos minutos.",
        )

    trip = Trip(
        origin=_point(payload.lat, payload.lng),
        customer_channel=payload.channel.value,
        customer_phone=(
            payload.customer_id if payload.channel is CustomerChannel.WHATSAPP else None
        ),
        customer_chat_id=(
            payload.customer_id if payload.channel is CustomerChannel.TELEGRAM else None
        ),
    )
    db.add(trip)
    await db.flush()
    trip_id = trip.id
    await db.commit()

    # create_task y no await: el motor recorre candidatos esperando respuesta
    # de cada uno, y el bot necesita contestarle al cliente ahora, no en dos
    # minutos. Mismo patrón que el bot de WhatsApp y el dashboard.
    asyncio.create_task(dispatch_trip(trip_id))

    logger.info(
        "Viaje %s creado desde el bot de %s", trip_id, payload.channel.value
    )
    # Después de crear el viaje, para que este cuente en la cola: si es el que
    # tira la balanza, es justo a este cliente al que hay que avisarle.
    demand = await measure_demand(db)
    return BotRideResponse(
        trip_id=trip_id,
        status=TripStatus.SOLICITADO,
        high_demand=demand.high_demand,
        wait_message=customer_wait_message(demand),
    )


@router.post(
    "/cancel-ride",
    response_model=BotCancelResponse,
    dependencies=[Depends(require_bot_key)],
)
async def cancel_ride(
    payload: BotCancelRequest, db: AsyncSession = Depends(get_db)
) -> BotCancelResponse:
    """El cliente se arrepiente. Sin esto, quien pide un taxi por error queda
    bloqueado hasta que expire (BOT_TRIP_MAX_WAIT_SECONDS) y de paso ocupa una
    unidad que podría estar tomando otro servicio — el bot de WhatsApp ya
    tenía su comando CANCELAR por lo mismo."""
    trip = await _find_active_trip(db, payload.channel, payload.customer_id)
    if trip is None:
        return BotCancelResponse(cancelled=False)

    trip_id = trip.id
    vehicle_id = trip.vehicle_id
    trip.status = TripStatus.CANCELADO
    await db.commit()
    # Libera la unidad y devuelve su lugar en la fila si lo tenía — es lo
    # mismo que hace el comando CANCELAR del bot de WhatsApp.
    await set_vehicle_status(db, vehicle_id, VehicleStatus.DISPONIBLE, trip_id=trip_id)

    logger.info("Viaje %s cancelado por el cliente desde el bot", trip_id)
    return BotCancelResponse(cancelled=True, trip_id=trip_id)


@router.get(
    "/trips/{trip_id}/status",
    response_model=BotRideResponse,
    dependencies=[Depends(require_bot_key)],
)
async def trip_status(
    trip_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> BotRideResponse:
    """Consulta puntual para el bot. Los avisos importantes salen empujados
    desde el backend (app.core.customer_notify), pero un bot que se reinició y
    perdió su estado necesita poder preguntar."""
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Viaje no encontrado")
    return BotRideResponse(
        trip_id=trip.id,
        status=trip.status,
        already_active=trip.status in _ACTIVE_STATUSES,
    )
