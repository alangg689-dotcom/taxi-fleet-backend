"""Envío de mensajes de WhatsApp vía la API de Twilio.

Dos usos: el bot de clientes (app.core.whatsapp_bot) y la entrega de la
device_key GPS al chofer (notify_driver_device_key). El login de chofer ya
no pasa por aquí, usa PIN (ver app.api.auth). Se manda con el prefijo
"whatsapp:" tanto en el remitente como en el destinatario. Nunca lanza —
igual que app.core.push: un aviso no puede tumbar el alta de una unidad
ni la respuesta del webhook.
"""

import logging

import httpx

from app.config import settings
from app.core.phone import InvalidPhoneError, normalize_mx_phone

logger = logging.getLogger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


def twilio_whatsapp_configured() -> bool:
    return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN)


def device_key_whatsapp_body(
    plate: str, folio_ctm: str | None, device_key: str
) -> str:
    """Texto en español. El folio es ID visible, nunca la clave."""
    unit = f"tu unidad {plate}"
    folio = (folio_ctm or "").strip()
    if folio:
        unit = f"{unit} (folio {folio})"
    return (
        f"Taxis CTM — Clave GPS de {unit}. "
        "Pégala en la app del chofer en Configuración del dispositivo. "
        f"No es tu Folio ni tu PIN:\n{device_key}"
    )


def _whatsapp_destination(phone: str) -> str:
    """E.164 para Twilio. Los choferes se guardan en 10 dígitos o +52."""
    stripped = phone.strip()
    try:
        digits = normalize_mx_phone(stripped)
    except InvalidPhoneError:
        digits = None
    if digits:
        return f"+52{digits}"
    return stripped


async def send_whatsapp_message(to_phone: str, body: str) -> None:
    if not twilio_whatsapp_configured():
        logger.warning(
            "TWILIO_ACCOUNT_SID o TWILIO_AUTH_TOKEN vacío: se descarta el "
            "mensaje de WhatsApp a %s. Configura también TWILIO_WHATSAPP_FROM.",
            to_phone,
        )
        return

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


async def notify_driver_device_key(
    phone: str | None,
    plate: str,
    folio_ctm: str | None,
    device_key: str,
) -> None:
    """Manda la device_key GPS al teléfono del chofer, una vez.

    El folio CTM no es secreto: va en el texto como identificador de la
    unidad, nunca en el lugar de la clave. Sin teléfono, o sin credenciales
    Twilio, solo se registra un aviso — el llamador sigue devolviendo la
    clave en JSON.
    """
    if not phone or not phone.strip():
        logger.info(
            "Sin teléfono de chofer: no se envía la device_key GPS de %s "
            "(folio %s) por WhatsApp",
            plate,
            folio_ctm or "s/n",
        )
        return
    if not device_key:
        logger.error(
            "notify_driver_device_key: device_key vacía para %s; no se envía",
            plate,
        )
        return

    destination = _whatsapp_destination(phone)
    body = device_key_whatsapp_body(plate, folio_ctm, device_key)
    try:
        await send_whatsapp_message(destination, body)
    except Exception:  # noqa: BLE001 — un aviso no puede tumbar el alta
        logger.error(
            "No se pudo WhatsAppearle la device_key GPS de %s a %s",
            plate,
            destination,
            exc_info=True,
        )
