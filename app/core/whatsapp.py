"""Envío de mensajes de WhatsApp al cliente, vía la API de Twilio.

Único uso que le queda a Twilio en el proyecto — el login de chofer ya no
pasa por aquí, usa PIN (ver app.api.auth). Se manda con el prefijo
"whatsapp:" tanto en el remitente como en el destinatario. Nunca lanza —
igual que app.core.push, es una respuesta de la conversación, no algo de
lo que dependa el resto del flujo del webhook para responderle a Twilio a
tiempo.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


async def send_whatsapp_message(to_phone: str, body: str) -> None:
    url = f"{_TWILIO_API_BASE}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                data={
                    "To": f"whatsapp:{to_phone}",
                    "From": settings.TWILIO_WHATSAPP_FROM,
                    "Body": body,
                },
            )
        if response.status_code >= 400:
            logger.error(
                "Twilio rechazó el mensaje de WhatsApp a %s (%s): %s",
                to_phone,
                response.status_code,
                response.text,
            )
    except httpx.HTTPError:
        logger.error("No se pudo contactar a Twilio para WhatsApp", exc_info=True)
