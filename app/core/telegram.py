"""Envío de mensajes de Telegram al cliente, vía la Bot API.

Espejo de app.core.whatsapp: el backend le habla al cliente directo por la API
de Telegram, sin pasar por el proceso del bot. El bot (bot/telegram_bot.py)
solo atiende lo que ENTRA; todo lo que sale — "un taxi va en camino", "el taxi
llegó" — sale de aquí, porque quien conoce esos eventos es el backend y no
tendría cómo despertar al proceso del bot para pedírselo.

Nunca lanza, igual que app.core.push y app.core.whatsapp: un aviso al cliente
no puede tumbar el flujo que lo disparó (aceptar un viaje, ingerir un lote de
pings).
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"


async def send_telegram_message(chat_id: str, body: str) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN vacío: se descarta el mensaje a %s. "
            "El canal de Telegram está apagado.",
            chat_id,
        )
        return

    url = f"{_TELEGRAM_API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json={"chat_id": chat_id, "text": body})
        if response.status_code >= 400:
            # 403 es el caso normal, no un bug: el cliente bloqueó al bot o
            # borró la conversación. No hay nada que reintentar.
            logger.error(
                "Telegram rechazó el mensaje a %s (%s): %s",
                chat_id,
                response.status_code,
                response.text,
            )
    except httpx.HTTPError:
        logger.error("No se pudo contactar a la API de Telegram", exc_info=True)
