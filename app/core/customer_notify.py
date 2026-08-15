"""Un solo punto para avisarle al cliente, sin que quien avisa sepa por dónde.

Antes de que existiera Telegram, cada lugar que le hablaba al cliente llamaba
a send_whatsapp_message directo. Con dos canales eso obliga a repetir el mismo
`if` en aceptar-viaje, en el barrido de atorados y en el aviso de llegada, y
basta olvidarlo en uno para que el cliente de un canal se quede sin enterarse.
Aquí se resuelve una vez: los llamadores pasan el viaje y el texto.

Un viaje de operador/dashboard no tiene cliente identificado (los tres campos
nulos) y simplemente no se avisa a nadie — no es un error.
"""

import logging

from app.core.telegram import send_telegram_message
from app.core.whatsapp import send_whatsapp_message
from app.models import CustomerChannel, Trip

logger = logging.getLogger(__name__)


async def notify_customer(trip: Trip, body: str) -> None:
    """Nunca lanza, y ahora de verdad: los emisores tragan lo suyo (httpx),
    pero eso deja fuera todo lo demás — un timeout de asyncio, una respuesta
    que no es JSON, un fallo de DNS envuelto raro. Cualquiera de esos subiendo
    tumbaría el flujo que llamó, y quien llama es aceptar un viaje o
    completarlo: perder un aviso es malo, perder la aceptación del viaje es
    peor. La red exterior nunca debe poder voltear una operación de negocio."""
    try:
        await _notify(trip, body)
    except Exception:  # noqa: BLE001 — es justo el punto: aquí se corta todo
        logger.error(
            "Viaje %s: no se pudo avisar al cliente por %s",
            trip.id,
            trip.customer_channel,
            exc_info=True,
        )


async def _notify(trip: Trip, body: str) -> None:
    if trip.customer_channel is None:
        # Sin canal pero con teléfono = viaje de WhatsApp anterior a la
        # migración 0013 que por alguna razón no se rellenó. Se atiende igual
        # en vez de dejar a ese cliente mudo.
        if trip.customer_phone:
            await send_whatsapp_message(trip.customer_phone, body)
        return

    if trip.customer_channel == CustomerChannel.WHATSAPP.value:
        if trip.customer_phone:
            await send_whatsapp_message(trip.customer_phone, body)
        return

    if trip.customer_channel == CustomerChannel.TELEGRAM.value:
        if trip.customer_chat_id:
            await send_telegram_message(trip.customer_chat_id, body)
        return

    logger.error(
        "Viaje %s: canal de cliente desconocido %r, no se pudo avisar",
        trip.id,
        trip.customer_channel,
    )


def has_customer(trip: Trip) -> bool:
    """Si hay alguien esperando noticias de este viaje. Lo usan el barrido y
    el aviso de llegada para no trabajar de más en viajes de operador."""
    return bool(trip.customer_phone or trip.customer_chat_id)
