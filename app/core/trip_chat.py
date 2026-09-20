"""Hilo cliente↔chofer, scoped al viaje.

El pasajero de WhatsApp escribe en el bot; el chofer lee y responde en
la app (REST + el mismo WebSocket /ws/driver que ya usa para ofertas).
No hay un stack paralelo: se guarda en `trip_messages`, se empuja por
Redis Pub/Sub al canal `driver:{id}:chat` (mismo motivo que las ofertas:
el socket puede estar en otra instancia) y, si el chofer no tiene la
app abierta, un push de Expo.

Reglas de seguridad, todas aquí — el bot y el endpoint HTTP solo
traducen estas excepciones a TwiML o a status codes:
  - Solo viajes `asignado` o `en_curso`. En `solicitado` todavía no hay
    chofer; en completado/cancelado el hilo se cierra.
  - Solo el chofer asignado (driver_id) ve y responde. Nadie más, ni el
    que recibió una oferta que no aceptó.
  - El cliente solo llega por el bot de su propio teléfono, que ya está
    amarrado al viaje en Redis (`wa:conv:{phone}`).
  - Techo de mensajes por lado y ventana (incr_with_ttl, igual que el
    login y los pings): un pasajero nervioso no satura al chofer.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.customer_notify import has_customer, notify_customer
from app.core.push import send_push_notification
from app.core.redis_client import incr_with_ttl, publish_trip_chat, redis_client
from app.models import Trip, TripMessage, TripMessageSender, TripStatus

logger = logging.getLogger(__name__)

CHATTABLE_STATUSES = (TripStatus.ASIGNADO, TripStatus.EN_CURSO)
MAX_BODY_LENGTH = 500

# Palabras sueltas que abren el hilo (tras plegar acentos). "mensaje" y
# "hablar" solas también: es lo que teclea alguien que no sabe el comando.
_OPEN_KEYWORDS = {
    "chofer",
    "conductor",
    "mensaje",
    "mensajes",
    "hablar",
    "hablarle",
    "escribir",
    "escribirle",
    "avisar",
    "avisarle",
    "decirle",
}

# Frases de coordinación en el punto de recogida — el caso de uso
# principal. Se buscan como subcadena ya plegada, no como tokens.
_CONTACT_PHRASES = (
    "donde estas",
    "donde estas?",
    "cuanto falta",
    "cuanto tardas",
    "ya llegas",
    "en cuanto llegas",
    "te espero",
    "estoy en",
    "estoy afuera",
    "estoy fuera",
    "en la esquina",
    "en la puerta",
    "color del",
    "de que color",
    "de que taxi",
)

_CLOSE_KEYWORDS = {"listo", "salir"}

# --- Copys en español, los que ve el cliente por WhatsApp -----------------

CHAT_OPEN = (
    "Ya puedes escribirle a tu conductor. Él ve tus mensajes en su app "
    "y te responde por aquí.\n"
    "Escribe *listo* para dejar de chatear, o *cancelar* si ya no necesitas el taxi."
)
CHAT_SENT_FIRST = (
    "Listo, se lo mandamos a tu conductor. Sigue escribiendo por aquí; "
    "él te responde en este mismo chat. Escribe *listo* para dejar de chatear "
    "o *cancelar* si ya no necesitas el taxi."
)
CHAT_SENT = "Listo, se lo mandamos a tu conductor."
CHAT_CLOSED = (
    "Dejamos de reenviar tus mensajes. Si quieres escribirle otra vez, manda *chofer*."
)
CHAT_RATE_LIMITED = (
    "Espera un momento antes de mandar otro mensaje, para no saturar a tu conductor."
)
CHAT_NO_DRIVER = (
    "Todavía estamos buscando tu taxi. En cuanto se asigne uno, podrás escribirle."
)
CHAT_ENDED = "Este viaje ya no está activo, no se pueden enviar más mensajes."
CHAT_NEED_TEXT = "Mándanos el texto del mensaje para tu conductor."
CHAT_TOO_LONG = f"El mensaje es muy largo. Máximo {MAX_BODY_LENGTH} caracteres."
ALREADY_ASSIGNED = (
    "Tu taxi ya va en camino. Si quieres escribirle al conductor, "
    "manda *chofer* o tu mensaje (ej. \"estoy en la esquina\")."
)
DRIVER_REPLY_PREFIX = "🚕 Mensaje de tu conductor:\n"

_PUSH_TITLE = "Mensaje del pasajero"
_PUSH_CHANNEL = "trip-chat"


class ChatClosed(Exception):
    """El viaje no está en un estado que permita chat."""


class ChatNoCounterpart(Exception):
    """Falta el otro lado: sin chofer asignado o sin cliente identificado."""


class ChatRateLimited(Exception):
    """Se superó el techo de mensajes del lado en la ventana actual."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(f"Demasiados mensajes. Reintenta en {self.retry_after}s.")


def _fold(text: str) -> str:
    """Minúsculas sin acentos, para comparar 'dónde estás' con 'donde estas'."""
    nfkd = unicodedata.normalize("NFKD", text.lower().strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def wants_driver_chat(text: str) -> bool:
    """True si el cliente está pidiendo hablar con el chofer, no solo
    contestando el flujo del bot (sí / ok / cancelar)."""
    if not text or not text.strip():
        return False
    folded = _fold(text)
    tokens = set(folded.replace("?", " ").replace("¡", " ").replace("!", " ").split())
    if tokens & _OPEN_KEYWORDS:
        return True
    return any(phrase in folded for phrase in _CONTACT_PHRASES)


def is_chat_open_command_only(text: str) -> bool:
    """'chofer' / 'conductor' a secas: abre el hilo sin mandar ese texto
    al chofer — no es un mensaje, es el comando para empezar a chatear."""
    folded = _fold(text)
    return folded in _OPEN_KEYWORDS


def is_chat_close(text: str) -> bool:
    return _fold(text) in _CLOSE_KEYWORDS


def normalize_body(body: str) -> str:
    cleaned = (body or "").strip()
    if not cleaned:
        raise ValueError("empty")
    if len(cleaned) > MAX_BODY_LENGTH:
        raise ValueError("too_long")
    return cleaned


def can_chat(trip: Trip) -> bool:
    return trip.status in CHATTABLE_STATUSES and trip.driver_id is not None


def _counter_key(trip_id: uuid.UUID, sender: str) -> str:
    return f"trip:chat:{trip_id}:{sender}"


async def check_rate_limit(trip_id: uuid.UUID, sender: str) -> None:
    total = await incr_with_ttl(
        _counter_key(trip_id, sender),
        settings.CHAT_WINDOW_SECONDS,
    )
    if total > settings.CHAT_MAX_PER_WINDOW:
        ttl = await redis_client.ttl(_counter_key(trip_id, sender))
        logger.info(
            "Viaje %s: techo de chat de %s (%s en %ss)",
            trip_id,
            sender,
            total,
            settings.CHAT_WINDOW_SECONDS,
        )
        raise ChatRateLimited(retry_after=ttl)


async def reset_rate_limit(trip_id: uuid.UUID, sender: str) -> None:
    """Solo para pruebas: la ventana vence sola en operación."""
    await redis_client.delete(_counter_key(trip_id, sender))


def _message_payload(trip: Trip, message: TripMessage) -> dict:
    created = message.created_at.isoformat() if message.created_at else None
    return {
        "trip_id": str(trip.id),
        "message_id": str(message.id),
        "sender": message.sender,
        "body": message.body,
        "created_at": created,
    }


async def notify_driver_of_message(
    trip: Trip, message: TripMessage, *, push_token: str | None
) -> None:
    """Empuja el mensaje al socket del chofer y, si hay token, un push.

    Nunca lanza: perder un aviso es malo, perder el persistido no. El
    chofer puede recuperar el hilo con GET /trips/{id}/messages.
    """
    if trip.driver_id is None:
        return
    payload = _message_payload(trip, message)
    try:
        await publish_trip_chat(str(trip.driver_id), payload)
    except Exception:
        logger.error(
            "Viaje %s: no se pudo publicar el mensaje %s en Redis",
            trip.id,
            message.id,
            exc_info=True,
        )
    if not push_token:
        return
    preview = message.body if len(message.body) <= 80 else message.body[:77] + "…"
    await send_push_notification(
        push_token,
        title=_PUSH_TITLE,
        body=preview,
        data={"type": "trip_chat", "trip_id": str(trip.id), "message_id": str(message.id)},
        channel_id=_PUSH_CHANNEL,
    )


async def notify_customer_of_reply(trip: Trip, message: TripMessage) -> None:
    await notify_customer(trip, f"{DRIVER_REPLY_PREFIX}{message.body}")


async def post_message(
    db: AsyncSession,
    trip: Trip,
    sender: TripMessageSender,
    body: str,
) -> TripMessage:
    """Valida, cuenta, persiste. El caller hace commit y dispara el aviso.

    No notifica aquí a propósito: el bot y el endpoint HTTP commitean
    primero (el mensaje no se puede perder si Expo/Twilio fallan) y
    avisan después.
    """
    if trip.status not in CHATTABLE_STATUSES:
        raise ChatClosed
    if sender is TripMessageSender.CUSTOMER and trip.driver_id is None:
        raise ChatNoCounterpart
    if sender is TripMessageSender.DRIVER and not has_customer(trip):
        raise ChatNoCounterpart

    try:
        cleaned = normalize_body(body)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    await check_rate_limit(trip.id, sender.value)

    message = TripMessage(
        trip_id=trip.id,
        sender=sender.value,
        body=cleaned,
        created_at=datetime.now(UTC),
    )
    db.add(message)
    await db.flush()
    return message
