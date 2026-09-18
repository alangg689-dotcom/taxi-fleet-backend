"""Lógica de conversación del bot de WhatsApp.

Mismo camino de despacho que ya usan el dashboard (POST /trips/dispatch) y
las pruebas manuales: aquí se llama directo a dispatch_trip(), sin pasar por
el endpoint HTTP con auth de operador — tal como se dejó anotado desde que se
armó ese endpoint (ver docstring de dispatch_new_trip en app.api.trips).

La conversación es una máquina de estados corta, guardada en Redis por
teléfono: ubicación → destino → confirmación → despacho, más la calificación
al terminar y la confirmación al cancelar. (La versión original despachaba
con la pura ubicación, sin confirmación, para no meter fricción; se cambió
por decisión de producto el 13-ago-2026: capturar el destino vale la fricción
porque el despachador y el chofer lo ven antes de aceptar.)

Todo se contesta por palabras ("sí", "cancelar", "1".."5"), no con botones:
los Reply Buttons de WhatsApp solo se pueden mandar como plantillas de
contenido aprobadas por Meta (Content API de Twilio) y el sandbox no las
soporta. Los textos ya están escritos como si fueran botones — migrar es
registrar las plantillas y cambiar el emisor, no reescribir este flujo.

Un viaje sin candidatos (o que nadie aceptó) NO se cancela solo — se queda
"solicitado" y sweep_stuck_bot_trips lo reintenta periódicamente; solo se
avisa y se cancela al agotarse la espera (dinámica, ver app.core.demand).
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text as sa_text

from app.api.location import _point
from app.config import settings
from app.core.customer_notify import notify_customer
from app.core.demand import customer_wait_message, measure_demand
from app.core.dispatch import dispatch_trip, set_vehicle_status
from app.core.redis_client import redis_client
from app.database import SessionLocal
from app.models import CustomerChannel, Trip, TripStatus, VehicleStatus

logger = logging.getLogger(__name__)

# Una hora de inactividad y se olvida la conversación — si el cliente vuelve
# a escribir después de eso, empieza de cero como si fuera la primera vez.
_CONVERSATION_TTL_SECONDS = 3600

_CANCEL_KEYWORDS = {"cancelar", "cancela", "cancel", "❌", "no"}
_CONFIRM_KEYWORDS = {"si", "sí", "confirmar", "confirmo", "✅", "1", "ok"}

_GREETING = (
    "¡Hola! Soy el asistente de Taxis CTM. Para pedir un taxi, comparte tu "
    "ubicación (el clip de adjuntar → Ubicación). "
    "Escribe *cancelar* en cualquier momento para cancelar."
)
_ASK_DESTINATION = (
    "📍 Recibimos tu ubicación. Para asignarte un taxi, envíanos tu destino: "
    "comparte otra ubicación o escríbelo con palabras (ej. \"Clínica 4\")."
)
_ALREADY_ACTIVE = "Ya tienes un viaje en curso. En cuanto haya novedades te avisamos por aquí."
# El texto de "buscando taxi" vive en app.core.demand.customer_wait_message:
# depende de la presión de demanda del momento, no es una constante.
_CANCELLED = "Viaje cancelado. Escríbenos cuando quieras pedir otro."
_CANCEL_CONFIRM = (
    "¿Estás seguro de cancelar tu viaje? Responde *sí* para cancelarlo o "
    "cualquier otra cosa para seguir esperando."
)
_NOTHING_TO_CANCEL = "No tienes ningún viaje activo para cancelar."
_REQUEST_DISCARDED = "Listo, descartamos esa solicitud. Comparte tu ubicación cuando quieras pedir otro taxi."
_GAVE_UP = (
    "Ya llevamos un rato buscando y no encontramos un taxi disponible cerca de ti. "
    "Escríbenos de nuevo cuando quieras intentarlo otra vez."
)
_RATING_THANKS = "¡Gracias por tu calificación! Nos ayuda a mejorar el servicio."
_RATING_INVALID = "Para calificar tu viaje responde con un número del 1 al 5."


# --- Estado de conversación en Redis ------------------------------------------
#
# Un solo valor por teléfono, JSON con "stage". Compatible hacia atrás: las
# conversaciones que ya existían guardaban el UUID del viaje a secas — se leen
# como stage "active" implícito.

def _state_key(phone: str) -> str:
    return f"wa:conv:{phone}"


async def _get_state(phone: str) -> dict:
    raw = await redis_client.get(_state_key(phone))
    if not raw:
        return {}
    try:
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except ValueError:
        # Formato anterior: el UUID del viaje activo, pelado.
        return {"stage": "active", "trip_id": raw}


async def _set_state(phone: str, state: dict) -> None:
    await redis_client.set(
        _state_key(phone), json.dumps(state), ex=_CONVERSATION_TTL_SECONDS
    )


async def _get_active_trip_id(phone: str) -> uuid.UUID | None:
    state = await _get_state(phone)
    if state.get("stage") in ("active", "cancel_confirm") and state.get("trip_id"):
        return uuid.UUID(state["trip_id"])
    return None


async def _set_active_trip(phone: str, trip_id: uuid.UUID) -> None:
    await _set_state(phone, {"stage": "active", "trip_id": str(trip_id)})


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


def _describe_destination(state: dict) -> str:
    return state.get("dest_address") or "el punto que marcaste en el mapa"


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
    state = await _get_state(phone)
    stage = state.get("stage")
    text = (body or "").strip().lower()
    wants_cancel = text in _CANCEL_KEYWORDS
    confirms = text in _CONFIRM_KEYWORDS

    # --- Calificación pendiente: lo único que se espera es un número -------
    if stage == "rating":
        if text in {"1", "2", "3", "4", "5"}:
            await _store_rating(uuid.UUID(state["trip_id"]), int(text))
            await _clear_active_trip(phone)
            return _RATING_THANKS
        if latitude is not None or wants_cancel:
            # Nueva ubicación o "cancelar": el cliente ya pasó a otra cosa.
            # La calificación se descarta sin drama — era opcional.
            await _clear_active_trip(phone)
            state, stage = {}, None
        else:
            return _RATING_INVALID

    # --- Confirmación de cancelación pendiente -----------------------------
    if stage == "cancel_confirm":
        trip_id = uuid.UUID(state["trip_id"])
        if confirms or wants_cancel:
            await _cancel_trip_for_customer(phone, trip_id)
            return _CANCELLED
        # Cualquier otra cosa = se arrepintió de cancelar; el viaje sigue.
        await _set_active_trip(phone, trip_id)
        return _ALREADY_ACTIVE

    # --- "cancelar" en cualquier otra etapa --------------------------------
    if wants_cancel:
        if stage in ("awaiting_destination", "awaiting_confirm"):
            await _clear_active_trip(phone)
            return _REQUEST_DISCARDED
        trip_id = await _get_active_trip_id(phone)
        if trip_id is None:
            return _NOTHING_TO_CANCEL
        async with SessionLocal() as db:
            trip = await db.get(Trip, trip_id)
        if not await _trip_still_active(trip):
            await _clear_active_trip(phone)
            return _NOTHING_TO_CANCEL
        # Pregunta antes de tirar el viaje: "cancelar" tecleado a medias o por
        # error dejaría a alguien sin el taxi que sí quería.
        await _set_state(phone, {"stage": "cancel_confirm", "trip_id": str(trip_id)})
        return _CANCEL_CONFIRM

    # --- Con viaje activo, cualquier mensaje repite el estado ---------------
    if stage == "active":
        trip_id = uuid.UUID(state["trip_id"])
        async with SessionLocal() as db:
            trip = await db.get(Trip, trip_id)
        if await _trip_still_active(trip):
            return _ALREADY_ACTIVE
        await _clear_active_trip(phone)
        state, stage = {}, None

    # --- Esperando el destino ----------------------------------------------
    if stage == "awaiting_destination":
        if latitude is not None and longitude is not None:
            state.update(dest_lat=latitude, dest_lng=longitude, dest_address=None)
        elif body and body.strip():
            state["dest_address"] = body.strip()[:255]
        else:
            return _ASK_DESTINATION
        state["stage"] = "awaiting_confirm"
        await _set_state(phone, state)
        return (
            f"📋 Perfecto. Viaje desde tu ubicación actual hasta "
            f"*{_describe_destination(state)}*. ¿Confirmas?\n"
            "✅ Responde *sí* para confirmar\n"
            "❌ Responde *cancelar* para descartar"
        )

    # --- Esperando la confirmación ------------------------------------------
    if stage == "awaiting_confirm":
        if confirms:
            return await _create_and_dispatch(phone, state)
        if latitude is not None and longitude is not None:
            # Mandó otra ubicación en vez de contestar: la tomamos como
            # corrección del destino, que es lo que casi siempre significa.
            state.update(dest_lat=latitude, dest_lng=longitude, dest_address=None)
            await _set_state(phone, state)
            return (
                f"📋 Viaje desde tu ubicación actual hasta "
                f"*{_describe_destination(state)}*. ¿Confirmas?\n"
                "✅ Responde *sí* / ❌ Responde *cancelar*"
            )
        return (
            "Responde *sí* para confirmar tu viaje o *cancelar* para descartarlo."
        )

    # --- Sin conversación: el arranque es compartir la ubicación ------------
    if latitude is None or longitude is None:
        return _GREETING

    await _set_state(
        phone,
        {"stage": "awaiting_destination", "origin_lat": latitude, "origin_lng": longitude},
    )
    return _ASK_DESTINATION


async def _create_and_dispatch(phone: str, state: dict) -> str:
    """Crea el viaje ya confirmado y lanza el despacho. El destino puede venir
    como coordenadas (compartió ubicación) o solo como texto de referencia."""
    destination = None
    if state.get("dest_lat") is not None:
        destination = _point(state["dest_lat"], state["dest_lng"])

    async with SessionLocal() as db:
        trip = Trip(
            origin=_point(state["origin_lat"], state["origin_lng"]),
            destination=destination,
            destination_address=state.get("dest_address"),
            customer_channel=CustomerChannel.WHATSAPP.value,
            customer_phone=phone,
        )
        db.add(trip)
        await db.flush()
        new_trip_id = trip.id
        await db.commit()
        # Se mide DESPUÉS de insertar para que este viaje cuente en la cola:
        # medir antes haría que el quinto viaje de una racha se lleve la
        # respuesta optimista que ya no le corresponde.
        demand = await measure_demand(db)
        position = await _queue_position(db, new_trip_id)

    await _set_active_trip(phone, new_trip_id)
    asyncio.create_task(dispatch_trip(new_trip_id))
    return customer_wait_message(demand, queue_position=position)


async def _queue_position(db, trip_id: uuid.UUID) -> int:
    """Lugar de este viaje entre los que siguen esperando chofer (1 = el más
    viejo). Mismo recorte de edad que la medición de demanda: los viajes
    abandonados de hace horas no forman parte de ninguna fila real."""
    return (
        await db.execute(
            sa_text(
                """
                SELECT count(*) + 1 FROM trips
                WHERE status = 'solicitado'
                  AND requested_at > now() - make_interval(secs => :ceiling)
                  AND requested_at < (SELECT requested_at FROM trips WHERE id = :trip_id)
                """
            ),
            {
                "ceiling": settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS,
                "trip_id": trip_id,
            },
        )
    ).scalar_one()


async def _store_rating(trip_id: uuid.UUID, rating: int) -> None:
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        # Solo si sigue completado y sin calificar: un doble mensaje "5" no
        # debe pisar nada, y un viaje que se reabrió (no pasa hoy) tampoco.
        if trip is not None and trip.status == TripStatus.COMPLETADO and trip.rating is None:
            trip.rating = rating
            await db.commit()


async def prompt_rating(trip: Trip) -> None:
    """La invita a calificar quien completa el viaje (app.api.trips): aquí
    solo se deja la conversación en la etapa de calificación. Solo aplica a
    clientes de WhatsApp — la máquina de estados vive keyed por teléfono."""
    if not trip.customer_phone:
        return
    await _set_state(
        trip.customer_phone, {"stage": "rating", "trip_id": str(trip.id)}
    )


# --- Barrido: reintenta viajes del bot atorados -------------------------------


async def sweep_stuck_bot_trips() -> None:
    """Corre cada BOT_TRIP_SWEEP_INTERVAL_SECONDS (ver main.py). Dos cosas:
      - Viajes del bot "solicitado" sin oferta viva (dispatch_trip ya
        terminó su pasada, con o sin suerte) → se reintentan.
      - Los que ya agotaron su espera → se cancelan y se le avisa al cliente,
        en vez de dejarlo esperando para siempre.

    El tope de espera ya no es una constante: sale de la presión de demanda
    del momento (ver app.core.demand). Se mide una vez por pasada y se aplica
    a todos los viajes de esa tanda — medirlo por viaje daría lecturas
    distintas dentro del mismo barrido sin que nada haya cambiado.
    """
    async with SessionLocal() as db:
        demand = await measure_demand(db)
        # "Tiene cliente" y no "tiene teléfono": desde que existe Telegram un
        # viaje de bot puede no traer teléfono, y filtrar solo por él dejaba a
        # esos esperando en silencio para siempre, sin reintento y sin aviso al
        # agotarse la espera.
        #
        # Los dos con OR, no solo el canal: un viaje con teléfono y sin canal
        # sigue siendo del bot de WhatsApp — así son todos los anteriores a la
        # migración 0013 y los que cree cualquier código que aún no fije el
        # canal. Exigir canal los volvía invisibles para este barrido.
        result = await db.execute(
            select(Trip).where(
                Trip.status == TripStatus.SOLICITADO,
                or_(Trip.customer_channel.isnot(None), Trip.customer_phone.isnot(None)),
            )
        )
        stuck_trips = list(result.scalars().all())

    now = datetime.now(UTC)
    for trip in stuck_trips:
        age_seconds = (now - trip.requested_at).total_seconds()

        if age_seconds > demand.max_wait_seconds:
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
    # El estado de conversación en Redis solo existe para WhatsApp: los demás
    # canales resuelven "¿este cliente ya tiene viaje?" consultando la tabla
    # (ver app.api.bot._find_active_trip), así que no hay nada que limpiar.
    if trip.customer_phone:
        await _clear_active_trip(trip.customer_phone)
    await notify_customer(trip, _GAVE_UP)
    logger.info("Viaje %s: se agotó el tiempo de espera, cancelado", trip.id)
